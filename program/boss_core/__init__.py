"""通用求职核心：领域对象、方案/策略版本、条件判断、快照、迁移。

这个包只依赖标准库，不导入 Web 框架或平台适配器，方便被 HTTP、CLI、采集器和
外部 AI 结果导入共同使用。

主要入口：

* :class:`boss_core.workspace.Workspace` —— 用户数据根：版本、活动指针、审计；
* :func:`boss_core.snapshot.build` / :func:`boss_core.snapshot.require` —— 有效快照；
* :func:`boss_core.conditions.judge` —— 岗位条件三态判断；
* :func:`boss_core.greeting.compose` —— 合成有效写作规则；
* :mod:`boss_core.migration` —— 旧工作区迁移（dry-run + 幂等）。
"""

from __future__ import annotations

from .errors import (
    Conflict,
    CoreError,
    Environment,
    ExternalResultInvalid,
    InvalidInput,
    MissingConfiguration,
    NeedsLogin,
    NotFound,
    StaleInput,
    Unsupported,
)
from .workspace import Workspace

__version__ = "1.0.0"

__all__ = [
    "Conflict",
    "CoreError",
    "Environment",
    "ExternalResultInvalid",
    "InvalidInput",
    "MissingConfiguration",
    "NeedsLogin",
    "NotFound",
    "StaleInput",
    "Unsupported",
    "Workspace",
    "__version__",
]
