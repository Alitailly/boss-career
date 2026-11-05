"""匹配舱：启动/轮询匹配任务、改招呼语、看历史分析。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import NotFoundError, ValidationWebError
from ..services.deliver_task import enrich_match_greet_state
from ..services.match_task import match_tasks, regenerate_greeting, rematch_job
from ..services.resume_store import (
    analyses_cover,
    delete_analysis,
    list_analyses,
    load_analysis,
    match_index,
    update_greeting,
)

router = APIRouter()


class MatchBody(BaseModel):
    """启动人岗匹配。

    Attributes:
        job_ids: 空 = 全库全部岗位；非空 = 只对勾选的 job_id 跑。
        crawl_task_id: 非空 = 只跑该抓取任务的岗位（优先级 > job_ids > 全库）。
    """

    resume_id: str
    job_ids: list[str] = Field(default_factory=list)
    crawl_task_id: str = ""
    limit: int = Field(default=0, ge=0, le=150)


class GreetingBody(BaseModel):
    """改单条招呼语。"""

    encrypt_job_id: str
    greeting: str = Field("", max_length=500)


class RegenGreetingBody(BaseModel):
    """重打招呼语草稿。"""

    encrypt_job_id: str


class RematchBody(BaseModel):
    """单条重跑匹配。"""

    encrypt_job_id: str


def _with_greet_state(snap: dict[str, Any]) -> dict[str, Any]:
    """任务快照出门前补当前发送史与冷却状态。"""
    matches = snap.get("matches")
    if isinstance(matches, list):
        enrich_match_greet_state([m for m in matches if isinstance(m, dict)])
        from ..services.job_verification import saved
        for item in matches:
            if isinstance(item, dict):
                conflict = saved(str(item.get('encrypt_job_id') or '')).get('conflict') or {}
                item['information_conflict'] = bool(conflict.get('active'))
                item['information_conflict_reason'] = conflict.get('reason') if conflict.get('active') else ''
    return snap


@router.post("/analyze")
def start_match(body: MatchBody) -> dict[str, Any]:
    """启动人岗匹配任务。"""
    task = match_tasks.start(
        resume_id=body.resume_id,
        job_ids=body.job_ids,
        crawl_task_id=body.crawl_task_id,
        limit=body.limit,
    )
    snap = task.snapshot()
    if "scope" not in snap:
        snap["scope"] = task.scope_info()
    return snap


@router.get("/analyze/status")
def match_status() -> dict[str, Any]:
    """当前匹配任务快照。"""
    return _with_greet_state(match_tasks.snapshot())


@router.get("/analyze/{task_id}")
def match_task(task_id: str) -> dict[str, Any]:
    """按 id 取匹配任务快照。"""
    return _with_greet_state(match_tasks.snapshot(task_id))


@router.post("/analyze/{task_id}/cancel")
def cancel_match(task_id: str) -> dict[str, Any]:
    """取消匹配任务。"""
    task = match_tasks.cancel(task_id)
    return task.snapshot()


@router.patch("/{analysis_id}/greeting")
def patch_greeting(analysis_id: str, body: GreetingBody) -> dict[str, Any]:
    """只改 payload 里那一条 match 的招呼语。"""
    try:
        item = update_greeting(analysis_id, body.encrypt_job_id, body.greeting)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
    except KeyError as exc:
        raise NotFoundError(f"分析里没有这个职位：{body.encrypt_job_id}") from exc
    return {"ok": True, "item": item}


@router.post("/{analysis_id}/greeting/regenerate")
def regen_greeting(analysis_id: str, body: RegenGreetingBody) -> dict[str, Any]:
    """重打一条招呼语草稿；只回新文案，不落库。"""
    try:
        greeting = regenerate_greeting(
            analysis_id=analysis_id, encrypt_job_id=body.encrypt_job_id
        )
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
    except KeyError as exc:
        raise NotFoundError(f"分析里没有这个职位：{body.encrypt_job_id}") from exc
    return {"ok": True, "greeting": greeting}


@router.post("/{analysis_id}/rematch")
def rematch_one(analysis_id: str, body: RematchBody) -> dict[str, Any]:
    """对一条 match 重跑人岗匹配；只改库里那条，不新增 analysis。"""
    try:
        item = rematch_job(analysis_id=analysis_id, encrypt_job_id=body.encrypt_job_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
    except KeyError as exc:
        raise NotFoundError(f"分析里没有这个职位：{body.encrypt_job_id}") from exc
    return {"ok": True, "item": item}


@router.get("/analyses")
def analyses() -> dict[str, Any]:
    """历史分析列表。

    除了逐条记录，还回一组汇总数：`cover` 的 `records` 是批次条数、`slots` 是明细条数
    （同一岗位被重打会重复计入）、`covered` 是去重后的岗位数。面板只显示批次条数时，
    会被读成"只分析过 138 个岗位"，跟「已打分 1000 多个」打架。
    """
    items = list_analyses()
    try:
        cover = analyses_cover()
    except Exception:  # noqa: BLE001 - 汇总失败不影响列表本身
        cover = {"records": len(items), "slots": 0, "covered": 0}
    return {"items": items, "cover": cover}


@router.get("/index")
def match_index_api(resume_id: str = "") -> dict[str, Any]:
    """每个岗位一份可用分数（含以前简历打的），并标明是哪份简历。

    `resume_id` 是"优先用谁"，不是"只要谁"——旧分数照样能用（2026-11-01 用户定）。
    """
    return {"items": match_index(resume_id)}


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str) -> dict[str, Any]:
    """按 id 取历史分析；出门前补当前发送史与冷却状态。"""
    try:
        payload = load_analysis(analysis_id)
    except FileNotFoundError as exc:
        raise NotFoundError(f"分析结果不存在：{analysis_id}") from exc
    return _with_greet_state(payload)


@router.delete("/analyses/{analysis_id}")
def remove_analysis(analysis_id: str) -> dict[str, Any]:
    """删除一条历史分析。"""
    if not delete_analysis(analysis_id):
        raise NotFoundError(f"分析结果不存在：{analysis_id}")
    return {"deleted": True}
