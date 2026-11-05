"""投递：评分阈值配置 + 打招呼发送任务。

阈值落状态库 `doc('deliver_config')`（见 `boss_web.services.deliver_config_store`）。
一键 / 多条 / 单条三种粒度共用 `POST /start`，区别只在 `encrypt_job_ids` 的长短。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import ValidationWebError
from ..services.deliver_config_store import load_config, update_config
from ..services.deliver_task import deliver_tasks

router = APIRouter()


class ConfigBody(BaseModel):
    """投递评分阈值。

    Attributes:
        min_score: 0-100 分制；越界由 FastAPI 直接回 422。
    """

    min_score: int = Field(..., ge=0, le=100)


class DeliverBody(BaseModel):
    """一次投递请求。

    Attributes:
        mode: 发送粒度；未声明模式的旧客户端按 `selected` 处理。
        confirmed: 受限职位在 `selected`/`manual` 模式下需要显式确认。
    """

    analysis_id: str
    mode: Literal["all", "selected", "manual"] = "selected"
    confirmed: bool = False
    confirmation_token: str = ""
    encrypt_job_ids: list[str] = Field(default_factory=list)
    sender_account_id: str = ''
    sender_session_version: str = ''
    application_ids: dict[str, str] = Field(default_factory=dict)


@router.get("/config")
def get_config() -> dict[str, Any]:
    """读投递评分阈值。"""
    return load_config().to_dict()


@router.put("/config")
def put_config(body: ConfigBody) -> dict[str, Any]:
    """写投递评分阈值。"""
    return update_config(body.model_dump()).to_dict()


@router.post("/start")
def start(body: DeliverBody) -> dict[str, Any]:
    """启动打招呼发送任务；清单为空时抛 `ValidationWebError`。"""
    if not body.encrypt_job_ids:
        raise ValidationWebError("没有要发送的岗位")
    task = deliver_tasks.start(
        analysis_id=body.analysis_id,
        job_ids=body.encrypt_job_ids,
        mode=body.mode,
        confirmed=body.confirmed,
        confirmation_token=body.confirmation_token,
        sender_account_id=body.sender_account_id,
        sender_session_version=body.sender_session_version,
        application_ids=body.application_ids,
    )
    return task.snapshot()


@router.get("/status")
def status() -> dict[str, Any]:
    """当前发送任务快照。"""
    from ..services.delivery_control import state
    return {**deliver_tasks.snapshot(), 'read_control': state()}


class ResumeBody(BaseModel):
    confirmed: Literal[True]
    scope: Literal['send', 'verification'] = 'send'


@router.post('/resume')
def resume(body: ResumeBody):
    """处理平台提示后清除本地暂停；不启动、重试或发送任何消息。"""
    from ..services.delivery_control import set_paused
    return {'ok': True, 'read_control': set_paused(False, '用户确认处理后恢复', scope=body.scope)}


@router.post("/{task_id}/cancel")
def cancel(task_id: str) -> dict[str, Any]:
    """取消指定发送任务。"""
    task = deliver_tasks.cancel(task_id)
    return task.snapshot()
