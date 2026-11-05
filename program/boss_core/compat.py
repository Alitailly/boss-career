"""给旧脚本用的兼容层：把新快照翻译成它们原来读的那几种形状。

`deliver_round_v2.py`、`collector.py`、`panel_action.py` 以前直接读
`greeting/profile.json`、`greeting/policy.json`、`config/workflow.json`。
现在它们改读这里，拿到的是**同一份有效快照**，但字段名保持兼容，避免一次性重写 2000 行。

没有工作区（还没迁移）时返回 `None`，由调用方明确报"未配置"，不悄悄用个人默认值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import conditions, greeting, snapshot as snapshot_mod
from .errors import MissingConfiguration
from .jsonio import digest
from .workspace import Workspace

#: 复用快照的数值转换规则：缺失/None 才用默认值，显式 0 保持 0。
#: 这里不再各写一遍 `or 默认值`，否则 0 会在兼容层被第二次改回默认值。
_num = snapshot_mod._num


def open_workspace(root: Path | str) -> Workspace | None:
    """工作区存在就打开，否则返回 None。"""
    ws = Workspace(root)
    return ws if ws.exists() else None


def load(root: Path | str) -> dict[str, Any] | None:
    """取当前有效快照；没有工作区/没有活动方案时返回 None。"""
    ws = open_workspace(root)
    if ws is None:
        return None
    try:
        return snapshot_mod.build(ws)
    except MissingConfiguration:
        return None


def candidate_facts(snap: dict[str, Any]) -> list[tuple[str, str]]:
    """`[(id, text)]`，顺序与旧 `facts()` 一致。"""
    return [(str(f.get("id")), str(f.get("text"))) for f in snap.get("candidate", {}).get("facts") or []]


def delivery_profile(snap: dict[str, Any]) -> dict[str, Any]:
    """旧 `load_profile()` 的形状：`{resume_id, facts, version, active_for_workflow}`。

    版本号优先沿用迁移前的 `profile-*`，这样历史草稿不会因为"换了存储"被判失效；
    真改了事实就会生成新候选人版本、版本号随之变化。
    """
    candidate = snap.get("candidate") or {}
    legacy = candidate.get("legacy") or {}
    version = legacy.get("profile_version") or candidate.get("id")
    return {
        "resume_id": (candidate.get("resume") or {}).get("resume_id"),
        "source_sha": (candidate.get("resume") or {}).get("source_sha"),
        "facts": [{"id": fid, "text": text} for fid, text in candidate_facts(snap)],
        "version": version,
        "active_for_workflow": True,
        "candidate_revision": candidate.get("id"),
    }


def delivery_policy(snap: dict[str, Any]) -> dict[str, Any]:
    """旧 `load_policy()` 的形状，内容来自当前绑定的写作策略。"""
    strategy = (snap.get("strategies") or {}).get("greeting") or {}
    content = strategy.get("content") or {}
    legacy = strategy.get("legacy") or {}
    length = content.get("length") or {}
    return {
        "version": legacy.get("policy_version") or strategy.get("id") or "policy-builtin",
        "max_len": int(length.get("max_chars") or 200),
        "min_len": int(length.get("min_chars") or 1),
        "requirements": str(content.get("style") or ""),
        "preamble": str(content.get("preamble") or ""),
        "forbidden": list(content.get("forbidden") or greeting.BANNED),
        "head_dedup": bool(content.get("head_dedup")),
        "input": dict(content.get("limits") or {}),
        "selection_version": str(content.get("selection_version") or "selection-legacy"),
        "strategy_id": strategy.get("id"),
        "source": strategy.get("source") or "builtin",
    }


def delivery_config(snap: dict[str, Any]) -> dict[str, Any]:
    """旧 `load_config()` 里被读的键，来源改为方案运行偏好。"""
    runtime = snap.get("runtime") or {}
    plan = snap.get("plan") or {}
    salary = plan.get("salary") or {}
    return {
        "version": f"plan:{plan.get('id')}#{plan.get('revision')}",
        "resume_id": (snap.get("candidate", {}).get("resume") or {}).get("resume_id"),
        "min_score": int(runtime.get("min_score") or 0),
        "daily_cap": int(runtime.get("daily_cap") or 0),
        "activity": dict(runtime.get("activity") or {}),
        "salary_gate": {
            "daily_min": salary.get("min") if salary.get("period") == "day" else None,
            "daily_max": salary.get("max") if salary.get("period") == "day" else None,
        },
        "pricing": dict(runtime.get("pricing") or {}),
        # chunk / batch_size 的 0 在下游是"参数非法"（投递脚本要求 chunk≥1、batch 1–15），
        # 按未配置回退默认值；chunk_gap 的 0 是有效的"不等待"。
        "chunk": int(runtime.get("chunk") or 5),
        "chunk_gap": _num(runtime, "chunk_gap", 180),
        "batch_size": int(runtime.get("batch_size") or 6),
        "max_delivery_attempts": int(runtime.get("max_delivery_attempts") or 2),
        "bridge_timeout_sec": int(runtime.get("bridge_timeout_sec") or 540),
        "rounds": dict(runtime.get("rounds") or {}),
    }


def preferences_version(snap: dict[str, Any]) -> str:
    """到岗口径版本号：内容与迁移时一致就沿用旧版本号，真改了才变。

    投递脚本用它判断"旧招呼语里关于到岗时间的说法还算不算数"；外部 AI 写回草稿时
    也必须用同一个版本号，否则发送前复核会把刚写好的稿判成 `stale_prefs`。
    只此一处实现，两边都调它。
    """
    plan = snap.get("plan") or {}
    legacy = plan.get("legacy") or {}
    avail = plan.get("availability") or {}
    body = {
        "start": avail.get("start"),
        "days_per_week": avail.get("days_per_week"),
        "months": avail.get("months"),
        "cities": (plan.get("target") or {}).get("cities"),
        "remote": (plan.get("target") or {}).get("accepts_relocation"),
    }
    current = digest(body)
    if legacy.get("prefs_version") and legacy.get("prefs_digest") == current:
        return str(legacy["prefs_version"])
    return f"prefs-{current}"


def prefilter_reason(job: dict[str, Any], snap: dict[str, Any] | None) -> str:
    """岗位硬冲突原因。

    只有**确定不合格**才拦；`unknown`（岗位没写学历/薪资认不出/资料没填）一律放行，
    交给模型和目标流程判断——旧代码的注释也是这个原则："拿不准的一律放行"。
    """
    if snap is None:
        raise MissingConfiguration("还没有有效求职方案（workspace.json）",
                                   next_steps=["运行迁移工具或先创建方案"])
    verdict = conditions.judge(job, snap)
    if verdict["result"] != "fail":
        return ""
    return verdict["summary"]


def search_config(snap: dict[str, Any]) -> dict[str, Any]:
    """旧 `config/search.json` 的形状，内容由方案编译而来。

    键名保持兼容，采集器不用改读法；`filters_hash` 用来判断"城市/关键词变了没有"，
    变了就清空翻页游标，避免拿旧城市的页码继续翻。
    """
    compiled = snap.get("search") or {}
    plan = snap.get("plan") or {}
    return {
        "version": f"search:{plan.get('id')}#{plan.get('revision')}",
        "base": dict(compiled.get("base") or {}),
        "bases": [dict(b) for b in compiled.get("bases") or [compiled.get("base") or {}]],
        "queries": list(compiled.get("keywords") or []),
        "pages_per_query": _num(compiled, "pages_per_query", 3),
        "pages": {"max_pages_per_query": _num(compiled, "max_pages_per_query", 20)},
        "daily_page_cap": _num(compiled, "daily_page_cap", 300),
        "cycle_sleep_sec": _num(compiled, "cycle_sleep_sec", 180),
        "jd_batch": _num(compiled, "jd_batch", 5),
        "jd_interval": _num(compiled, "jd_interval", 5.0, float),
        "daily_jd_cap": _num(compiled, "daily_jd_cap", 1500),
        "match_batch": _num(compiled, "match_batch", 80),
        "match_force_backlog": _num(compiled, "match_force_backlog", 60),
        "activity_batch": _num(compiled, "activity_batch", 4),
        "activity_min_score": _num(compiled, "activity_min_score", 0),
        "activity_interval": _num(compiled, "activity_interval", 8.0, float),
        "corpus_export_every": _num(compiled, "corpus_export_every", 40),
        "filters_hash": str(compiled.get("filters_hash") or ""),
    }


def active_resume_id(snap: dict[str, Any] | None) -> str:
    """投递/匹配用的简历 id：不再是两个文件二选一。"""
    if not snap:
        return ""
    return str((snap.get("candidate", {}).get("resume") or {}).get("resume_id") or "")
