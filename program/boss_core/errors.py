"""稳定错误类别：外部 AI 靠 `code` 分流，不靠匹配异常字符串。

每个错误都带 `message`（可读解释）、可选 `field`（定位字段）、`next`（下一步动作）。
HTTP 层只做映射，不再自造类别。
"""

from __future__ import annotations

from typing import Any


class CoreError(Exception):
    """业务可读错误的基类。

    Attributes:
        code: 稳定类别，外部 AI 与面板都按它分流。
        status_code: 映射到 HTTP 状态码。
    """

    code = "invalid_input"
    status_code = 422

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        next_steps: list[str] | None = None,
        detail: Any = None,
    ) -> None:
        """初始化；`message` 要能直接给人看。"""
        super().__init__(message)
        self.message = message
        self.field = field
        self.next_steps = list(next_steps or [])
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        """组装 `{ok, code, message[, field][, next][, detail]}`。"""
        body: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.field:
            body["field"] = self.field
        if self.next_steps:
            body["next"] = self.next_steps
        if self.detail is not None:
            body["detail"] = self.detail
        return body


class InvalidInput(CoreError):
    """字段缺失、类型不对或取值越界。"""

    code = "invalid_input"
    status_code = 422


class NotFound(CoreError):
    """指定的方案/资料/任务/会话不存在。"""

    code = "not_found"
    status_code = 404


class Conflict(CoreError):
    """同一对象被并发修改，或状态机不允许当前操作。"""

    code = "revision_conflict"
    status_code = 409


class MissingConfiguration(CoreError):
    """有效快照缺必需项（身份、目标、策略、模型连接等）。"""

    code = "missing_configuration"
    status_code = 422


class Unsupported(CoreError):
    """策略来源或执行器不具备所需能力，且不允许静默降级。"""

    code = "unsupported_strategy"
    status_code = 422


class StaleInput(CoreError):
    """结果绑定的输入哈希已失效，只能留档，不能作为当前依据。"""

    code = "stale_input"
    status_code = 409


class NeedsLogin(CoreError):
    """平台登录态失效；需要人重新登录，不是重试能解决的。"""

    code = "needs_login"
    status_code = 409


class ExternalResultInvalid(CoreError):
    """外部 AI 提交的结果缺岗、越界、证据对不上或 schema 不符。"""

    code = "external_result_invalid"
    status_code = 422


class Environment(CoreError):
    """本机环境不满足（数据目录不可写、缺依赖、端口占用）。"""

    code = "environment"
    status_code = 503
