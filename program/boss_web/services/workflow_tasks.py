"""Durable panel tasks. Execution is claimed once; interrupted sends are never retried."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

ACTIONS = {"collect", "match", "prepare", "send", "feedback", "auto_send"}

#: 需要走"逐条正文指纹"的发送类动作。`auto_send` 只是来源不同（由范围授权挑人），
#: 执行链与 `send` 完全一致——不另开一条发送路径。
SEND_ACTIONS = {"send", "auto_send"}

_JID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 执行器是否真的在跑。面板要能回答"到点会不会有人执行"，不能只看计划存不存在。
RUNNER: dict = {"alive": False, "since": 0.0, "tick_seconds": 2}


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() != 87  # Access denied is not proof of exit.
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "runs" / "panel-tasks.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
                due REAL NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                started REAL, finished REAL, result TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0)""")
            columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            for name, kind in (("owner_pid", "INTEGER"), ("child_pid", "INTEGER"),
                               ("idem", "TEXT"), ("schedule_id", "TEXT")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
            # 幂等键在存储层唯一：重试只会命中同一行，不会因为状态流转再建一个任务
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tasks_idem ON tasks(idem) "
                         "WHERE idem IS NOT NULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL,
                every_seconds INTEGER NOT NULL DEFAULT 0,
                at_hhmm TEXT NOT NULL DEFAULT '', weekdays TEXT NOT NULL DEFAULT '',
                catch_up TEXT NOT NULL DEFAULT 'auto',
                next_run REAL NOT NULL, last_run REAL,
                last_result TEXT NOT NULL DEFAULT '', missed INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, updated REAL NOT NULL)""")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def public(row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list(self):
        with self.connect() as conn:
            return [self.public(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created DESC LIMIT 100")]

    def get(self, jid: str):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (jid,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        return self.public(row)

    def add(self, action: str, payload: dict, due: float | None = None, idem: str | None = None,
            jid: str | None = None):
        """排一个任务。`jid` 可以外部指定，让调用方先占额度、再用同一个 id 建任务。"""
        if action not in ACTIONS:
            raise ValueError("不支持的任务类型")
        now = time.time()
        due = now if due is None else float(due)
        from boss_login.account import pin_payload
        payload = pin_payload(action, payload)
        if not math.isfinite(due) or due > now + 366 * 86400 or due < now - 60:
            raise ValueError("执行时间必须在现在至一年以内")
        jid = jid or uuid.uuid4().hex
        if not _JID.fullmatch(str(jid)):
            raise ValueError("任务 id 只能用字母数字")
        with self.connect() as conn:
            conn.execute("INSERT INTO tasks(id,action,payload,due,status,created,idem) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (jid, action, json.dumps(payload, ensure_ascii=False), due, "queued",
                          now, idem))
        return {"id": jid, "status": "queued", "action": action, "due": due}

    def add_idempotent(self, action: str, payload: dict, idem: str):
        """同一幂等键只产生一个任务；键已存在就返回那一行。

        查询和插入在同一个 `BEGIN IMMEDIATE` 事务里，`idem` 上还有唯一索引，
        所以并发重试、或任务已经 done/failed 之后重试，都不会再建第二个任务。

        Returns:
            `(task, created)`；`created=False` 表示命中了已有任务。
        """
        if action not in ACTIONS:
            raise ValueError("不支持的任务类型")
        now = time.time()
        jid = uuid.uuid4().hex
        from boss_login.account import pin_payload
        body = json.dumps(pin_payload(action, payload), ensure_ascii=False)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE idem=?", (idem,)).fetchone()
            if row is not None:
                return self.public(row), False
            conn.execute("INSERT INTO tasks(id,action,payload,due,status,created,idem) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (jid, action, body, now, "queued", now, idem))
        return {"id": jid, "status": "queued", "action": action, "due": now}, True

    def claim(self, now: float | None = None):
        now = time.time() if now is None else now
        expanded: list[dict] = []
        claimed = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for item in conn.execute("SELECT * FROM tasks WHERE status='running'").fetchall():
                if not process_alive(item["owner_pid"]) and not process_alive(item["child_pid"]):
                    conn.execute("UPDATE tasks SET status='needs_review',result=? WHERE id=?",
                                 ("执行进程已退出，结果未完整记录。请先核对运行日志与发送台账，再解除队列暂停；不会自动重试本任务。", item["id"]))
            # 逾期作废的任务要把占着的授权额度退回去，否则"没跑"会一直算在额度里
            # Immediate work can legitimately wait behind a long send batch.
            # Only an explicitly scheduled time has the five-minute missed-slot cutoff.
            for item in conn.execute("SELECT * FROM tasks WHERE status='queued' AND due<? "
                                     "AND (due>created OR schedule_id IS NOT NULL)",
                                     (now - 300,)).fetchall():
                conn.execute("UPDATE tasks SET status='expired',finished=?,result=? WHERE id=?",
                             (now, "已超过计划时间 5 分钟，未执行。请重新安排。", item["id"]))
                expanded.append(self.public(item))
            # One task per workspace, including when more than one app process is open.
            if not conn.execute("SELECT 1 FROM tasks WHERE status IN ('running','needs_review')").fetchone():
                row = conn.execute("SELECT * FROM tasks WHERE status='queued' AND due<=? ORDER BY due,created LIMIT 1",
                                   (now,)).fetchone()
                if row is not None:
                    conn.execute("UPDATE tasks SET status='running',started=?,owner_pid=? WHERE id=?",
                                 (now, os.getpid(), row["id"]))
                    claimed = self.public(row)
        for item in expanded:
            settle_grant(self.root, item, finish=False)
        return claimed

    def acknowledge(self, jid):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (jid,)).fetchone()
            if row is None or row["status"] != "needs_review":
                raise ValueError("仅可确认已中断且待核对的任务")
            if process_alive(row["owner_pid"]) or process_alive(row["child_pid"]):
                raise ValueError("执行进程仍存在，请等待其退出")
            conn.execute("UPDATE tasks SET status='failed',finished=?,result=result || ? WHERE id=?",
                         (time.time(), "\n用户已核对，解除队列暂停；本任务未重试。", jid))
        return {"status": "failed"}

    def finish(self, jid: str, status: str, result: str):
        if status not in {"done", "failed", "cancelled"}:
            raise ValueError("无效结果")
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET status=?,result=?,finished=? WHERE id=? AND status='running'",
                         (status, result[-12000:], time.time(), jid))
        # 停止标记只在任务生命周期内有意义：收尾时清掉，免得 runs/panel-jobs 越堆越多，
        # 也免得以后有人复用同一个任务 id 时被一条旧标记一启动就停掉。
        try:
            (self.root / "runs" / "panel-jobs" / f"{jid}.stop").unlink()
        except OSError:
            pass

    def cancel(self, jid: str):
        dropped = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (jid,)).fetchone()
            if row is None:
                raise ValueError("任务不存在")
            if row["status"] == "queued":
                conn.execute("UPDATE tasks SET status='cancelled',finished=? WHERE id=?", (time.time(), jid))
                dropped = self.public(row)
                result = {"status": "cancelled"}
            elif row["status"] == "running":
                conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (jid,))
                folder = self.root / "runs" / "panel-jobs"
                folder.mkdir(exist_ok=True)
                (folder / f"{jid}.stop").touch()
                result = {"status": "stopping", "message": "已请求停止后续操作；已发出的请求等待结果，不强制终止"}
            else:
                result = {"status": row["status"]}
        if dropped is not None:
            # 取消的发送任务：没执行就不该占额度
            settle_grant(self.root, dropped, finish=False)
        return result

    def stop_grant(self, grant_id: str) -> list:
        """授权暂停/撤销时，把**正在跑**的同授权发送任务请求停止。

        停止只能落在"下一条还没发出的消息"之前：发送子进程每批前看 `.stop`
        文件，已经发出去的请求照旧等结果，最后由台账结算额度。
        以前暂停只改授权状态，子进程毫不知情，会照原计划把这一批发完。
        """
        stopped: list = []
        folder = self.root / "runs" / "panel-jobs"
        folder.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status='running' AND action IN ('send','auto_send')"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except ValueError:
                    continue
                if str((payload or {}).get("grant_id") or "") != grant_id:
                    continue
                conn.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (row["id"],))
                (folder / f"{row['id']}.stop").touch()
                stopped.append(str(row["id"]))
        return stopped

    # ------------------------------------------------------------- 周期计划
    #
    # 周期计划不是"另一个调度器"：它只是一行**计划**，到点由执行器的 2 秒轮询
    # materialize 成一个普通的 `tasks` 行，之后的领取、串行、失败不重试、
    # 额度占用全部复用既有逻辑。`next_run` 是唯一的下次执行依据，落盘。

    def add_schedule(self, action: str, payload: dict, *, kind: str,
                     every_seconds: int = 0, at_hhmm: str = "", weekdays: str = "",
                     catch_up: str = "auto", note: str = "",
                     now: float | None = None) -> dict:
        """排一条周期计划。`kind=interval` 用 `every_seconds`，`kind=daily` 用 `at_hhmm`。"""
        if action not in ACTIONS:
            raise ValueError("不支持的周期任务类型")
        now = time.time() if now is None else now
        kind = str(kind or "").strip()
        if kind not in {"interval", "daily"}:
            raise ValueError("周期类型只支持 interval/daily")
        every = int(every_seconds or 0)
        if kind == "interval" and not 60 <= every <= 7 * 86400:
            raise ValueError("间隔要在 60 秒至 7 天之间")
        at_hhmm = str(at_hhmm or "").strip()
        if kind == "daily":
            parsed = _parse_hhmm(at_hhmm)
            if parsed is None:
                raise ValueError("每天执行时间要写成 HH:MM")
        days = _parse_weekdays(weekdays)
        if catch_up not in {"auto", "once", "skip"}:
            raise ValueError("错过时间后的处理只支持 auto/once/skip")
        spec = {"kind": kind, "at_hhmm": at_hhmm, "weekdays": ",".join(str(x) for x in days),
                "every_seconds": every}
        first = _next_after(spec, now, now)
        sid = f"sch_{uuid.uuid4().hex[:12]}"
        from boss_login.account import pin_payload
        payload = pin_payload(action, payload)
        with self.connect() as conn:
            conn.execute("INSERT INTO schedules(id,action,payload,enabled,kind,every_seconds,"
                         "at_hhmm,weekdays,catch_up,next_run,note,created,updated) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, action, json.dumps(payload, ensure_ascii=False), 1, kind, every,
                          at_hhmm, spec["weekdays"], catch_up, first, str(note or "")[:200],
                          now, now))
        return self.get_schedule(sid)

    def get_schedule(self, sid: str) -> dict:
        """按 id 读周期计划；不存在抛 `ValueError`。"""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (str(sid),)).fetchone()
        if row is None:
            raise ValueError("周期计划不存在")
        return self._public_schedule(row)

    def list_schedules(self) -> list[dict]:
        """全部周期计划，新在前。"""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY created DESC").fetchall()
        return [self._public_schedule(row) for row in rows]

    def set_schedule_enabled(self, sid: str, enabled: bool, *, now: float | None = None) -> dict:
        """启用/停用。重新启用时把 `next_run` 推到未来，避免一开就补跑一串。"""
        now = time.time() if now is None else now
        row = self.get_schedule(sid)
        value = 1 if enabled else 0
        nxt = _next_after(row, now, now) if enabled else float(row["next_run"])
        with self.connect() as conn:
            conn.execute("UPDATE schedules SET enabled=?,next_run=?,updated=? WHERE id=?",
                         (value, nxt, now, str(sid)))
        return self.get_schedule(sid)

    def delete_schedule(self, sid: str) -> dict:
        """删掉周期计划。已经排队的任务不受影响（它们已经是普通任务）。"""
        self.get_schedule(sid)
        with self.connect() as conn:
            conn.execute("DELETE FROM schedules WHERE id=?", (str(sid),))
        return {"deleted": str(sid)}

    def materialize(self, now: float | None = None) -> list[dict]:
        """把到点的周期计划变成普通任务；同一时刻只生成一个，不补跑一串。"""
        now = time.time() if now is None else now
        created: list[dict] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run<=?",
                               (now,)).fetchall()
            for row in rows:
                sched = dict(row)
                due_at = float(row["next_run"])
                nxt, missed = _advance(sched, due_at, now)
                idem = f"sched:{row['id']}:{int(due_at)}"
                catch = str(row["catch_up"] or "auto")
                if catch == "auto":
                    # 发送类错过就不补：防止休眠恢复后一次性发出好几批
                    catch = "skip" if row["action"] in SEND_ACTIONS else "once"
                fired = False
                if catch == "once" and not conn.execute(
                        "SELECT 1 FROM tasks WHERE idem=?", (idem,)).fetchone():
                    task_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO tasks(id,action,payload,due,status,created,idem,"
                                 "schedule_id) VALUES(?,?,?,?,?,?,?,?)",
                                 (task_id, row["action"], row["payload"], now, "queued", now,
                                  idem, row["id"]))
                    created.append({"id": task_id, "action": row["action"],
                                    "schedule_id": row["id"]})
                    fired = True
                conn.execute("UPDATE schedules SET next_run=?,last_run=?,missed=missed+?,"
                             "updated=? WHERE id=?",
                             (nxt, now if fired else row["last_run"], missed, now, row["id"]))
        return created

    def note_schedule_result(self, sid: str, status: str, result: str,
                             *, now: float | None = None) -> None:
        """把这一次的执行结果写回计划，供面板/AI 核验"到底跑没跑成"。"""
        now = time.time() if now is None else now
        head = str(result or "").strip().splitlines()
        text = f"{status}｜{(head[0] if head else '')}"[:240]
        with self.connect() as conn:
            conn.execute("UPDATE schedules SET last_result=?,updated=? WHERE id=?",
                         (text, now, str(sid)))

    @staticmethod
    def _public_schedule(row) -> dict:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["enabled"] = bool(item["enabled"])
        return item


def _parse_hhmm(text: str):
    """`HH:MM` → (时, 分)；格式不对返回 None。"""
    raw = str(text or "").strip()
    if len(raw) != 5 or raw[2] != ":":
        return None
    head, tail = raw[:2], raw[3:]
    if not (head.isdigit() and tail.isdigit()):
        return None
    hour, minute = int(head), int(tail)
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _parse_weekdays(text: str) -> list[int]:
    """`"1,2,3"` → [1,2,3]（1=周一…7=周日）；空 = 每天。"""
    out: list[int] = []
    for part in str(text or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= 7:
            raise ValueError("星期几只能写 1–7（1=周一）；空表示每天")
        if int(part) not in out:
            out.append(int(part))
    return sorted(out)


def _next_after(spec: dict, after: float, now: float) -> float:
    """`spec` 描述的计划在 `after` 之后的下一个时刻（不受 `now` 影响）。"""
    kind = str(spec.get("kind") or "")
    if kind == "interval":
        every = max(60, int(spec.get("every_seconds") or 60))
        return after + every
    if kind != "daily":
        raise ValueError("周期类型只支持 interval/daily")
    parsed = _parse_hhmm(str(spec.get("at_hhmm") or ""))
    if parsed is None:
        raise ValueError("每天执行时间要写成 HH:MM")
    hour, minute = parsed
    days = _parse_weekdays(str(spec.get("weekdays") or ""))
    base = time.localtime(after)
    for offset in range(0, 400):
        stamp = time.mktime((base.tm_year, base.tm_mon, base.tm_mday + offset,
                             hour, minute, 0, 0, 0, -1))
        if days and (time.localtime(stamp).tm_wday + 1) not in days:
            continue
        if stamp > after:
            return stamp
    raise ValueError("算不出下一次执行时间")


def _advance(sched: dict, due_at: float, now: float) -> tuple[float, int]:
    """从 `due_at` 往后推进到 `now` 之后，返回 `(下一次, 错过次数)`。

    间隔类用除法一次算完，不循环；每天类最多看 400 天。
    """
    spec = {"kind": sched.get("kind"), "at_hhmm": sched.get("at_hhmm"),
            "weekdays": sched.get("weekdays"), "every_seconds": sched.get("every_seconds")}
    if str(spec["kind"]) == "interval":
        every = max(60, int(spec.get("every_seconds") or 60))
        missed = int(max(0.0, now - due_at) // every)
        return due_at + (missed + 1) * every, missed
    cur, missed = float(due_at), 0
    for _ in range(400):
        cur = _next_after(spec, cur, now)
        if cur > now:
            return cur, missed
        missed += 1
    return cur, missed


def build_command(code: Path, root: Path, job: dict):
    """Only fixed executable/argument templates; no shell commands from the browser."""
    action, payload = job["action"], job["payload"]
    folder = root / "runs" / "panel-jobs"
    folder.mkdir(parents=True, exist_ok=True)
    request = folder / f"{job['id']}.json"
    request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stop = folder / f"{job['id']}.stop"
    if action in {"prepare", "send", "auto_send"}:
        cmd = [sys.executable, "-B", str(code / "deliver_round_v2.py"), "--round", "manual", "--no-refill",
               "--stop-file", str(stop), "--expected-profile", payload["profile_version"]]
        if action == "prepare":
            only = [str(x) for x in (payload.get("only") or []) if str(x).strip()]
            cmd += ["--prepare-only", "--max-prepare", str(len(only) or payload["limit"])]
            if only:
                cmd += ["--only", ",".join(only)]
        else:
            cmd += ["--send-ready-only", "--approved-file", str(request), "--only", ",".join(payload["approved"]),
                    "--cap", str(len(payload["approved"]))]
        # 授权窗口的结束时间交给子进程：到点自己停手，不用等面板下一次操作。
        deadline = float(payload.get("grant_deadline") or 0)
        if deadline > 0:
            cmd += ["--grant-deadline", f"{deadline:.0f}"]
        return cmd
    if action in {"collect", "match", "feedback"}:
        return [sys.executable, "-B", str(code / "panel_action.py"), "--action", action,
                "--request", str(request), "--stop-file", str(stop)]
    raise ValueError("不支持的任务类型")


def _profile_version(root: Path) -> str:
    from .workflow_context import effective_profile
    return str(effective_profile(Path(root)).get("version") or "")


def _evaluated(root: Path, job: dict) -> dict:
    """发送任务在执行前过一遍授权。

    两种来源：
    * 周期计划派生的（`payload.evaluate`）：到点才挑人、才占额度；
    * 手动按授权发起的：建任务时已经挑好并占了额度，但**仍要复核一次授权层**——
      建任务到真正执行之间用户可能按了暂停或改了方案，"暂停在发送前生效"必须落在
      执行这一刻，而不是建任务那一刻。
    """
    if job.get("action") != "auto_send":
        return job
    payload = job.get("payload") or {}
    grant_id = str(payload.get("grant_id") or "")
    if not grant_id:
        return job

    from . import auto_send

    if payload.get("evaluate"):
        plan = auto_send.prepare(root, grant_id, task_id=str(job["id"]))
        payload = {**payload, "approved": plan["approved"],
                   "limit": len(plan["approved"]), "profile_version": _profile_version(root)}
        job = {**job, "payload": payload}

    blocking, texts = auto_send.verify(root, grant_id)
    if blocking:
        raise RuntimeError("授权在执行前已失效，本轮不发送：" + "；".join(texts))
    payload = {**payload, "grant_deadline": _grant_deadline(root, grant_id)}
    return {**job, "payload": payload}


def _grant_deadline(root: Path, grant_id: str) -> float:
    """授权窗口的结束时间（epoch 秒）；没有窗口就是 0，子进程据此不设到期。"""
    try:
        from . import authz

        grant = authz.store(root).get(grant_id)
        end = ((grant.get("spec") or {}).get("window") or {}).get("end")
        return float(end) if end is not None else 0.0
    except Exception:  # noqa: BLE001 - 读不到就不设到期；执行前的 verify 已经拦过
        import logging

        logging.getLogger(__name__).warning("读授权窗口失败：%s", grant_id, exc_info=True)
        return 0.0


def execute(code: Path, store: TaskStore, job: dict):
    pinned = (job.get('payload') or {}).get('_account_binding') or {}
    if job.get('action') in {'collect', 'send', 'auto_send', 'feedback'}:
        from boss_login.account import current_account
        current = current_account()
        if not pinned.get('sender_account_id') or any(pinned.get(k) != current.get(k) for k in ('sender_account_id', 'session_version')):
            return 'failed', '任务绑定账号或会话已变化（或历史任务未绑定账号）；未访问平台，请确认后重新创建任务'
    job = _evaluated(store.root, job)
    cmd = build_command(code, store.root, job)
    env = dict(os.environ, BOSS_WORKFLOW_DIR=str(store.root), BOSS_WORKFLOW_CODE=str(code),
               BOSS_APP_SOURCE=str(Path(__file__).resolve().parents[2]), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update(BOSS_TASK_ACCOUNT_ID=str(pinned.get('sender_account_id') or ''),
               BOSS_TASK_SESSION_VERSION=str(pinned.get('session_version') or ''))
    # Store output privately and read only its tail; no unbounded PIPE buffering.
    log = store.root / "runs" / "panel-jobs" / f"{job['id']}.log"
    with log.open("w", encoding="utf-8") as output:
        proc = subprocess.Popen(cmd, cwd=str(code), env=env, stdout=output, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        with store.connect() as conn:
            conn.execute("UPDATE tasks SET child_pid=? WHERE id=?", (proc.pid, job["id"]))
        rc = proc.wait()
    with log.open("rb") as output:
        output.seek(max(0, log.stat().st_size - 12000))
        result = output.read().decode("utf-8", errors="replace")
    return ("done" if rc == 0 else "cancelled" if rc == 8 else "failed"), f"退出码 {rc}\n{result}"


def settle_grant(root: Path, job: dict, *, finish: bool) -> None:
    """把这条任务占掉的授权额度结算掉。

    `finish=False`（被取消、没执行）就直接退回；`finish=True` 按台账判实际结果。
    任何异常都只记日志：结算失败不该把任务结果改坏，额度会继续占着等人工核对。
    """
    payload = job.get("payload") or {}
    gid = str(payload.get("grant_id") or "")
    if not gid:
        return
    jobs = [str(x) for x in (payload.get("approved") or {}) if str(x)]
    try:
        from . import authz

        if finish:
            authz.settle_from_queue(root, str(job.get("id") or ""), jobs)
        else:
            authz.store(root).release_task(str(job.get("id") or ""))
    except Exception:  # noqa: BLE001 - 结算异常不影响任务结论
        import logging

        logging.getLogger(__name__).warning("授权额度结算失败：%s", gid, exc_info=True)


class Runner:
    def __init__(self, resolve_paths, executor=execute, clock=time.time,
                 sweep_interval=600):
        self.resolve_paths = resolve_paths
        self.executor = executor
        # 时钟可注入：周期计划的"下一次执行"必须能用模拟时钟验证，不能只靠等
        self.clock = clock
        #: 采集收益自动采样间隔（秒）；0 = 关掉。只算指标、必要时落事件，
        #: 不发任何消息，和"定时发送"没有关系。
        self.sweep_interval = float(sweep_interval)
        self._last_sweep = 0.0
        self.stop_event = threading.Event()
        self.thread = None

    def _sweep(self, root: Path, now: float) -> None:
        """到点算一次采集收益，低新增就落事件。

        不依赖采集器在跑：采集器可以停，收益判断照样要能算出来（算不出来也只能
        说"没有数据"，不能假装有）。异常不拖垮执行器。
        """
        if self.sweep_interval <= 0 or now - self._last_sweep < self.sweep_interval:
            return
        self._last_sweep = now
        try:
            from . import collection_yield

            collection_yield.sweep(root)
        except Exception:  # noqa: BLE001 - 采样失败不影响任务执行
            import logging

            logging.getLogger(__name__).warning("采集收益自动采样失败", exc_info=True)

    def tick(self, now: float | None = None):
        now = self.clock() if now is None else now
        root, code = self.resolve_paths()
        if root is None or not (
                (root / "workspace.json").is_file() or (root / "config" / "workflow.json").is_file()):
            # 新工作区没有 config/workflow.json 也算就绪：执行参数从方案快照取
            return
        # 采样只要状态目录，所以放在"代码目录在不在"之前：没代码也要能看见收益
        self._sweep(root, now)
        if code is None:
            return
        store = TaskStore(root)
        try:
            store.materialize(now)
        except Exception:  # noqa: BLE001 - 计划算不出来不能拖垮整个执行器
            import logging
            logging.getLogger(__name__).warning("周期计划到点处理失败", exc_info=True)
        job = store.claim(now)
        if job is None:
            return
        try:
            status, result = self.executor(code, store, job)
        except Exception as exc:
            status, result = "failed", f"任务执行失败：{type(exc).__name__}: {exc}"
        store.finish(job["id"], status, result)
        if job.get("schedule_id"):
            store.note_schedule_result(str(job["schedule_id"]), status, result, now=now)
        settle_grant(store.root, job, finish=True)
        # 任务改动了台账/数据库：面板的「待发清单」缓存必须作废，否则刷新看到的还是旧数
        try:
            from ..api.workflow import clear_report_cache
            clear_report_cache()
        except Exception:  # noqa: BLE001 - 缓存清不掉不影响任务结果
            import logging
            logging.getLogger(__name__).warning("清理 sendable 缓存失败", exc_info=True)

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    # The API exposes unreadable state; do not delete/reset a damaged queue.
                    import logging
                    logging.getLogger(__name__).exception("面板执行器暂不可用")
                self.stop_event.wait(2)
        self.thread = threading.Thread(target=loop, name="workflow-panel", daemon=True)
        self.thread.start()
        RUNNER.update(alive=True, since=time.time())

    def stop(self):
        # Do not kill a sending subprocess: its ledger owns unknown/outstanding outcomes.
        self.stop_event.set()
        RUNNER.update(alive=False)
