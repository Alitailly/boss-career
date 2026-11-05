"""工作区配置接口：`GET/POST /api/system/workspace`。

和 `/api/workflow/*` 的区别：这里**不要求工作区已就绪**。刚下载、还没建工作区时，
设置页要能打开、要能告诉人下一步做什么，所以：

* ``GET`` 只读现状 + 「下一步该做什么」清单，永远是 200；
* ``POST`` 才真正创建/绑定工作区，并且要求 ``confirmed=true``（前端按钮会带）。

真正读工作流台账的接口仍然守在 ``workflow_control.paths()`` 后面，缺配置回 422；
本模块刻意不经过那条路径。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import ValidationWebError
from ..services import workspace_setup

router = APIRouter()


class ConfigureBody(BaseModel):
    """创建工作区请求体。

    Attributes:
        path: 工作区目录；空串 = 用默认目录（``<数据目录>/workspace``）。
        code: 工作流代码目录；空串 = 自动解析（环境变量 → launch.json → 状态目录 → 自带 workflow/）。
        confirmed: 必须为 true 才真正建目录，避免误触把工作区建到奇怪的位置。
    """

    path: str = Field("", description="工作区目录，空串表示用默认目录")
    code: str = Field("", description="工作流代码目录，空串表示自动解析")
    confirmed: bool = Field(False, description="必须为 true 才执行创建/绑定")


@router.get("/workspace")
def workspace_state() -> dict[str, Any]:
    """工作区现状与下一步清单；没配置也返回 200。"""
    state = workspace_setup.current()
    try:
        state.update(workspace_setup.checklist(state))
    except Exception:  # noqa: BLE001 - 清单算不出来也要把现状交出去
        state.update({"checks": [], "next_steps": []})
    state["ok"] = True
    return state


@router.post("/workspace")
def configure_workspace(body: ConfigureBody) -> dict[str, Any]:
    """创建（或绑定）工作区并持久化到 ``launch.json``。"""
    if not body.confirmed:
        raise ValidationWebError("创建工作区要明确确认：请在请求体里带 confirmed=true")
    return workspace_setup.configure(
        body.path or None,
        code=body.code or None,
        actor="setup",
    )
