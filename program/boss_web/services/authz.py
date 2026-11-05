"""把工作区现状翻译成"规则范围授权"判定需要的那几样东西。

这里只做翻译与对账，不做判断：判定在 `boss_core.authorization` 里，发送在既有的
`deliver_round_v2 --send-ready-only` 里。这样"能改哪、算不算超范围、发不发"三件事
各只有一处实现。
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

from boss_core.authorization import (DEFAULT_LOCKED, GrantStore, fact_fingerprints,
                                     lock_fields, reason_text)
from boss_core.jsonio import digest, read_json, read_jsonl
from boss_core.workspace import Workspace

#: 台账里的发送事件 → 授权额度口径。
#: `skipped`/`voided` 表示"这一条没真的发出去"，额度要退回；其余都算占用。
_OUTCOME = {"sent": "sent", "unknown": "unknown", "failed": "failed",
            "skipped": "released", "voided": "released"}


def store(root: Path) -> GrantStore:
    """这个工作区的授权库。"""
    return GrantStore(root)


def basis(root: Path) -> dict[str, Any]:
    """当前活动资料/方案/策略的版本指纹——授权的绑定对象。"""
    ws = Workspace(root)
    if not ws.exists():
        return {"candidate": {"id": "", "candidate_facts": {}},
                "plan": {"id": "", "locked_digest": ""},
                "strategies": {"matching": "", "greeting": ""},
                "profile_version": ""}
    plan = ws.active_plan() or {}
    candidate = ws.get("candidate", plan["candidate_id"]) if plan.get("candidate_id") else ws.active_candidate() or {}
    bound = plan.get("strategies") or {}
    locked = lock_fields(plan, DEFAULT_LOCKED) if plan else {}
    return {
        "candidate": {"id": str(candidate.get("id") or ""),
                      "revision": int(candidate.get("revision") or 0),
                      "candidate_facts": fact_fingerprints(candidate.get("facts") or [])},
        "plan": {"id": str(plan.get("id") or ""),
                 "revision": int(plan.get("revision") or 0),
                 # 逐字段存值：判定时能说清"哪一项变了"，而不是只给一个总指纹
                 "locked_values": locked,
                 "locked_digest": digest(locked) if locked else ""},
        "strategies": {stage: _strategy_ref(ws, bound.get(stage))
                       for stage in ("matching", "greeting")},
        "profile_version": str(_profile(root).get("version") or ""),
    }


def _strategy_ref(ws: Workspace, sid: Any) -> str:
    """`<id>@<revision>`；没绑定就是空串（= 用内置默认）。"""
    text = str(sid or "").strip()
    if not text:
        return ""
    with contextlib.suppress(Exception):
        row = ws.get("strategy", text)
        return f"{row.get('id')}@{row.get('revision')}"
    return text


def _profile(root: Path) -> dict[str, Any]:
    from .workflow_context import effective_profile
    return effective_profile(root)


def labels(root: Path, *, goal: str = "") -> dict[str, Any]:
    """预览文本里用到的可读名字：工作区、方案、简历。"""
    ws = Workspace(root)
    name = ws.root.name
    plan_label = ""
    candidate_label = ""
    if ws.exists():
        with contextlib.suppress(Exception):
            meta = ws.meta()
            name = str(meta.get("name") or name)
        with contextlib.suppress(Exception):
            plan_label = str((ws.active_plan() or {}).get("label") or "")
        with contextlib.suppress(Exception):
            plan = ws.active_plan() or {}
            candidate = ws.get("candidate", plan["candidate_id"]) if plan.get("candidate_id") else ws.active_candidate() or {}
            candidate_label = str(candidate.get("label") or "")
    return {"workspace_name": name, "plan_label": plan_label,
            "candidate_label": candidate_label, "goal": goal}


def default_spec(root: Path) -> dict[str, Any]:
    """给当前工作区预填一份保守授权参数：绑定现在的资料/方案，额度小、期限短。"""
    snap = basis(root)
    plan = Workspace(root).active_plan() or {} if Workspace(root).exists() else {}
    min_score = int(((plan.get("runtime") or {}).get("min_score")) or 70)
    daily = int(((plan.get("runtime") or {}).get("daily_cap")) or 5)
    return {
        "basis": {
            "candidate_id": snap["candidate"]["id"],
            "candidate_revision": snap["candidate"]["revision"],
            "candidate_facts": snap["candidate"]["candidate_facts"],
            "plan_id": snap["plan"]["id"], "plan_revision": snap["plan"]["revision"],
            "locked_plan_fields": list(DEFAULT_LOCKED),
            "locked_values": dict(snap["plan"]["locked_values"]),
            "locked_digest": snap["plan"]["locked_digest"],
            "strategies": dict(snap["strategies"]),
            "profile_version": snap["profile_version"],
        },
        "admission": {"min_score": min_score},
        "quota": {"total": max(1, daily), "per_day": max(1, min(daily, 5))},
    }


def jobs_from_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 `/sendable` 的行翻译成判定用的岗位。

    能出现在 `rows` 里，就意味着它已经过了"有正文、够分、招呼语通过"这三关
    （`deliver_round_v2.ready_jobs` 的同一套判断），所以这里可以标成就绪；
    真正的正文指纹用 `review_hash`，与发出去时校验的是同一个。
    """
    out: list[dict[str, Any]] = []
    for row in rows or []:
        jid = str(row.get("job_id") or "")
        if not jid:
            continue
        out.append({
            "job_id": jid,
            "score": row.get("score"),
            "company": str(row.get("company") or ""),
            "title": str(row.get("title") or ""),
            "has_jd": bool(row.get("chars")),
            "greeting_ready": bool(row.get("review_hash")),
            "body_sha": str(row.get("review_hash") or ""),
        })
    return out


def settle_from_queue(root: Path, task_id: str, job_ids: Iterable[str], *,
                      now: float | None = None) -> int:
    """任务结束后，按台账把每条的实际结果写回授权用量。

    只认台账（发送的唯一证据）：有 `sent` 就是发出去了，`failed` 算试过，
    `skipped`/`voided` 退回额度；一条事件都没有说明根本没走到发送，也退回。
    结果未明的保持 `unknown`，继续占额度——宁可少发，不重复扣或少扣。
    """
    wanted = [str(x) for x in job_ids or [] if str(x)]
    if not wanted:
        return 0
    last: dict[str, str] = {}
    with contextlib.suppress(Exception):
        for row in read_jsonl(root / "greeting" / "queue.jsonl"):
            event = str(row.get("event") or "")
            jid = str(row.get("job_id") or "")
            if jid and event in _OUTCOME:
                last[jid] = event
    outcomes: dict[str, str] = {}
    for jid in wanted:
        events = last.get(jid)
        outcomes[jid] = _OUTCOME.get(events, "released") if events else "released"
    return store(root).settle(str(task_id), outcomes, now=now)


def summary(root: Path) -> dict[str, Any]:
    """面板/AI 都能读的一份授权现状。"""
    grants = store(root)
    active = grants.active()
    items = grants.list(20)
    payload: dict[str, Any] = {"ok": True, "active": None, "items": items,
                               "usage": None, "text": "", "can_run": False,
                               "blockers": []}
    if active:
        usage = grants.usage(active["id"])
        payload.update({"active": active, "usage": usage, "text": active["preview"],
                        "can_run": active["status"] == "active" and usage["left_total"] > 0
                        and usage["left_today"] > 0,
                        "blockers": [reason_text(x) for x in _blockers(active, usage)]})
    return payload


def _blockers(grant: dict[str, Any], usage: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if grant["status"] != "active":
        out.append({"paused": "grant_paused", "revoked": "grant_revoked",
                    "expired": "grant_expired"}.get(grant["status"], "grant_not_active"))
    end = (grant["spec"].get("window") or {}).get("end")
    if end is not None and time.time() >= float(end):
        out.append("window_ended")
    if usage["left_total"] <= 0:
        out.append("quota_total_exhausted")
    if usage["left_today"] <= 0:
        out.append("quota_daily_exhausted")
    return out


def dump(value: Any) -> str:
    """稳定的 JSON 文本（授权预览比对用）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
