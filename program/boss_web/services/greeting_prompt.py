"""招呼语生成约束，供完整匹配和单独重生成共用。

规则只有一份：用户在「系统配置 → 招呼语」里维护，存在 `app_settings` 里。
这里不再保留出厂默认文本——留着它只会让"界面显示"和"模型实际收到"两处分叉。
空值 = 未配置：写稿与重新生成会明确报错，而不是悄悄用一段看不见的旧规则。
"""

from __future__ import annotations

from pathlib import Path


def resolve_greeting_requirement(path: Path | str | None = None) -> str:
    """取当前生效的招呼语约束；未配置时返回空串。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。

    Returns:
        要插进提示词的约束文本；空串表示用户还没配置。
    """
    from .app_settings_store import load_settings

    return load_settings(path).greeting_requirement.strip()


def require_greeting_requirement(path: Path | str | None = None) -> str:
    """同 :func:`resolve_greeting_requirement`，但未配置时抛 ValueError。

    需要"没有规则就不许写稿"的调用方用这个，避免把空串塞进提示词。
    """
    text = resolve_greeting_requirement(path)
    if not text:
        raise ValueError("招呼语规则还没配置，请先到「系统配置 → 招呼语」写一份。")
    return text
