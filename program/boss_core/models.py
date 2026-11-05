"""领域对象与归一化规则：候选人资料、求职方案、策略版本。

三个原则：

1. **不替用户补值**。缺就是缺，用 `{"mode": "unknown"}` 表达；不能拿原用户的默认值填。
2. **未知 / 不限 / 不披露分开**。`unknown`（不知道）、`any`（明确不限）、`list`/`value`（具体要求）；
   披露是另一组开关，只决定"能不能写进招呼语"，不参与筛选。
3. **字段名稳定**。面板、HTTP、CLI 和外部 AI 都读同一份结构，改字段要带 schema 版本。
"""

from __future__ import annotations

from typing import Any

from . import templates
from .errors import InvalidInput

PLAN_SCHEMA = "plan/v1"
CANDIDATE_SCHEMA = "candidate/v1"
STRATEGY_SCHEMA = "strategy/v1"

#: 学历从低到高；判定"岗位要求是否高于候选人"用得到。
DEGREES = ("不限", "中专", "大专", "本科", "硕士", "博士")

#: 雇佣类型。`any` 表示明确接受全部。
EMPLOYMENT = ("internship", "full_time", "part_time", "any")

WORK_MODES = ("onsite", "hybrid", "remote")

STAGES = ("resume_revision", "matching", "greeting", "interview")


def c_unknown() -> dict[str, Any]:
    """明确的"不知道"。"""
    return {"mode": "unknown"}


def c_any() -> dict[str, Any]:
    """明确的"不限/都行"。和"不知道"不是一回事。"""
    return {"mode": "any"}


def c_list(values: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """白名单；空列表等于"不知道"，调用方应显式传 `c_unknown()`。"""
    cleaned = [str(x).strip() for x in values if str(x).strip()]
    return {"mode": "list", "values": cleaned} if cleaned else c_unknown()


def c_value(value: Any) -> dict[str, Any]:
    """单值（天数、月数、最低学历）。`None` 表示"不知道"。"""
    if value is None:
        return c_unknown()
    if value == "any":
        return c_any()
    return {"mode": "value", "value": value}


def c_mode(choice: Any) -> str:
    """取选择器模式；非字典或坏值按 `unknown` 处理，不抛异常。"""
    if isinstance(choice, dict):
        mode = str(choice.get("mode") or "")
        if mode in {"unknown", "any", "list", "value"}:
            return mode
    if choice is None:
        return "unknown"
    if choice == "any":
        return "any"
    if isinstance(choice, list):
        return "list" if choice else "unknown"
    return "value"


def c_values(choice: Any) -> list[str]:
    """列表模式下的值；其它模式返回空列表。"""
    if c_mode(choice) != "list":
        return []
    raw = (choice or {}).get("values") if isinstance(choice, dict) else choice
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def c_scalar(choice: Any) -> Any:
    """单值模式下的值；其它模式返回 `None`。"""
    if c_mode(choice) != "value":
        return None
    return (choice or {}).get("value") if isinstance(choice, dict) else choice


def default_plan() -> dict[str, Any]:
    """中性方案模板：每一项都是显式的未知，不含任何原用户信息。"""
    return {
        "schema": PLAN_SCHEMA,
        "id": "",
        "revision": 0,
        "parent": None,
        "status": "draft",
        "label": "",
        "created_at": "",
        "created_by": "",
        "candidate_id": "",
        # 提示词模板预设：只选 id，具体措辞在 `boss_core.templates` 里。
        # 默认 `general` 是方向中性的，不继承任何个人方案。
        "template": {"id": templates.DEFAULT_ID},
        "target": {
            "directions": c_unknown(),
            "levels": c_unknown(),
            "employment_types": c_unknown(),
            "cities": c_unknown(),
            "work_modes": c_unknown(),
            "accepts_relocation": c_unknown(),
            "industries": c_unknown(),
            "company_scales": c_unknown(),
        },
        "requirements": {
            "degree_min": c_unknown(),
            "degree_block": c_unknown(),
            "years_min": c_unknown(),
            "excluded_companies": c_unknown(),
            "excluded_keywords": c_unknown(),
            "excluded_titles": c_unknown(),
            "hard_conflicts": c_unknown(),
        },
        "salary": {
            "currency": "CNY",
            "period": "month",
            "min": None,
            "max": None,
            "days_per_month": 22,
            "months_per_year": 12,
            "unknown": "unknown",
            "compare": "top",
        },
        "availability": {
            "start": c_unknown(),
            "days_per_week": c_unknown(),
            "months": c_unknown(),
            "graduation": c_unknown(),
        },
        "disclosure": {
            "city": False,
            "availability": True,
            "salary": False,
            "facts": {"mode": "all"},
        },
        "search": {
            "keywords": [],
            "base": {},
            "city_filters": {},
            # 采集/补 JD/匹配/探活跃的数值键都要列在模板里：`_merge` 只认模板已有的键，
            # 没列的会被塞进 `extra`，编译时等于"没配置"——显式 0 会整个丢掉。
            # `pages` 留空模板但必须列出：否则嵌套的 max_pages_per_query 会被塞进
            # `extra` 丢掉。不在这里填 20，免得嵌套默认值盖掉顶层的显式设置。
            "pages": {},
            "pages_per_query": 3,
            "max_pages_per_query": 20,
            "daily_page_cap": 300,
            "cycle_sleep_sec": 180,
            "jd_batch": 5,
            "jd_interval": 5.0,
            "daily_jd_cap": 1500,
            "match_batch": 80,
            "match_force_backlog": 60,
            "activity_batch": 4,
            "activity_interval": 8.0,
            "corpus_export_every": 40,
        },
        "runtime": {
            "min_score": 70,
            "daily_cap": 150,
            "activity": {"within_days": 7, "enabled": True},
            "chunk": 1,
            "chunk_gap": 20,
            "batch_size": 6,
            "pricing": {"peak_weekdays": [1, 2, 3, 4, 5],
                        "peak_windows": ["09:00-12:00", "14:00-18:00"],
                        "holidays": [], "source": "builtin"},
        },
        "strategies": {"resume_revision": None, "matching": None,
                       "greeting": None, "interview": None},
        "legacy": {},
    }


def default_candidate() -> dict[str, Any]:
    """中性候选人模板：身份为空即"缺失"，绝不默认成原用户。"""
    return {
        "schema": CANDIDATE_SCHEMA,
        "id": "",
        "revision": 0,
        "parent": None,
        "created_at": "",
        "created_by": "",
        "label": "",
        "resume": {"resume_id": None, "source_sha": None, "raw_path": None,
                   "title": None, "format": None},
        "identity": {"name": None, "phone": None, "email": None},
        "facts": [],
        "education": [],
        "experiences": [],
        "skills": [],
        "scoring_material": {"text": "", "confirmed": False, "resume_id": "", "source_sha": "", "facts_sha": ""},
        "availability": {
            "start": c_unknown(),
            "days_per_week": c_unknown(),
            "months": c_unknown(),
            "graduation": c_unknown(),
        },
        "status": {"identity": "missing", "unknowns": []},
        "legacy": {},
    }


def _merge(default: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """把 `raw` 叠到 `default` 上。

    只递归"嵌套对象"；带 `mode` 的选择器是**叶子**，整体替换，不能把它的
    `value/values` 拆到 `extra` 里去。默认值是空对象时表示"自由结构"（如 legacy），
    原样保留，否则迁移记录会被丢掉。
    """
    if not default:
        return dict(raw)
    out: dict[str, Any] = {}
    for key, default_value in default.items():
        value = raw.get(key, default_value)
        nested = isinstance(default_value, dict) and "mode" not in default_value
        if nested and isinstance(value, dict):
            out[key] = _merge(default_value, value)
        elif isinstance(default_value, dict) and not isinstance(value, dict):
            out[key] = default_value
        else:
            out[key] = value
    extra = {k: v for k, v in raw.items() if k not in default}
    if extra:
        out["extra"] = {**out.get("extra", {}), **extra}
    return out


def normalise_plan(raw: dict[str, Any] | None) -> dict[str, Any]:
    """补全缺省键；不推断任何用户没给的值。"""
    if raw is not None and not isinstance(raw, dict):
        raise InvalidInput("方案必须是 JSON 对象")
    for section in ("target", "search"):
        if section in (raw or {}) and not isinstance(raw[section], dict):
            raise InvalidInput(f"{section} 必须是对象", field=section)
    source_search = (raw or {}).get("search") or {}
    if "base" in source_search and not isinstance(source_search["base"], dict):
        raise InvalidInput("平台条件必须写为对象", field="search.base")
    if 'city_filters' in source_search and not isinstance(source_search['city_filters'], dict):
        raise InvalidInput('城市精细位置必须写为对象', field='search.city_filters')
    for key in ("cities", "industries", "company_scales", "employment_types"):
        target = (raw or {}).get("target") or {}
        if key not in target:
            continue
        choice = target[key]
        if not isinstance(choice, dict) or choice.get("mode") not in ("unknown", "any", "list"):
            raise InvalidInput("筛选选择需使用 unknown / any / list", field=f"target.{key}")
        if choice["mode"] == "list" and (not isinstance(choice.get("values"), list)
                or not choice["values"] or any(not isinstance(v, str) or not v.strip() for v in choice["values"])):
            raise InvalidInput("具体选项需使用非空字符串数组 values", field=f"target.{key}")
    plan = _merge(default_plan(), raw or {})
    plan["schema"] = PLAN_SCHEMA
    plan["template"] = _normalise_template(plan.get("template"))
    plan["target"]["employment_types"] = _normalise_employment(plan["target"]["employment_types"])
    plan["salary"] = _normalise_salary(plan["salary"])
    plan["runtime"] = _normalise_runtime(plan["runtime"])
    from boss_filter.validation import normalize_fields, resolve, option_map, FilterValidationError, SCALARS, MULTIPLE
    base = plan["search"]["base"]
    bad = set(base) - set(SCALARS + MULTIPLE + ("pageSize",))
    if bad:
        raise InvalidInput(f"未知平台筛选字段：{', '.join(sorted(bad))}", field="search.base")
    try:
        plan["search"]["base"] = normalize_fields(base)
        for target_key, platform_key in (("cities", "city"), ("industries", "industry"),
                                         ("company_scales", "scale")):
            choice = plan["target"][target_key]
            if c_mode(choice) == "list":
                values = c_values(choice)
                # Reuse the array validator to reject mixing '不限' with specifics.
                codes = ([resolve(platform_key, v) for v in values] if platform_key == "city"
                         else normalize_fields({platform_key: values})[platform_key])
                if "100010000" in codes and len(codes) > 1 and platform_key == "city":
                    raise FilterValidationError("city", values, "全国不能与具体城市同时选择")
                if platform_key == "city":
                    # 个人城市名单保留可读名称，供本地城市判断使用；仅请求层转平台码。
                    plan["target"][target_key] = (c_any() if codes == ["100010000"] else
                                                 c_list([option_map("city")[c] for c in dict.fromkeys(codes)]))
                else:
                    plan["target"][target_key] = c_list(list(dict.fromkeys(codes)))
    except FilterValidationError as exc:
        raise InvalidInput(str(exc), field=exc.field) from exc
    return plan


def _normalise_template(choice: Any) -> dict[str, Any]:
    """模板预设：只接受 `boss_core.templates` 里有的 id；未知 id 报错而不静默替换。

    读取路径（`templates.plan_preset_id`）可以回落到默认预设，写入路径不行：
    用户写错模板名却按另一个方向跑，比直接报错更难发现。
    """
    if not isinstance(choice, dict):
        raise InvalidInput('模板预设必须是对象，如 {"id": "general"}', field="template")
    pid = str(choice.get("id") or "").strip()
    if pid not in templates.ids():
        raise InvalidInput(f"模板预设不支持：{pid or '（空）'}", field="template.id",
                           next_steps=[f"改为 {' / '.join(templates.ids())}"])
    out = dict(choice)
    out["id"] = pid
    return out


def _normalise_employment(choice: Any) -> dict[str, Any]:
    """雇佣类型只接受约定值；写错就报字段错误，不静默丢弃。"""
    if c_mode(choice) == "list":
        bad = [v for v in c_values(choice) if v not in EMPLOYMENT]
        if bad:
            raise InvalidInput(f"雇佣类型不支持：{'、'.join(bad)}", field="target.employment_types",
                               next_steps=["改为 internship / full_time / part_time / any"])
    return choice


def _normalise_salary(salary: dict[str, Any]) -> dict[str, Any]:
    """薪资口径：周期、币种、是否允许未知，缺一不可。"""
    out = dict(salary)
    if out.get("currency") not in {"CNY", "USD", "EUR", "HKD"}:
        raise InvalidInput("薪资币种只支持 CNY/USD/EUR/HKD", field="salary.currency")
    if out.get("period") not in {"day", "month", "year"}:
        raise InvalidInput("薪资周期只支持 day/month/year", field="salary.period")
    for key in ("min", "max"):
        value = out.get(key)
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            raise InvalidInput(f"薪资 {key} 必须是非负数字或 null", field=f"salary.{key}")
    if out.get("min") is not None and out.get("max") is not None and out["min"] > out["max"]:
        raise InvalidInput("薪资下限不能高于上限", field="salary.min")
    if out.get("unknown") not in {"allow", "unknown", "block"}:
        raise InvalidInput("salary.unknown 只支持 allow/unknown/block", field="salary.unknown")
    if out.get("compare") not in {"top", "overlap"}:
        raise InvalidInput("salary.compare 只支持 top/overlap", field="salary.compare")
    days = out.get("days_per_month")
    # 0 保持为非法（不当"关闭"）：salary.to_monthly 把它当乘数、gate_monthly 折月薪也用它，
    # 0 天/月会让薪资口径直接失真，所以这里仍然只接受 1–31。
    if not isinstance(days, int) or not 1 <= days <= 31:
        raise InvalidInput("salary.days_per_month 必须是 1–31 的整数", field="salary.days_per_month")
    months = out.get("months_per_year")
    # 同理 0 非法：年薪折月薪时 months_per_year 是除数。
    if not isinstance(months, int) or not 1 <= months <= 12:
        raise InvalidInput("salary.months_per_year 必须是 1–12 的整数", field="salary.months_per_year")
    return out


def _normalise_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    """运行偏好取值范围；越界报错而不是夹断。"""
    out = dict(runtime)
    score = out.get("min_score")
    if not isinstance(score, int) or not 0 <= score <= 100:
        raise InvalidInput("runtime.min_score 必须是 0–100 的整数", field="runtime.min_score")
    cap = out.get("daily_cap")
    # 0 是显式"当天不投递"（关闭），不是"没配置"；模板缺省给 150。
    if not isinstance(cap, int) or not 0 <= cap <= 150:
        raise InvalidInput("runtime.daily_cap 必须是 0–150 的整数（0 = 当天不投递）",
                           field="runtime.daily_cap")
    activity = out.get("activity") or {}
    within = activity.get("within_days")
    if within is not None and (not isinstance(within, int) or not 1 <= within <= 366):
        raise InvalidInput("runtime.activity.within_days 必须是 1–366 的整数或 null",
                           field="runtime.activity.within_days")
    return out


def normalise_candidate(raw: dict[str, Any] | None) -> dict[str, Any]:
    """补全候选人键；事实列表保持用户给的原样顺序与编号。"""
    if raw is not None and not isinstance(raw, dict):
        raise InvalidInput("候选人资料必须是 JSON 对象")
    cand = _merge(default_candidate(), raw or {})
    cand["schema"] = CANDIDATE_SCHEMA
    material = cand.get("scoring_material")
    if not isinstance(material, dict) or not isinstance(material.get("text"), str):
        raise InvalidInput("scoring_material 必须包含文本 text", field="scoring_material")
    if type(material.get("confirmed")) is not bool:
        raise InvalidInput("confirmed 必须是布尔值", field="scoring_material.confirmed")
    if len(material["text"]) > 12000:
        raise InvalidInput("评分用材料不能超过12000字符", field="scoring_material.text")
    if material["confirmed"] and (not material["text"].strip() or not material.get("resume_id") or not material.get("source_sha")):
        raise InvalidInput("确认材料需包含正文、简历编号与原文指纹", field="scoring_material")
    facts = cand.get("facts") or []
    if not isinstance(facts, list):
        raise InvalidInput("facts 必须是数组", field="facts")
    seen: set[str] = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise InvalidInput("每条 fact 必须是对象", field="facts")
        fid = str(fact.get("id") or "").strip()
        text = str(fact.get("text") or "").strip()
        if not fid or not text:
            raise InvalidInput("每条 fact 都必须有 id 和 text", field="facts")
        if fid in seen:
            raise InvalidInput(f"事实编号重复：{fid}", field="facts")
        seen.add(fid)
        fact.setdefault("source", "user")
        fact.setdefault("confidence", "confirmed")
    return cand


def normalise_strategy(raw: dict[str, Any] | None) -> dict[str, Any]:
    """策略版本；阶段必须合法，内容原样保留（不同阶段结构不同）。"""
    if raw is not None and not isinstance(raw, dict):
        raise InvalidInput("策略必须是 JSON 对象")
    stage = str((raw or {}).get("stage") or "")
    if stage not in STAGES:
        raise InvalidInput(f"策略阶段不支持：{stage or '（空）'}", field="stage")
    out = {
        "schema": STRATEGY_SCHEMA,
        "id": "",
        "revision": 0,
        "parent": None,
        "stage": stage,
        "name": str((raw or {}).get("name") or ""),
        "source": str((raw or {}).get("source") or "custom_prompt"),
        "content": (raw or {}).get("content") or {},
        "params": (raw or {}).get("params") or {},
        "requires": list((raw or {}).get("requires") or []),
        "created_at": "",
        "created_by": "",
        "legacy": (raw or {}).get("legacy") or {},
    }
    if out["source"] not in {"builtin", "custom_prompt", "external_skill"}:
        raise InvalidInput("策略来源只支持 builtin/custom_prompt/external_skill", field="source")
    if not isinstance(out["content"], dict):
        raise InvalidInput("策略 content 必须是对象", field="content")
    if stage == "matching" and out["source"] != "external_skill" and "criteria" in out["content"]:
        criteria = out["content"]["criteria"]
        if not isinstance(criteria, str) or not criteria.strip():
            raise InvalidInput("评分标准 criteria 必须是非空文本", field="content.criteria")
    return out
