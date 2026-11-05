"""按范围授权挑出这一轮要发的岗位，并占住额度。

一段逻辑两个入口：面板/AI 手动点「按授权发」走 HTTP，周期计划到点由执行器调这里。
两边必须是同一套判定，否则"计划跑的"和"手动跑的"会悄悄不一致。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from boss_core.authorization import reason_text

from . import authz


class NotAllowed(Exception):
    """授权当前不允许发送；`reasons` 是可读原因，`blocked` 是逐条明细。"""

    def __init__(self, message: str, *, reasons: list[str] | None = None,
                 blocked: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.reasons = reasons or []
        self.blocked = blocked or []


def verify(root: Path, grant_id: str) -> tuple[list[str], list[str]]:
    """**执行前**复核授权层：状态、有效期、版本漂移。

    为什么还要查一遍：任务从建好到被领取之间可能过了几分钟，用户在这段时间按了
    暂停、或者改了方案——"暂停在发送前生效"不能只在建任务那一刻成立。

    这里**不查额度**：额度在建任务时已经预占，执行时余额必然偏少（甚至正好为 0），
    拿它再判一次会把合法任务误杀。岗位层的条件（分数、正文、冷却、黑名单）也不在这里
    重复判定——那些由发送链自己再核一遍。

    Returns:
        `(blocking, texts)`；`blocking` 为空表示可以继续。
    """
    grants = authz.store(root)
    decision = grants.evaluate(grant_id, snapshot=authz.basis(root), jobs=[])
    quota_only = {"quota_total_exhausted", "quota_daily_exhausted"}
    blocking = [code for code in decision["blocking"] if code not in quota_only]
    return blocking, [reason_text(code) for code in blocking]


def prepare(root: Path, grant_id: str, *, task_id: str | None = None,
            force: bool = True) -> dict[str, Any]:
    """判定 → 占额度 → 返回可交给发送链的 `{job_id: review_hash}`。

    抛 `NotAllowed` 表示这一轮不该发（授权暂停/过期/超范围/没额度）。
    """
    from ..api import workflow as view  # 延迟导入：api 层已经 import 了本模块的兄弟

    grants = authz.store(root)
    try:
        grant = grants.get(grant_id)
    except Exception as exc:  # noqa: BLE001 - 找不到就是没授权
        raise NotAllowed(f"找不到授权：{grant_id}") from exc

    report = view.sendable(force=force)
    if not report.get("ok"):
        raise NotAllowed(report.get("error") or "无法核对待发稿件")
    jobs = authz.jobs_from_rows(report.get("rows") or [])
    decision = grants.evaluate(grant, snapshot=authz.basis(root), jobs=jobs)
    if not decision["ok"]:
        raise NotAllowed("授权当前不能发送",
                         reasons=decision["reasons_text"] or decision["blocking"],
                         blocked=decision["blocked"])
    if not decision["allowed"]:
        detail = ""
        if decision["blocked"]:
            first = decision["blocked"][0]
            detail = (f"（最近一条被拦：{first['job_id']}——"
                      + "、".join(reason_text(x) for x in first["reasons"]) + "）")
        raise NotAllowed("授权范围内现在没有可发岗位" + detail,
                         blocked=decision["blocked"][:10])

    task_id = task_id or f"authsend_{uuid.uuid4().hex[:16]}"
    taken = grants.reserve(grant_id, decision["allowed"], task_id=task_id)
    if not taken:
        # 区分"没额度了"和"这几条已经占过额度了"——后者不是故障，不该报警
        used = {row["job_id"] for row in grants.rows(grant_id, limit=4000)
                if row["outcome"] != "released"}
        if all(item["job_id"] in used for item in decision["allowed"]):
            raise NotAllowed("这一轮没有可发岗位：可发的都已在授权内发过或在途，不会重复发送")
        raise NotAllowed("这一轮没有可发名额，额度已被其它任务占用",
                         reasons=["quota_total_exhausted"])
    return {"task_id": task_id, "grant_id": grant_id,
            "approved": {item["job_id"]: item["body_sha"] for item in taken},
            "blocked": decision["blocked"][:10], "usage": grants.usage(grant_id)}
