"""工作区：一份用户数据，代码共享、状态不串用。

目录约定（`<workspace>` 是用户数据根，代码在别处）::

    workspace.json                 工作区元信息：活动资料/方案、schema 版本、迁移记录
    candidate/revisions/<id>.json  候选人资料版本（不可变）
    candidate/raw/<sha>.txt        简历原文（可选；按内容哈希命名，不去重用户隐私）
    plans/revisions/<id>.json      求职方案版本（不可变）
    strategies/revisions/<id>.json 策略版本（不可变）
    tasks/<task_id>.json           外部 AI 工作项与提交结果
    interviews/<sid>/session.json  面试会话；turns.jsonl 逐轮追加
    audit/audit.jsonl              谁在什么时候改了什么
    objects/                       其它不可变内容（按哈希寻址）

版本一律"新增不可变文件 + 移动指针"，指针更新用比较并交换（CAS），失败抛 `revision_conflict`。
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import Conflict, NotFound
from .jsonio import digest, new_id, now_iso, read_json, read_jsonl, append_jsonl, write_json
from .models import (
    PLAN_SCHEMA,
    normalise_candidate,
    normalise_plan,
    normalise_strategy,
)

WORKSPACE_SCHEMA = "workspace/v1"

_PREFIX = {"candidate": "cand", "plan": "plan", "strategy": "strat"}
_DIRS = {"candidate": "candidate", "plan": "plans", "strategy": "strategies"}


def _dir(ws_root: Path, kind: str) -> Path:
    """某类版本的目录（candidate / plans / strategies）。"""
    return ws_root / _DIRS[kind] / "revisions"


class Workspace:
    """一个用户的数据根。构造不读盘；`open` / `create` 才校验。"""

    def __init__(self, root: Path | str) -> None:
        """只记录路径。"""
        self.root = Path(root).expanduser().resolve()

    # ---------------------------------------------------------------- 身份

    @property
    def meta_path(self) -> Path:
        """`workspace.json` 的完整路径。"""
        return self.root / "workspace.json"

    def exists(self) -> bool:
        """是否已经初始化过（有 `workspace.json`）。"""
        return self.meta_path.is_file()

    def meta(self) -> dict[str, Any]:
        """读工作区元信息；没初始化直接报错，不猜。"""
        if not self.exists():
            raise NotFound(f"这个目录还不是工作区（缺 workspace.json）：{self.root}",
                           next_steps=["先运行 setup 或迁移工具创建工作区"])
        data = read_json(self.meta_path)
        if not isinstance(data, dict):
            raise NotFound("workspace.json 必须是 JSON 对象")
        if data.get("schema") != WORKSPACE_SCHEMA:
            raise Conflict(f"工作区 schema 不认识：{data.get('schema')}",
                           next_steps=["用当前版本程序打开，或先跑迁移工具"])
        return data

    @classmethod
    def create(cls, root: Path | str, name: str = "", *, actor: str = "setup") -> "Workspace":
        """初始化空工作区；已存在就报冲突，避免覆盖别人的数据。"""
        ws = cls(root)
        if ws.exists():
            raise Conflict(f"工作区已经存在：{ws.root}", next_steps=["用 open 打开，不要重复初始化"])
        ws.root.mkdir(parents=True, exist_ok=True)
        write_json(ws.meta_path, {
            "schema": WORKSPACE_SCHEMA,
            "id": new_id("ws"),
            "name": name or ws.root.name,
            "created_at": now_iso(),
            "active_candidate": None,
            "active_plan": None,
            "platform": {"kind": "boss", "account_ref": None},
            "migration": {},
            "legacy": {},
        })
        ws.audit(actor, "workspace.create", str(ws.root), {"name": name})
        return ws

    @classmethod
    def open(cls, root: Path | str) -> "Workspace":
        """打开已存在的工作区。"""
        ws = cls(root)
        ws.meta()
        return ws

    def _update_meta(self, mutate) -> dict[str, Any]:
        """在锁内读-改-写 `workspace.json`，其它进程不会读到中间态。"""
        with self.lock():
            data = self.meta()
            changed = mutate(data) or data
            write_json(self.meta_path, changed)
            return changed

    def audit(self, actor: str, action: str, target: str, detail: dict[str, Any] | None = None) -> None:
        """记一行审计。`actor` 是自报标签，用于回溯，不作为认证依据。"""
        append_jsonl(self.root / "audit" / "audit.jsonl", {
            "at": now_iso(),
            "actor": str(actor or "unknown")[:80],
            "action": action,
            "target": target,
            "detail": detail or {},
        })

    @contextlib.contextmanager
    def lock(self, timeout: float = 8.0) -> Iterator[None]:
        """跨进程小锁；只保护指针与元信息，版本文件本身靠不可变避免竞争。"""
        folder = self.root / ".locks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "workspace.lock"
        deadline = time.monotonic() + timeout
        fd: int | None = None
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > 30:  # 上次进程被杀留下的
                        path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise Conflict("工作区正被另一个操作修改，请稍后重试")
                time.sleep(0.05)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            yield
        finally:
            if fd is not None:
                os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()

    # ---------------------------------------------------------------- 版本

    def _revision_path(self, kind: str, rid: str) -> Path:
        return _dir(self.root, kind) / f"{rid}.json"

    def get(self, kind: str, rid: str) -> dict[str, Any]:
        """按 id 读一个版本；不存在抛 `NotFound`。"""
        if kind not in _PREFIX:
            raise NotFound(f"未知对象类型：{kind}")
        path = self._revision_path(kind, rid)
        if not path.is_file():
            raise NotFound(f"找不到{_label(kind)}：{rid}")
        data = read_json(path)
        if not isinstance(data, dict):
            raise NotFound(f"{_label(kind)}文件损坏：{rid}")
        return data

    def list(self, kind: str) -> list[dict[str, Any]]:
        """列出某类版本，按 revision 从旧到新。"""
        if kind not in _PREFIX:
            raise NotFound(f"未知对象类型：{kind}")
        folder = _dir(self.root, kind)
        if not folder.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in folder.glob("*.json"):
            with contextlib.suppress(ValueError, OSError):
                data = read_json(path)
                if isinstance(data, dict):
                    rows.append(data)
        rows.sort(key=lambda r: (int(r.get("revision") or 0), str(r.get("created_at") or "")))
        return rows

    def _save(self, kind: str, data: dict[str, Any], *, actor: str,
              base: str | None, parent_id: str | None = None) -> dict[str, Any]:
        """写一个新版本。

        `base` 是调用方要求的乐观锁值：给了就必须等于**当前活动版本**，否则报冲突。
        `parent_id` 是新版本的父版本（从哪一版改出来的）；不传就取 `base`。
        """
        if base:
            current = self.active(kind)
            if current is None or current.get("id") != base:
                raise Conflict(f"{_label(kind)}已被别人改过，请刷新后重试",
                               field="expected_revision",
                               detail={"expected": base, "actual": (current or {}).get("id")})
        parent = self.get(kind, parent_id) if parent_id else None
        rid = new_id(_PREFIX[kind])
        data = dict(data)
        data.update({
            "id": rid,
            "revision": int((parent or {}).get("revision") or 0) + 1,
            "parent": (parent or {}).get("id"),
            "created_at": now_iso(),
            "created_by": str(actor or "user")[:80],
        })
        write_json(self._revision_path(kind, rid), data)
        self.audit(actor, f"{kind}.save", rid, {
            "parent": data["parent"], "revision": data["revision"],
            "digest": digest({k: v for k, v in data.items() if k != "created_at"}),
        })
        return data

    def save_candidate(self, raw: dict[str, Any], *, actor: str = "user", base: str | None = None) -> dict[str, Any]:
        """新增一份候选人资料版本，并设为活动资料。"""
        data = self._save("candidate", normalise_candidate(raw), actor=actor, base=base,
                          parent_id=base or (self.active_candidate() or {}).get("id"))
        self._update_meta(lambda m: {**m, "active_candidate": data["id"]})
        return data

    def save_plan(self, raw: dict[str, Any], *, actor: str = "user", base: str | None = None,
                  parent: str | None = None) -> dict[str, Any]:
        """新增一份求职方案版本（草稿），不改活动方案。

        `parent` 用于"从当前方案复制一份"：新方案的 revision 接着父版本往下数。
        """
        return self._save("plan", normalise_plan(raw), actor=actor, base=base,
                          parent_id=parent or base)

    def save_strategy(self, raw: dict[str, Any], *, actor: str = "user") -> dict[str, Any]:
        """新增一份策略版本；登记不等于激活，激活靠方案绑定。"""
        return self._save("strategy", normalise_strategy(raw), actor=actor, base=None)

    # ---------------------------------------------------------------- 活动指针

    def active(self, kind: str) -> dict[str, Any] | None:
        """当前活动版本；没有返回 None。"""
        meta = self.meta()
        key = {"candidate": "active_candidate", "plan": "active_plan", "strategy": "active_strategy"}[kind]
        rid = meta.get(key)
        if not rid:
            return None
        try:
            return self.get(kind, str(rid))
        except NotFound:
            return None

    def active_candidate(self) -> dict[str, Any] | None:
        """活动候选人资料。"""
        return self.active("candidate")

    def active_plan(self) -> dict[str, Any] | None:
        """活动求职方案。"""
        return self.active("plan")

    def activate_plan(self, plan_id: str, *, actor: str = "user", reason: str = "") -> dict[str, Any]:
        """把某份方案设为活动方案；返回生效说明。

        生效边界是"下一个任务"：已经取过快照的任务继续用旧快照，新任务才读新方案。
        """
        plan = self.get("plan", plan_id)
        if plan.get("schema") != PLAN_SCHEMA:
            raise Conflict("方案 schema 不认识，拒绝激活")
        before = self.meta().get("active_plan")
        if before == plan_id:
            return {"changed": False, "active_plan": plan_id, "plan_revision": plan.get("revision"),
                    "effective_from": "next_task", "previous": before}

        def mutate(meta: dict[str, Any]) -> dict[str, Any]:
            current = meta.get("active_plan")
            if current != before:
                raise Conflict("活动方案已被其他操作改过，请刷新后重试",
                               field="expected_revision", detail={"expected": before, "actual": current})
            return {**meta, "active_plan": plan_id, "active_plan_since": now_iso()}

        self._update_meta(mutate)
        self.audit(actor, "plan.activate", plan_id, {"previous": before, "reason": reason})
        return {"changed": True, "active_plan": plan_id, "plan_revision": plan.get("revision"),
                "effective_from": "next_task", "previous": before, "reason": reason}

    def set_active_candidate(self, cand_id: str, *, actor: str = "user", reason: str = "") -> dict[str, Any]:
        """切换活动候选人资料。"""
        cand = self.get("candidate", cand_id)
        before = self.meta().get("active_candidate")
        self._update_meta(lambda m: {**m, "active_candidate": cand_id})
        self.audit(actor, "candidate.activate", cand_id,
                   {"previous": before, "reason": reason, "digest": digest(cand.get("facts") or [])})
        return {"changed": before != cand_id, "active_candidate": cand_id,
                "candidate_revision": cand.get("revision"), "previous": before}

    # ---------------------------------------------------------------- 其它

    def plan_for_task(self, plan_id: str | None = None) -> dict[str, Any]:
        """任务用的方案：显式 id 优先，否则活动方案；没有就报缺配置。"""
        if plan_id:
            return self.get("plan", plan_id)
        plan = self.active_plan()
        if plan is None:
            raise NotFound("还没有活动求职方案",
                           next_steps=["先 create_plan，再 activate_plan"])
        return plan

    def tasks_dir(self) -> Path:
        """外部工作项目录。"""
        return self.root / "tasks"

    def interview_dir(self, sid: str | None = None) -> Path:
        """面试会话目录（给了 id 就是那一个）。"""
        base = self.root / "interviews"
        return base / sid if sid else base

    def objects_dir(self) -> Path:
        """按哈希寻址的附件目录。"""
        return self.root / "objects"

    def raw_resume_path(self, sha: str) -> Path:
        """简历原文路径：按内容哈希命名，重复上传自然去重。"""
        return self.root / "candidate" / "raw" / f"{sha}.txt"

    def audit_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        """最近审计记录（新在前）。"""
        path = self.root / "audit" / "audit.jsonl"
        rows = read_jsonl(path)
        return rows[-limit:][::-1]


def _label(kind: str) -> str:
    return {"candidate": "候选人资料", "plan": "求职方案", "strategy": "策略"}[kind]
