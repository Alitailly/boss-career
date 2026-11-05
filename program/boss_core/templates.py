"""模板预设：把"按什么评分、怎么写、怎么面"里可变的措辞集中成数据。

默认预设 `general` 方向中性：不假设作者身份、行业、学历阶段或雇佣类型。
业务代码只引用变量名（``{{dimension_primary}}`` 等），具体说法由预设给出；
要换方向时改 ``plan["template"]["id"]``，不用改代码。

约定：

* 预设是**纯数据**，`get()` 返回深拷贝，调用方改了不会污染全局；
* 变量名固定（见 :data:`VARIABLES`），外部 AI 也按这个名字读；
* 渲染缺变量抛 `MissingConfiguration`，不静默留空让模型自由发挥。
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .errors import MissingConfiguration

#: 模板变量占位符；和 :mod:`boss_core.strategies` 共用同一套语法。
VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

#: 默认预设：方向中性。方案里没写 ``template`` 时用它。
DEFAULT_ID = "general"

#: 所有预设都必须提供的可渲染变量（键名固定，测试与外部 AI 都按这个读）。
VARIABLES = (
    "dimension_primary",
    "condition_items",
    "condition_brief",
    "interview_focus",
    "interview_types",
    "question_count",
    "greeting_tone",
    "greeting_max_chars",
    "revision_tone",
    "keyword_seeds",
    "availability_month_label",
    "availability_months_label",
)

#: 预设。`condition_items` / `condition_brief` 是同一份清单的两种写法
#: （列表用于结构化读取，短句用于塞进提示词）。
PRESETS: dict[str, dict[str, Any]] = {
    "general": {
        "id": "general",
        "label": "通用（方向中性）",
        "dimension_primary": "与目标方向的契合度",
        "condition_items": ["学历", "经验年限", "到岗时间", "地点"],
        "condition_brief": "学历、经验年限、到岗时间、地点",
        "interview_focus": "围绕岗位职责与专业能力，追问具体做法、取舍和结果",
        "interview_types": ["技术深度", "项目经历", "场景", "软技能"],
        "question_count": 12,
        "greeting_tone": "用平实、具体的口吻写，突出与岗位相关的事实和结果。",
        "greeting_max_chars": 200,
        "revision_tone": "你是求职顾问，擅长把一份已有的求职简历按目标岗位改一版",
        "keyword_seeds": {
            "能力": ["岗位职责", "专业技能"],
            "经历": ["项目经验", "业务理解"],
            "通用": ["行为面试"],
        },
        "availability_month_label": "可持续",
        "availability_months_label": "可工作时长（月）",
    },
    "technical": {
        "id": "technical",
        "label": "技术方向",
        "dimension_primary": "技术能力与岗位要求的契合度",
        "condition_items": ["学历", "经验年限", "技术栈匹配", "到岗时间", "地点"],
        "condition_brief": "学历、经验年限、技术栈匹配、到岗时间、地点",
        "interview_focus": "围绕 JD 要求的技术栈与工程能力，追问细节、取舍和结果",
        "interview_types": ["技术深度", "项目经历", "场景"],
        "question_count": 12,
        "greeting_tone": "用技术人平实的口吻，讲清做过的方案与结果。",
        "greeting_max_chars": 200,
        "revision_tone": "你是技术方向的求职顾问，擅长把一份已有的求职简历按目标岗位改一版",
        "keyword_seeds": {
            "能力": ["算法", "系统设计", "性能优化"],
            "经历": ["项目经验", "技术方案"],
        },
        "availability_month_label": "可持续",
        "availability_months_label": "可工作时长（月）",
    },
    "non_technical": {
        "id": "non_technical",
        "label": "非技术方向（业务 / 职能 / 运营）",
        "dimension_primary": "岗位职责与经验的契合度",
        "condition_items": ["学历", "经验年限", "行业与职能", "到岗时间", "地点"],
        "condition_brief": "学历、经验年限、行业与职能、到岗时间、地点",
        "interview_focus": "围绕岗位职责、业务场景与协作方式，追问具体做法和结果",
        "interview_types": ["项目经历", "场景", "软技能"],
        "question_count": 10,
        "greeting_tone": "平实、有礼，突出与岗位相关的业务成果和协作经验。",
        "greeting_max_chars": 200,
        "revision_tone": "你是求职顾问，擅长把一份偏业务与职能的求职简历按目标岗位改一版",
        "keyword_seeds": {
            "能力": ["业务理解", "数据分析"],
            "经历": ["项目经验", "沟通协作"],
        },
        "availability_month_label": "可持续",
        "availability_months_label": "可工作时长（月）",
    },
    "internship": {
        "id": "internship",
        "label": "实习 / 在校",
        "dimension_primary": "实习岗位要求与在校经历的契合度",
        "condition_items": ["实习性质", "届别", "每周天数", "可持续时长", "城市"],
        "condition_brief": "实习性质、届别、每周天数、可持续时长、城市",
        "interview_focus": "围绕实习岗位的基础能力、学习能力与可投入时间，追问具体安排",
        "interview_types": ["项目经历", "场景", "软技能"],
        "question_count": 8,
        "greeting_tone": "像学生本人写的第一条消息，朴素、具体，说明可到岗安排。",
        "greeting_max_chars": 200,
        "revision_tone": "你是求职顾问，擅长把一份在校生的求职简历按目标岗位改一版",
        "keyword_seeds": {
            "经历": ["课程项目", "实习经历"],
            "能力": ["技能基础", "学习能力"],
        },
        "availability_month_label": "可实习",
        "availability_months_label": "可实习月数",
    },
    "experienced": {
        "id": "experienced",
        "label": "资深 / 全职",
        "dimension_primary": "岗位职责与资深经验的契合度",
        "condition_items": ["学历", "经验年限", "行业与职级", "到岗时间", "地点"],
        "condition_brief": "学历、经验年限、行业与职级、到岗时间、地点",
        "interview_focus": "围绕过往职责、业绩与决策取舍，追问细节和结果",
        "interview_types": ["项目经历", "场景", "软技能"],
        "question_count": 12,
        "greeting_tone": "职业、克制，突出可迁移的职责与业绩。",
        "greeting_max_chars": 200,
        "revision_tone": "你是求职顾问，擅长把一份有多年工作经验的求职简历按目标岗位改一版",
        "keyword_seeds": {
            "经历": ["职责范围", "业绩指标"],
            "能力": ["团队协作", "行业经验"],
        },
        "availability_month_label": "可持续",
        "availability_months_label": "可工作时长（月）",
    },
}


def ids() -> tuple[str, ...]:
    """全部预设 id（含默认）。"""
    return tuple(PRESETS)


def get(preset_id: str | None = None) -> dict[str, Any]:
    """取预设（深拷贝）。

    未知 id 回落到 :data:`DEFAULT_ID`，并在返回值里标记
    ``fallback=True`` / ``requested=<原 id>``——读取路径不报错，写入路径
    （:func:`boss_core.models.normalise_plan`）才报 `InvalidInput`。
    """
    key = str(preset_id or "").strip()
    if key in PRESETS:
        out = copy.deepcopy(PRESETS[key])
        out["fallback"] = False
        return out
    out = copy.deepcopy(PRESETS[DEFAULT_ID])
    out["fallback"] = True
    out["requested"] = key
    return out


def plan_preset_id(plan: dict[str, Any] | None) -> str:
    """方案里选的预设 id；缺省或未知一律按默认预设。"""
    choice = (plan or {}).get("template")
    if isinstance(choice, dict):
        raw = str(choice.get("id") or "").strip()
    elif isinstance(choice, str):
        raw = choice.strip()
    else:
        raw = ""
    return raw if raw in PRESETS else DEFAULT_ID


def _flat_seeds(seeds: dict[str, list[str]]) -> list[str]:
    """关键词分组压平成一串候选（只作建议，不自动启用）。"""
    out: list[str] = []
    for group in seeds.values():
        for word in group:
            text = str(word).strip()
            if text and text not in out:
                out.append(text)
    return out


def variables(preset_id: str | None = None) -> dict[str, Any]:
    """预设 → 可渲染变量。字符串变量可直接塞进模板，列表另给 ``*_list``。"""
    preset = get(preset_id)
    items = [str(x).strip() for x in preset["condition_items"] if str(x).strip()]
    types = [str(x).strip() for x in preset["interview_types"] if str(x).strip()]
    seeds = _flat_seeds(preset["keyword_seeds"])
    return {
        "preset_id": preset["id"],
        "preset_label": preset["label"],
        "dimension_primary": preset["dimension_primary"],
        "condition_items": "、".join(items),
        "condition_brief": preset["condition_brief"],
        "interview_focus": preset["interview_focus"],
        "interview_types": "、".join(types),
        "interview_type_list": list(types),
        "question_count": int(preset["question_count"]),
        "greeting_tone": preset["greeting_tone"],
        "greeting_max_chars": int(preset["greeting_max_chars"]),
        "revision_tone": preset["revision_tone"],
        "keyword_seeds": "、".join(seeds),
        "keyword_seed_list": ", ".join(f'"{word}"' for word in seeds),
        "availability_month_label": preset["availability_month_label"],
        "availability_months_label": preset["availability_months_label"],
    }


def render(text: str, variables_: dict[str, Any] | None = None) -> str:
    """替换 ``{{var}}``；缺变量抛 `MissingConfiguration` 并列出名字。

    和 `boss_core.strategies.render` 是同一套语义：换源不改行为，缺变量
    不静默留空。``variables_`` 为 ``None`` 时按默认预设补全。
    """
    ctx = dict(variables(variables_.get("preset_id") if variables_ else None))
    ctx.update(variables_ or {})
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in ctx:
            missing.append(name)
            return match.group(0)
        value = ctx[name]
        return value if isinstance(value, str) else str(value)

    out = VAR.sub(repl, text)
    if missing:
        raise MissingConfiguration(f"策略模板缺变量：{'、'.join(sorted(set(missing)))}",
                                   field="strategy.content",
                                   detail={"missing": sorted(set(missing))})
    return out


def fallback_prompt(template: str, preset_id: str | None = None) -> str:
    """按预设渲染一段兜底提示词（不需要方案）。"""
    return render(template, variables(preset_id))
