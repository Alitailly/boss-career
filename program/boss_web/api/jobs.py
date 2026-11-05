"""职位库：列表 / 详情 / 删除 / 统计 / 补抓描述。

`fetch-descriptions` 必须注册在 `/{encrypt_job_id}` 之前，免得被动态路由吃掉。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from boss_jobs.store import JobStore

from ..errors import NotFoundError, ValidationWebError
from ..services.desc_task import desc_tasks

router = APIRouter()


class DeleteBody(BaseModel):
    """批量删除职位。"""

    ids: list[str] = Field(default_factory=list)


class ClearBody(BaseModel):
    """清空职位库。"""

    confirm: bool = False
    city: str | None = None
    keyword: str | None = None


class FetchDescBody(BaseModel):
    """补抓 JD 参数。

    Attributes:
        limit: 本次最多补几条；0 = 全部没描述的。
        interval: 条间隔（秒）。0/不传 = 服务端默认 `DETAIL_INTERVAL`（1s），防风控。
    """

    limit: int = Field(0, ge=0, le=5000)
    interval: float = Field(0.0, ge=0.0, le=30.0)


@router.get("")
def list_jobs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    city: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    jd_status: str | None = None,
) -> dict[str, Any]:
    """分页列出职位库。"""
    with JobStore() as store:
        items = store.list_jobs(
            limit=limit, offset=offset, city=city, keyword=keyword,
            category=category, jd_status=jd_status,
        )
        total = store.count_jobs_matching(
            city=city, keyword=keyword, category=category, jd_status=jd_status
        )
    return {
        "items": [j.to_dict() for j in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats")
def stats() -> dict[str, Any]:
    """职位库汇总统计与最近翻页日志。"""
    with JobStore() as store:
        summary = store.summary()
        summary["missing_desc"] = store.count_jobs_missing_desc()
        pages_log = []
        for row in store.list_pages(limit=20):
            item = dict(row)
            item["inserted"] = item.get("inserted_count", 0)
            item["updated"] = item.get("updated_count", 0)
            pages_log.append(item)
        summary["pages_log"] = pages_log
    return summary


@router.post("/fetch-descriptions")
def fetch_descriptions(body: FetchDescBody | None = None) -> dict[str, Any]:
    """对还没抓到描述的职位批量补 JD（后台任务）。

    名单 = 一次没抓过的 + 抓过但 `jd_status = 'failed'` 的；回过空 JD 的不再撞。
    """
    limit = (body.limit if body else 0) or 0
    interval = (body.interval if body else 0.0) or None
    task = desc_tasks.start(limit=limit, interval=interval)
    return task.snapshot()


@router.get("/fetch-descriptions/status")
def fetch_descriptions_status() -> dict[str, Any]:
    """补抓 JD 任务快照。"""
    return desc_tasks.snapshot()


@router.post("/fetch-descriptions/cancel")
def fetch_descriptions_cancel() -> dict[str, Any]:
    """取消补抓 JD 任务。"""
    task = desc_tasks.cancel()
    return task.snapshot()


@router.get("/{encrypt_job_id}")
def get_job(encrypt_job_id: str) -> dict[str, Any]:
    """按 id 取单条职位。"""
    with JobStore() as store:
        job = store.get_job(encrypt_job_id)
    if job is None:
        raise NotFoundError(f"职位不存在：{encrypt_job_id}")
    from ..services.job_verification import saved
    return {**job.to_dict(), 'verification': saved(encrypt_job_id)}


@router.get('/{encrypt_job_id}/audit')
def audit_job(encrypt_job_id: str):
    """Read local source/timing evidence and current conditions; no BOSS requests."""
    from ..services import job_audit, workflow_context
    with JobStore() as store:
        job = store.get_job(encrypt_job_id)
        if job is None:
            raise NotFoundError('岗位不存在')
        return job_audit.audit(store, job, workflow_context.snapshot())


@router.post('/{encrypt_job_id}/verify')
def verify_job(encrypt_job_id: str):
    """Refresh one platform detail and return the hard-condition verdict; sends no message."""
    from boss_jobs import create_client
    from ..services import workflow_context, job_verification
    snap = workflow_context.snapshot()
    if not snap:
        raise ValidationWebError('先启用求职方案，再核验岗位')
    with JobStore() as store:
        job = store.get_job(encrypt_job_id)
        if job is None:
            raise NotFoundError('岗位不存在')
        client = create_client(auto_stoken=False)
        client.timeout, client.retries = 20, 0
        reason, _ = job_verification.verify(client, store, job, snap)
    return {'ok': True, 'send_allowed': not bool(reason), 'reason': reason,
            'verification': job_verification.saved(encrypt_job_id)}


class JobConflictBody(BaseModel):
    confirmed: Literal[True]
    reason: str = Field(min_length=1, max_length=500)
    active: bool = True


@router.post('/{encrypt_job_id}/conflict')
def flag_job_conflict(encrypt_job_id: str, body: JobConflictBody):
    """Record or resolve a user-reported information conflict; active conflicts block sends."""
    from ..services.job_verification import flag_conflict
    with JobStore() as store:
        if store.get_job(encrypt_job_id) is None:
            raise NotFoundError('岗位不存在')
    return {'ok': True, 'verification': flag_conflict(encrypt_job_id, body.reason, active=body.active)}


@router.delete("/{encrypt_job_id}")
def delete_job(encrypt_job_id: str) -> dict[str, Any]:
    """按 id 删单条职位。"""
    with JobStore() as store:
        ok = store.delete_job(encrypt_job_id)
    if not ok:
        raise NotFoundError(f"职位不存在：{encrypt_job_id}")
    return {"deleted": 1}


@router.post("/delete")
def delete_many(body: DeleteBody) -> dict[str, Any]:
    """批量删除职位。"""
    if not body.ids:
        raise ValidationWebError("没有选中任何职位")
    with JobStore() as store:
        deleted = store.delete_jobs(body.ids)
    return {"deleted": deleted}


@router.post("/clear")
def clear_jobs(body: ClearBody) -> dict[str, Any]:
    """清空职位库（需 confirm）。"""
    if not body.confirm:
        raise ValidationWebError("需要 confirm=true 才能清空")
    with JobStore() as store:
        deleted = store.clear_jobs(city=body.city, keyword=body.keyword)
    return {"deleted": deleted}
