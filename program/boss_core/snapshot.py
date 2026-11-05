"""有效快照：一次任务/一轮采集到底用哪份资料、哪份方案、哪版策略。

快照是**冻结**的：任务创建时取一次，之后用户改方案不影响它；新任务取新的。
这样"旧任务用旧版本算完"和"新版本立即生效"不矛盾，也能在结果里写清用了什么。
"""

from __future__ import annotations

from typing import Any

from . import conditions
from .errors import InvalidInput, MissingConfiguration, NotFound
from .jsonio import digest, now_iso
from .models import STAGES, c_mode, c_scalar, c_values

#: 学历 → BOSS 平台筛选码（仅用于编译采集条件，不参与判断）。
DEGREE_CODE = {"中专": "208", "大专": "202", "本科": "203", "硕士": "204", "博士": "205"}
#: 求职身份 → 平台经验码。
EXPERIENCE_CODE = {"student": "108", "graduate": "102", "any": "101"}


def candidate_identity(candidate: dict[str, Any]) -> dict[str, Any]:
    """从教育经历里推出"在校/已毕业"和毕业年份；推不出就留 None。"""
    grad = c_scalar((candidate.get("availability") or {}).get("graduation"))
    year = int(str(grad)[:4]) if grad else None
    in_school = None
    if year is not None:
        import datetime as _dt
        in_school = year >= _dt.date.today().year
    return {"graduation_year": year, "in_school": in_school,
            "highest_degree": _highest_degree(candidate)}


def _highest_degree(candidate: dict[str, Any]) -> str | None:
    ranks = {"中专": 1, "中技": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}
    best = None
    for item in candidate.get("education") or []:
        degree = str(item.get("degree") or "")
        if degree in ranks and (best is None or ranks[degree] > ranks[best]):
            best = degree
    return best


def _raw(source: Any, key: str, default: Any = None) -> Any:
    """取用户显式配置的原始值：缺失或 None 才算"没配置"。

    显式 0 / False 原样返回。注意 `0 == False` 在 Python 里成立，所以这里只能
    用 `is None` 判断"没配置"，不能写成 `value or default`。

    旧版本的 `models._merge` 会把模板里没列的 search 键塞进 `extra`，所以顶层没有
    时再往 `extra` 里找一次：老方案里显式写的 0（以及任何值）不能因此被当成没配置。
    """
    value = source.get(key) if isinstance(source, dict) else None
    if value is None and isinstance(source, dict):
        extra = source.get("extra")
        if isinstance(extra, dict):
            value = extra.get(key)
    return default if value is None else value


def _num(source: Any, key: str, default: Any, cast: type = int) -> Any:
    """数值字段统一转换：缺失/None 用默认值；显式 0 保持 0；布尔值不转成数字。

    布尔量单独放行，是因为 `bool` 是 `int` 的子类：`int(False)` 会变成 0，把
    "显式关掉"和"显式填 0"混成一个值。非数字值不静默回退默认值，直接报字段错误，
    免得用户在配置里写错字却被悄悄当成没配。
    """
    value = _raw(source, key)
    if value is None:
        return cast(default)
    if isinstance(value, bool):
        return value
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"{key} 必须是数字（{value!r} 无法转换）", field=key) from exc


def compile_search(plan: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """把方案编译成采集条件（后台采集与面板交互采集共用这一份）。

    城市、行业和规模以已确认的 target 为准；其他维度使用 search.base。
    空值保持为空，不从本人学历/毕业年份推导岗位要求。空参数会被省略，
    因此必须通过 audit 提示平台默认行为，而不能宣称已明确不限。
    """
    target = plan.get("target") or {}
    search = plan.get("search") or {}
    # 显式清空也有效；target 未确认时兼容直接配置的 base。
    explicit = {k: v for k, v in (search.get("base") or {}).items()
                if v is not None}

    base = {
        "city": "",
        "jobType": "",
        "salary": "",
        "experience": [],
        "degree": [],
        "industry": [],
        "scale": [],
        "payType": [],
        "partTime": [],
        "stage": [],
        "pageSize": 15,
    }
    base.update({k: v for k, v in explicit.items() if k in base})
    # Keep old fingerprints unchanged unless a new precision condition is actually set.
    for key in ('position', 'multiBusinessDistrict', 'multiSubway'):
        if explicit.get(key):
            base[key] = explicit[key]
    from boss_filter.validation import normalize_fields, FilterValidationError, audit, option_map, validate_search_context
    employment = target.get("employment_types")
    if c_mode(employment) in ("list", "any"):
        types = set(c_values(employment))
        # 仅从已读取的选项表映射。以后平台新增实习选项即可采用，不猜1902等代码。
        labels = {"full_time": "全职", "part_time": "兼职", "internship": "实习"}
        wanted = labels.get(next(iter(types))) if len(types) == 1 else None
        available = option_map("jobType")
        base["jobType"] = ("0" if c_mode(employment) == "any" or types == {"any"} else
                           next((code for code, name in available.items() if name == wanted), ""))
    for target_key, platform_key in (("industries", "industry"), ("company_scales", "scale")):
        choice = target.get(target_key)
        if c_mode(choice) in ("list", "any"):
            base[platform_key] = c_values(choice)
    cities = c_values(target.get("cities"))
    if c_mode(target.get("cities")) == "any":
        cities = ["100010000"]
    if not cities:
        cities = [base["city"]]
    try:
        from boss_filter.validation import resolve
        city_filters = search.get('city_filters') or {}
        if not isinstance(city_filters, dict):
            raise FilterValidationError('city_filters', city_filters, '需要城市到精细位置条件的对象')
        scoped = {}
        for city, fields in city_filters.items():
            if not isinstance(fields, dict) or set(fields) - {'multiBusinessDistrict', 'multiSubway'}:
                raise FilterValidationError('city_filters', fields, '城市覆盖只接受 multiBusinessDistrict 和 multiSubway')
            code = resolve('city', city)
            if code in scoped:
                raise FilterValidationError('city_filters', city, '同一城市重复配置')
            scoped[code] = fields
        if len(set(cities)) > 1 and any(base.get(k) for k in ('multiBusinessDistrict', 'multiSubway')):
            raise FilterValidationError('city_filters', base, '多城市精细位置请分别写入 search.city_filters，不能共用一份位置')
        bases = [normalize_fields({**base, "city": city, **scoped.get(resolve('city', city), {})}) for city in dict.fromkeys(cities)]
        if set(scoped) - {b['city'] for b in bases}:
            raise FilterValidationError('city_filters', city_filters, '包含不在当前采集城市范围内的条件')
        for item in bases:
            validate_search_context(item)
    except FilterValidationError as exc:
        raise InvalidInput(str(exc), field=exc.field) from exc
    base = bases[0]

    compiled = {
        "base": base,
        "bases": bases,
        "audit": audit(plan, bases),
        "keywords": list(search.get("keywords") or []),
        # 下面每个数值键：缺失/None 用默认值，显式 0 保持 0（0 是"关掉"），见 `_num`。
        "pages_per_query": _num(search, "pages_per_query", 3),
        # 嵌套 `search.pages.max_pages_per_query` 优先，其次顶层同名键；哪边写 0 都算数。
        "max_pages_per_query": _num(_raw(search, "pages") or {}, "max_pages_per_query",
                                    _num(search, "max_pages_per_query", 20)),
        "daily_page_cap": _num(search, "daily_page_cap", 300),
        "cycle_sleep_sec": _num(search, "cycle_sleep_sec", 180),
        "jd_batch": _num(search, "jd_batch", 5),
        "jd_interval": _num(search, "jd_interval", 5.0, float),
        "daily_jd_cap": _num(search, "daily_jd_cap", 1500),
        "match_batch": _num(search, "match_batch", 80),
        "match_force_backlog": _num(search, "match_force_backlog", 60),
        "activity_batch": _num(search, "activity_batch", 4),
        "activity_interval": _num(search, "activity_interval", 8.0, float),
        "activity_min_score": _num(plan.get("runtime") or {}, "min_score", 0),
        "corpus_export_every": _num(search, "corpus_export_every", 40),
    }
    # 单城市沿用已有指纹形状，避免仅升级校验就让原有深页游标从头开始。
    scope = {"base": base} if len(bases) == 1 else {"bases": bases}
    compiled["filters_hash"] = digest({**scope, "keywords": compiled["keywords"]})
    return compiled


def build(ws, *, plan_id: str | None = None, candidate_id: str | None = None) -> dict[str, Any]:
    """组装有效快照；缺必需项时返回的 `missing` 会列出，不在这里抛。

    候选人取谁，按"这个方案钉的是哪一版"来定，**不**优先取全局活动资料：
    否则保存一份新资料，旧方案会悄悄改用新资料打分，"旧任务用旧版本"就不成立了。

    优先级：显式 `candidate_id` > 方案记录的 `candidate_id` > 全局活动资料。
    方案记录的版本不存在时直接报错，不退回活动资料——那正是要避免的静默替换。

    需要"缺了就不许执行"的调用方用 :func:`require`。
    """
    plan = ws.get("plan", plan_id) if plan_id else ws.active_plan()
    if plan is None:
        raise MissingConfiguration("还没有求职方案", field="active_plan",
                                   next_steps=["先 create_plan 并 activate_plan"])
    if candidate_id:
        candidate = ws.get("candidate", candidate_id)
    elif plan.get("candidate_id"):
        try:
            candidate = ws.get("candidate", str(plan["candidate_id"]))
        except NotFound:
            candidate = None
    else:
        candidate = ws.active_candidate()
    if candidate is None:
        raise MissingConfiguration("方案没有关联候选人资料", field="candidate_id",
                                   next_steps=["先保存候选人资料，再创建方案"])

    strategies: dict[str, Any] = {}
    missing: list[str] = []
    for stage in STAGES:
        sid = (plan.get("strategies") or {}).get(stage)
        if not sid:
            strategies[stage] = None
            continue
        try:
            strategies[stage] = ws.get("strategy", str(sid))
        except Exception:  # noqa: BLE001 - 绑定指向不存在的策略，记为缺失
            strategies[stage] = None
            missing.append(f"strategies.{stage}")

    search = compile_search(plan, candidate)
    if not search["keywords"]:
        missing.append("search.keywords")
    if c_mode((plan.get("target") or {}).get("directions")) == "unknown":
        missing.append("target.directions")

    versions = {
        "candidate": f"{candidate.get('id')}#{candidate.get('revision')}",
        "plan": f"{plan.get('id')}#{plan.get('revision')}",
    }
    for stage in STAGES:
        item = strategies.get(stage)
        versions[stage] = f"{item.get('id')}#{item.get('revision')}" if item else None

    body = {
        "schema": "snapshot/v1",
        "built_at": now_iso(),
        "candidate": candidate,
        "plan": plan,
        "strategies": strategies,
        "search": search,
        "identity": candidate_identity(candidate),
        "versions": versions,
        "missing": missing,
        "runtime": plan.get("runtime") or {},
        "thresholds": {
            "admit_min_score": int((plan.get("runtime") or {}).get("min_score") or 0),
            "stage2_min_score": _stage2_threshold(strategies.get("matching")),
            "daily_cap": int((plan.get("runtime") or {}).get("daily_cap") or 0),
        },
    }
    body["fingerprint"] = digest({
        "candidate": digest(candidate),
        "plan": digest(plan),
        "strategies": {k: (digest(v) if v else None) for k, v in strategies.items()},
    })
    body["stages"] = stages_status(plan, strategies, search, candidate)
    return body


def _stage2_threshold(strategy: dict[str, Any] | None) -> int | None:
    """详评门槛：策略里写多少就是多少；没写返回 None（不替用户定）。"""
    if not strategy:
        return None
    value = ((strategy.get("content") or {}).get("thresholds") or {}).get("stage2_min_score")
    return int(value) if isinstance(value, (int, float)) else None


def stages_status(plan: dict[str, Any], strategies: dict[str, Any], search: dict[str, Any],
                  candidate: dict[str, Any]) -> dict[str, Any]:
    """每个阶段现在能不能跑，不能跑缺什么。"""
    out: dict[str, Any] = {}
    ident = candidate_identity(candidate)
    directions = c_mode((plan.get("target") or {}).get("directions")) != "unknown"

    checks = {
        "resume_revision": [("candidate.facts", bool(candidate.get("facts")))],
        "matching": [("target.directions", directions), ("strategies.matching", bool(strategies.get("matching")))],
        "greeting": [("strategies.greeting", bool(strategies.get("greeting"))),
                     ("candidate.facts", bool(candidate.get("facts")))],
        "interview": [("strategies.interview", bool(strategies.get("interview")))],
    }
    for stage, pairs in checks.items():
        missing = [name for name, ok in pairs if not ok]
        out[stage] = {"ready": not missing, "missing": missing}
    if not search.get("keywords"):
        out["matching"].setdefault("notes", []).append("search.keywords")
    if ident["highest_degree"] is None:
        out["matching"].setdefault("notes", []).append("candidate.education")
    return out


def require(ws, *, plan_id: str | None = None, stage: str | None = None) -> dict[str, Any]:
    """取快照并确认可执行；缺项抛 `MissingConfiguration`。"""
    snap = build(ws, plan_id=plan_id)
    if stage:
        state = (snap.get("stages") or {}).get(stage) or {}
        if not state.get("ready"):
            raise MissingConfiguration(
                f"{stage} 阶段还缺：{'、'.join(state.get('missing') or ['未知'])}",
                field=(state.get("missing") or [None])[0],
                next_steps=["补齐后重新预览"],
                detail={"stage": stage, "missing": state.get("missing")})
    return snap


def judge_job(job: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """对外的条件判断入口（供 HTTP/外部 AI 批量调用）。"""
    return conditions.judge(job, snapshot)


def effective_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """两份快照的差异：只报会改变行为的项，给"预览影响"用。"""
    rows: list[dict[str, Any]] = []
    for key in ("candidate", "plan"):
        old, new = before.get(key) or {}, after.get(key) or {}
        for field in _flat_fields(old, new):
            a, b = _dig(old, field), _dig(new, field)
            if a != b:
                rows.append({"scope": key, "field": field, "before": a, "after": b})
    for stage in STAGES:
        old = (before.get("strategies") or {}).get(stage) or {}
        new = (after.get("strategies") or {}).get(stage) or {}
        if (old.get("id"), old.get("revision")) != (new.get("id"), new.get("revision")):
            rows.append({"scope": "strategy", "field": stage,
                         "before": old.get("id"), "after": new.get("id")})
    if (before.get("search") or {}).get("filters_hash") != (after.get("search") or {}).get("filters_hash"):
        rows.append({"scope": "search", "field": "filters_hash",
                     "before": before["search"]["filters_hash"], "after": after["search"]["filters_hash"]})
    return rows


def _flat_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """收集两边所有叶子路径（最多两层），用于比较。"""
    names = set(old) | set(new)
    return sorted(names)


def _dig(data: dict[str, Any], field: str) -> Any:
    value = data.get(field)
    return digest(value) if isinstance(value, (dict, list)) else value
