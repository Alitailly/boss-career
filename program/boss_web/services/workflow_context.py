"""把"有效方案 + 策略"接进程序内的执行阶段（匹配、写稿重生成）。

程序跑在应用进程里，没有 `workflow_env` 那条 `sys.path` 链，所以这里自己解析工作区：
`BOSS_WORKFLOW_DIR` 环境变量 → 应用里同一套默认回落。取不到就返回 `None`，
调用方用**内置模板**（不是某个人的默认值）并记为 `builtin`，不静默套用个人规则。

快照按几秒缓存：一个 80 岗位的批次不该读 80 次方案文件；改配置后最多等几秒生效。
"""

from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Any

from boss_core import snapshot as snapshot_mod
from boss_core import strategies as strategies_mod
from boss_core.errors import CoreError
from boss_core.workspace import Workspace

_CACHE: dict[str, Any] = {"snap": None, "at": 0.0, "fp": None}
_TTL = 5.0


def _root() -> Path | None:
    """工作区目录：只认 `BOSS_WORKFLOW_DIR`，不再回落到任何本机固定路径。

    这是 T15 的要求：换用户/换机器时不能悄悄读到别人的数据目录。没设置就没有
    工作区，调用方用内置模板并明确标记来源。
    """
    raw = os.environ.get("BOSS_WORKFLOW_DIR")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def invalidate() -> None:
    """丢掉缓存（配置保存后调用，让下一次执行立即用新值）。"""
    _CACHE.update({"snap": None, "at": 0.0, "fp": None})


def effective_profile(root: Path) -> dict[str, Any]:
    """Execution and admission use the plan-bound candidate, without legacy mirrors."""
    ws = Workspace(root)
    if ws.exists() and ws.active_plan() is not None:
        from boss_core.compat import delivery_profile

        return delivery_profile(snapshot_mod.build(ws))
    path = root / "greeting" / "profile.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("profile.json 必须是 JSON 对象")
    return value


def snapshot() -> dict[str, Any] | None:
    """当前有效快照；没有工作区/没活动方案时返回 None。"""
    now = time.monotonic()
    if _CACHE["snap"] is not None and now - float(_CACHE["at"]) < _TTL:
        return _CACHE["snap"]
    root = _root()
    if root is None:
        return None
    ws = Workspace(root)
    if not ws.exists():
        return None
    try:
        snap = snapshot_mod.build(ws)
    except CoreError:
        return None
    _CACHE.update({"snap": snap, "at": now})
    return snap


def stage(stage_name: str) -> dict[str, Any]:
    """某阶段解析后的策略；没绑定时返回内置模板。"""
    snap = snapshot()
    if snap is None:
        return {"source": "builtin", "id": None, "content": strategies_mod.builtin(stage_name),
                "params": {}}
    return strategies_mod.resolve(snap, stage_name)


def render(stage_name: str, key: str, extra: dict[str, Any] | None = None) -> str:
    """渲染策略里的提示词片段（如 `matching.stage1.system`）；缺变量抛 `MissingConfiguration`。

    `extra` 用来补当次调用的变量（简历原文、目标岗位块等），不污染全局快照。
    """
    content = stage(stage_name).get("content") or {}
    template: Any = content
    for part in key.split("."):
        template = template.get(part) if isinstance(template, dict) else None
    if not template:
        builtin: Any = strategies_mod.builtin(stage_name)
        for part in key.split("."):
            builtin = builtin.get(part) if isinstance(builtin, dict) else None
        template = builtin or ""
    snap = snapshot()
    # 没有工作区/没有方案时也用 `general` 预设补齐变量：内置模板是方向中性的，
    # 不该因为"读不到方案"就退回某个人的提示词（见 boss_core.templates）。
    ctx = dict(strategies_mod.context(snap))
    ctx.update(extra or {})
    return strategies_mod.render(str(template), ctx)


def limits() -> dict[str, Any]:
    """输入装配参数：JD 截断上限、初筛批量。"""
    content = stage("matching").get("content") or {}
    stage1 = content.get("stage1") or {}
    stage2 = content.get("stage2") or {}
    evaluation = content.get("evaluation") or {}
    return {
        "stage1_jd_limit": int(stage1.get("jd_limit") or 1200),  # 仅旧执行辅助函数使用
        "stage1_batch": int(evaluation.get("batch") or stage1.get("batch") or 6),
        "input_char_budget": int(evaluation.get("input_char_budget") or 24000),
        "stage2_jd_limit": int(evaluation.get("jd_limit") or stage2.get("jd_limit") or 8000),
        "stage2_head_ratio": float(stage2.get("head_ratio") or 0.6),
        "stage2_max_tokens": int(evaluation.get("max_tokens") or stage2.get("max_tokens") or 4096),
    }


def stage2_min_score(fallback: int = 75) -> int:
    """详评门槛：策略里写了就按策略；没写用调用方给的值。"""
    content = stage("matching").get("content") or {}
    value = ((content.get("thresholds") or {}).get("stage2_min_score"))
    return int(value) if isinstance(value, (int, float)) else int(fallback)


def admit_min_score(fallback: int = 0) -> int:
    """准入门槛：来自方案的运行偏好。"""
    snap = snapshot()
    if not snap:
        return int(fallback)
    return int((snap.get("thresholds") or {}).get("admit_min_score") or fallback)


def candidate_brief(resume: Any, llm_parse: dict[str, Any] | None) -> str | None:
    """候选人摘要：优先用有效快照（已确认事实 + 原文证据）。

    只在这份简历就是活动候选人时用快照，避免"按 A 简历打分、按 B 简历投递"。
    """
    snap = snapshot()
    if not snap:
        return None
    candidate = snap.get("candidate") or {}
    active_id = (candidate.get("resume") or {}).get("resume_id")
    if active_id and getattr(resume, "resume_id", None) and active_id != resume.resume_id:
        return None
    facts = candidate.get("facts") or []
    if not facts:
        return None
    identity = []
    for item in candidate.get("education") or []:
        identity.append("教育：" + "；".join(str(item.get(k) or "") for k in
                                          ("degree", "major", "start", "end") if item.get(k)))
    for item in candidate.get("experiences") or []:
        identity.append("经历：" + "；".join(str(item.get(k) or "") for k in
                                          ("org", "title", "start", "end") if item.get(k)))
    lines = ["# 已确认事实（只能当依据，不能改写）"]
    lines += [f"{f.get('id')}: {f.get('text')}" for f in facts]
    if identity:
        lines.append("# 结构化资料")
        lines += identity
    return "\n".join(lines)


def greeting_rules() -> dict[str, Any] | None:
    """合成后的写作规则（面板重生成、/api/ai/greeting-rules 共用同一份）。

    面板「系统配置 → 招呼语」里那段也加进来：它是用户自己写的可见规则，
    以前只出现在某个通道里，导致"改了没效果"。
    """
    snap = snapshot()
    if not snap:
        return None
    from boss_core import greeting

    try:
        from .greeting_prompt import resolve_greeting_requirement

        panel_rule = resolve_greeting_requirement()
    except Exception:  # noqa: BLE001 - 规则库读不到时只合成方案里的部分
        panel_rule = ""
    return greeting.compose(snap, panel_rule=panel_rule)
