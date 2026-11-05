"""岗位条件判断：用"候选人 + 求职方案"的有效快照判断单个岗位。

和旧代码的区别：不再写死"硕士/博士不要""出现 8 个月就排除"。这里把岗位条件
（学历、年限、每周天数、可工作时长（月）、届别、城市、工作方式、薪资）解析成结构化值，
再和候选人快照逐项比较，返回 **pass / fail / unknown / skipped** 四态和依据。

四态里 `skipped` 很重要：方案没提这一项（未知）时不参与判断，而不是把岗位判成未知。
只有"方案提了要求、但岗位或候选人数据不够"才是 `unknown`。
"""

from __future__ import annotations

import re
from typing import Any

from . import salary as salary_mod
from . import templates
from .models import c_mode, c_scalar, c_values

#: 学历从低到高；0 表示不限。
_DEGREE_RANK = {"不限": 0, "学历不限": 0, "中专": 1, "中技": 1, "大专": 2, "本科": 3, "硕士": 4, "博士": 5}

_YEARS = {
    "经验不限": (0.0, None), "在校": (0.0, None), "应届": (0.0, None),
    "1年以内": (0.0, 1.0), "1年以下": (0.0, 1.0),
    "1-3年": (1.0, 3.0), "1—3年": (1.0, 3.0),
    "3-5年": (3.0, 5.0), "3—5年": (3.0, 5.0),
    "5-10年": (5.0, 10.0), "5—10年": (5.0, 10.0),
    "10年以上": (10.0, None), "10年及以上": (10.0, None),
}

_MONTHS_IN_JD = re.compile(r"(?:至少|最少|能够|可以|需|须|要求)?\s*(\d{1,2})\s*个月(?:以上)?")
_DAYS_IN_JD = re.compile(r"每?周\s*(\d)\s*天")
_GRAD_YEAR = re.compile(r"(20\d{2})\s*届")
_REMOTE_WORDS = ("远程办公", "远程", "线上办公", "居家办公", "可远程")
_FULLTIME_WORDS = ("全职", "正式员工", "社招")

#: 强实习短语：标题或正文里出现这些，才够判"这是实习岗"。
_INTERN_PHRASES = ("实习岗", "实习生", "实习期")
#: 紧挨在"实习"前面的强信号，如"需实习""接受实习""要求实习"。
_INTERN_STRONG_PREFIX = ("接受", "要求", "必须", "需", "须")
#: 否定语境：这些词修饰的"实习"不是在说岗位性质。
_INTERN_NEGATION = re.compile(r"(?:无需|不需要|不要求|不招|不接受|不考虑|不是|没有|非|无)\s*$")
#: 优先/加分语境：实习只是加分项，不是在招实习岗。
_INTERN_BONUS = ("优先", "加分", "经历者", "经验者")
#: "实习 3 个月""每周 5 天"这类具体的实习安排，说明正文真的在招实习。
_INTERN_ARRANGEMENT = re.compile(
    r"\s*[，,]?\s*(?:(?:至少|最少|满)\s*)?(?:\d{1,2}|[一二三四五六七八九十])\s*个月"
    r"|\s*[，,]?\s*每周\s*\d\s*天")
#: "有实习经历""有相关实习"：在描述候选人的经历，不是在描述岗位。
_INTERN_HAVE = re.compile(r"(?:有|有过|有相关|具备|拥有|有一段)\s*$")

#: unknown 时给用户看的中文字段名，避免只暴露内部 id。
_UNKNOWN_LABELS = {
    "degree": "学历要求",
    "experience": "工作年限",
    "employment": "雇佣类型",
    "availability_months": "可工作时长（月）",
    "availability_days": "每周可到岗天数",
    "graduation": "毕业时间",
    "city": "目标城市",
    "work_mode": "工作方式",
    "salary": "薪资",
}


def _norm_city(text: str) -> str:
    """城市名归一：去掉"市"和空白，便于比较。"""
    return re.sub(r"[市\s]", "", str(text or ""))


def _degree_rank(text: str) -> int | None:
    """岗位学历要求 → 等级；认不出返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    for word, rank in _DEGREE_RANK.items():
        if word and word in raw:
            return rank
    return None


def _experience_span(text: str) -> tuple[float, float | None] | None:
    """岗位经验要求 → (最少年限, 最多年限)；认不出返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw in _YEARS:
        return _YEARS[raw]
    for word, span in _YEARS.items():
        if word in raw:
            return span
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-—~]\s*(\d+(?:\.\d+)?)\s*年", raw)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*年(?:以上|及以上)?", raw)
    if m:
        return float(m.group(1)), None
    return None


def candidate_years(candidate: dict[str, Any]) -> float:
    """候选人可计入"社招年限"的经历月数，实习不算社招年限。"""
    months = 0.0
    for item in candidate.get("experiences") or []:
        if str(item.get("kind") or "") not in {"full_time", "part_time", "freelance"}:
            continue
        months += float(item.get("months") or 0)
    return round(months / 12.0, 2)


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """把数据库字段名和面板字段名收成一套；缺的留空，不猜。"""
    def pick(*names: str) -> str:
        for name in names:
            value = job.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    return {
        "job_id": pick("job_id", "encrypt_job_id"),
        "title": pick("title", "job_name"),
        "company": pick("company", "brand_name"),
        "city": pick("city", "city_name"),
        "salary": pick("salary", "salary_desc"),
        "degree": pick("degree", "job_degree"),
        "experience": pick("experience", "job_experience"),
        "industry": pick("industry", "brand_industry"),
        "scale": pick("scale", "brand_scale_name"),
        "jd": pick("jd", "jd_text", "job_desc"),
        "labels": job.get("job_labels") or "",
    }


def _check(check_id: str, state: str, rule: str, evidence: str = "", **detail: Any) -> dict[str, Any]:
    """一条判定结果。"""
    return {"id": check_id, "state": state, "rule": rule, "evidence": evidence[:200], "detail": detail}


def _availability(plan: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """可用安排：方案里显式设置的优先，否则用候选人资料里的。

    同一个字段只在一处维护：临时方案（比如"这三个月只能每周 3 天"）写在方案里，
    覆盖候选人默认值；方案没写才回落到资料。
    """
    base = dict(candidate.get("availability") or {})
    override = plan.get("availability") or {}
    for key in ("start", "days_per_week", "months", "graduation"):
        choice = override.get(key)
        if c_mode(choice) != "unknown":
            base[key] = choice
    return base


def judge(job: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """判断一个岗位是否值得进入后续流程。

    Args:
        job: 岗位元数据（数据库列名或面板字段名都行）。
        snapshot: `snapshot.build()` 的有效快照，至少要含 `candidate` 与 `plan`。

    Returns:
        `{"result", "checks", "blocking", "unresolved", "summary"}`。
    """
    view = _job_view(job)
    plan = snapshot.get("plan") or {}
    candidate = snapshot.get("candidate") or {}
    target = plan.get("target") or {}
    req = plan.get("requirements") or {}
    avail = _availability(plan, candidate)
    checks: list[dict[str, Any]] = []

    checks.append(_excluded(view, req))
    checks.append(_degree(view, req, candidate))
    checks.append(_experience(view, req, candidate))
    checks.append(_employment(view, target, candidate))
    checks.append(_months(view, avail))
    checks.append(_days_per_week(view, avail))
    checks.append(_graduation(view, avail))
    checks.append(_city(view, target))
    checks.append(_work_mode(view, target))
    checks.append(_salary(view, plan))
    checks.append(_hard_conflicts(view, req))

    real = [c for c in checks if c["state"] != "skipped"]
    blocking = [c["id"] for c in real if c["state"] == "fail"]
    unresolved = [c["id"] for c in real if c["state"] == "unknown"]
    if blocking:
        result = "fail"
    elif unresolved:
        result = "unknown"
    else:
        result = "pass"

    if result == "fail":
        first = next(c for c in real if c["state"] == "fail")
        summary = f"{first['rule']}：{first['evidence'] or '不符合'}"
    elif result == "unknown":
        unknown = [c for c in real if c["state"] == "unknown"]
        first = unknown[0]
        months_label = templates.variables(templates.plan_preset_id(plan)).get(
            "availability_months_label") or _UNKNOWN_LABELS["availability_months"]
        labels: list[str] = []
        for item in unknown:
            label = months_label if item["id"] == "availability_months" else _UNKNOWN_LABELS.get(
                item["id"], item["id"])
            if label not in labels:
                labels.append(label)
        # 未知不能只说"数据不足"：要告诉用户补哪几项，同时保留第一条规则的出处。
        summary = (f"还差{'、'.join(labels)}，补齐后才能判断："
                   f"{first['rule']}（{first['evidence'] or '数据不足'}）")
    else:
        summary = "条件都满足"
    return {"result": result, "checks": checks, "blocking": blocking,
            "unresolved": unresolved, "summary": summary}


def prefilter_reason(job: dict[str, Any], snapshot: dict[str, Any]) -> str:
    """只要一句"为什么不该投"；合格或拿不准都返回空串。给补打分/补采集复用。

    `unknown` 不是硬冲突，先放行、后续补问，不能拿它当拦截理由。
    """
    verdict = judge(job, snapshot)
    if verdict["result"] != "fail":
        return ""
    return verdict["summary"]


# ------------------------------------------------------------------ 各项判定


def _excluded(view: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """个人排除项：公司、标题关键词、正文关键词。"""
    companies = c_values(req.get("excluded_companies"))
    titles = c_values(req.get("excluded_titles"))
    keywords = c_values(req.get("excluded_keywords"))
    if not (companies or titles or keywords):
        return _check("excluded", "skipped", "没有设置排除项")

    company = view["company"]
    for name in companies:
        if name and name in company:
            return _check("excluded", "fail", "公司在排除名单里", company, field="excluded_companies")
    title = view["title"]
    for word in titles:
        if word and word in title:
            return _check("excluded", "fail", "岗位名称命中排除词", title, field="excluded_titles")
    for word in keywords:
        if word and (word in view["jd"] or word in view["title"]):
            return _check("excluded", "fail", "岗位内容命中排除词", word, field="excluded_keywords")
    return _check("excluded", "pass", "不在排除名单里")


def _degree(view: dict[str, Any], req: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """岗位学历要求 vs 候选人最高学历 + 明确的学历排除。"""
    job_rank = _degree_rank(view["degree"])
    blocks = c_values(req.get("degree_block"))

    if job_rank is None:
        return _check("degree", "unknown", "岗位学历要求认不出", view["degree"])

    for word in blocks:
        if word and word in view["degree"]:
            return _check("degree", "fail", "学历要求在排除名单里", view["degree"], field="degree_block")

    my_rank = _candidate_degree_rank(candidate)
    if my_rank is None:
        return _check("degree", "unknown", "候选人最高学历未确认", view["degree"])

    min_choice = req.get("degree_min")
    floor = c_scalar(min_choice)
    floor_rank = _DEGREE_RANK.get(str(floor)) if floor else None
    if c_mode(min_choice) != "unknown" and floor_rank is None:
        return _check("degree", "unknown", "方案里的最低学历写得不认识", str(floor), field="degree_min")

    if job_rank > my_rank:
        return _check("degree", "fail", "岗位要求高于我的学历", view["degree"],
                      job_rank=job_rank, my_rank=my_rank)
    if floor_rank is not None and job_rank < floor_rank:
        return _check("degree", "fail", "岗位学历低于方案要求的底线", view["degree"],
                      job_rank=job_rank, floor_rank=floor_rank)
    return _check("degree", "pass", "学历符合", view["degree"], job_rank=job_rank, my_rank=my_rank)


def _candidate_degree_rank(candidate: dict[str, Any]) -> int | None:
    """候选人最高学历等级；教育经历里取最高，没填返回 None。"""
    ranks = []
    for item in candidate.get("education") or []:
        rank = _degree_rank(str(item.get("degree") or ""))
        if rank is not None:
            ranks.append(rank)
    return max(ranks) if ranks else None


def _experience(view: dict[str, Any], req: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """岗位年限要求 vs 候选人可计入的年限。"""
    span = _experience_span(view["experience"])
    if span is None:
        return _check("experience", "unknown", "岗位经验要求认不出", view["experience"])
    min_years, _max_years = span
    mine = candidate_years(candidate)
    floor = c_scalar(req.get("years_min"))
    if c_mode(req.get("years_min")) == "value" and isinstance(floor, (int, float)):
        min_years = max(min_years, float(floor))
    # 半年容差：差几个月不算硬冲突，交给后续判断
    if min_years > mine + 0.5:
        return _check("experience", "fail", "岗位年限要求超过我的经历", view["experience"],
                      required=min_years, mine=mine)
    return _check("experience", "pass", "年限符合", view["experience"], required=min_years, mine=mine)


def _intern_phrase(text: str, start: int) -> str:
    """`start` 处的"实习"属于哪个强短语；不是强短语返回空串。"""
    for phrase in _INTERN_PHRASES:
        if text.startswith(phrase, start):
            return phrase
    prefix = text[max(0, start - 2):start]
    for token in _INTERN_STRONG_PREFIX:
        if prefix.endswith(token):
            return f"{token}实习"
    return ""


def _intern_is_bonus(prefix: str, suffix: str) -> bool:
    """判断这个"实习"是否只是在说"有实习经历优先"之类的加分项。"""
    if any(word in prefix[-4:] or word in suffix[:6] for word in _INTERN_BONUS):
        return True
    return bool(_INTERN_HAVE.search(prefix))


def _intern_evidence(text: str, *, title: bool = False) -> tuple[list[str], list[str]]:
    """找文本里的实习证据，返回 (强证据, 弱证据)。

    - 强证据：实习岗 / 实习生 / 实习期 / 需实习 / 接受实习，或正文里的
      "实习 3 个月""每周 5 天"这类具体安排；标题里的"实习"也算，
      因为标题直接说明招聘身份。
    - 弱证据：正文里单独出现的"实习"，可能只是顺带一提，不足以判成实习岗。
    - "有实习经历优先""无需实习""非实习"等语境直接剔除，不算证据。
    """
    strong: list[str] = []
    weak: list[str] = []
    raw = str(text or "")
    for match in re.finditer("实习", raw):
        start = match.start()
        prefix = raw[max(0, start - 8):start]
        suffix = raw[match.end():match.end() + 12]
        if _INTERN_NEGATION.search(prefix):
            continue
        if _intern_is_bonus(prefix, suffix):
            continue
        phrase = _intern_phrase(raw, start)
        if not phrase:
            arrangement = _INTERN_ARRANGEMENT.match(suffix)
            if arrangement:
                phrase = ("实习" + arrangement.group(0).strip())[:20]
        if phrase:
            strong.append(phrase)
        elif title:
            strong.append("实习")
        else:
            weak.append("实习")
    return strong, weak


def _fulltime_evidence(text: str) -> list[str]:
    """找全职证据；"非全职""不招全职"这类否定不算。"""
    raw = str(text or "")
    hits: list[str] = []
    for word in _FULLTIME_WORDS:
        for match in re.finditer(re.escape(word), raw):
            prefix = raw[max(0, match.start() - 3):match.start()]
            if re.search(r"(?:非|不招|不是|不要|不要求|无)\s*$", prefix):
                continue
            hits.append(word)
    return hits


def _employment(view: dict[str, Any], target: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """雇佣类型：只认真实的实习/全职证据，不把"应届""有实习经历"当实习岗。

    "应届""在校""校招"是身份/招聘渠道，不是实习雇佣关系；判 fail 必须有
    "实习生""实习岗""需实习"这类明确证据。证据冲突（标题写实习生、正文写全职）
    或只有弱信号时返回 unknown，交给后续补问。
    """
    wanted = c_values(target.get("employment_types"))
    if not wanted or "any" in wanted:
        return _check("employment", "skipped", "方案未限定雇佣类型")

    title_strong, _title_weak = _intern_evidence(view["title"], title=True)
    jd_strong, jd_weak = _intern_evidence(view["jd"])
    # experience 字段本身就在说岗位类型（如"实习"），出现过就算强证据。
    exp_strong, _exp_weak = _intern_evidence(view["experience"], title=True)
    intern_strong = list(dict.fromkeys(title_strong + jd_strong + exp_strong))
    intern_weak = list(dict.fromkeys(jd_weak))
    full = list(dict.fromkeys(
        _fulltime_evidence(view["title"]) + _fulltime_evidence(view["jd"]) + _fulltime_evidence(view["experience"])))

    if intern_strong and full:
        return _check("employment", "unknown", "岗位同时写了实习和全职，雇佣类型冲突",
                      f"实习：{'/'.join(intern_strong[:2])}；全职：{'/'.join(full[:2])}")
    if intern_strong:
        if "internship" not in wanted:
            return _check("employment", "fail", "这是实习岗，方案不要实习",
                          view["title"] or intern_strong[0])
        return _check("employment", "pass", "雇佣类型符合（实习）", view["title"] or intern_strong[0])
    if intern_weak and full:
        return _check("employment", "unknown", "岗位提到实习又说全职，雇佣类型存疑",
                      f"实习：{'/'.join(intern_weak[:2])}；全职：{'/'.join(full[:2])}")
    if full:
        if "full_time" not in wanted:
            return _check("employment", "fail", "这是全职岗，方案只要实习", view["title"] or full[0])
        return _check("employment", "pass", "雇佣类型符合（全职）", view["title"] or full[0])
    if intern_weak:
        return _check("employment", "unknown", "岗位只提到实习，但看不出是不是实习岗", intern_weak[0])
    return _check("employment", "pass", "雇佣类型符合", view["title"])


def _months(view: dict[str, Any], avail: dict[str, Any]) -> dict[str, Any]:
    """岗位要求的可工作时长 vs 候选人能提供几个月。

    岗位写了硬要求、但候选人没填时是 `unknown`，不是 `skipped`：`skipped`
    只表示"岗位没这个要求"或"确实不适用"。
    """
    found = [int(x) for x in _MONTHS_IN_JD.findall(view["jd"])]
    months = avail.get("months")
    if c_mode(months) != "value" or not isinstance(c_scalar(months), (int, float)):
        if found:
            need = max(found)
            return _check("availability_months", "unknown",
                          f"岗位要求至少 {need} 个月，需要你补充可工作时长（月）",
                          f"至少 {need} 个月", required=need, field="availability.months",
                          missing="months")
        return _check("availability_months", "skipped", "岗位没写工作时长")
    wanted = c_scalar(months)
    if not found:
        return _check("availability_months", "skipped", "岗位没写工作时长", "", mine=wanted)
    need = max(found)
    if need > float(wanted):
        return _check("availability_months", "fail", "岗位要求的时长超过我能提供的", f"{need} 个月",
                      required=need, mine=wanted)
    return _check("availability_months", "pass", "工作时长满足", f"{need} 个月", required=need, mine=wanted)


def _days_per_week(view: dict[str, Any], avail: dict[str, Any]) -> dict[str, Any]:
    """岗位要求的每周到岗天数 vs 候选人能到几天。

    和 `_months` 一样：岗位写了要求、候选人没填 → `unknown`。
    """
    found = [int(x) for x in _DAYS_IN_JD.findall(view["jd"])]
    days = avail.get("days_per_week")
    if c_mode(days) != "value" or not isinstance(c_scalar(days), (int, float)):
        if found:
            need = max(found)
            return _check("availability_days", "unknown",
                          f"岗位要求每周 {need} 天，需要你补充每周可到岗天数",
                          f"每周 {need} 天", required=need, field="availability.days_per_week",
                          missing="days_per_week")
        return _check("availability_days", "skipped", "岗位没写每周天数")
    wanted = c_scalar(days)
    if not found:
        return _check("availability_days", "skipped", "岗位没写每周天数", "", mine=wanted)
    need = max(found)
    if need > float(wanted):
        return _check("availability_days", "fail", "岗位要求的每周天数超过我能到的", f"每周 {need} 天",
                      required=need, mine=wanted)
    return _check("availability_days", "pass", "每周天数满足", f"每周 {need} 天", required=need, mine=wanted)


def _graduation(view: dict[str, Any], avail: dict[str, Any]) -> dict[str, Any]:
    """届别要求 vs 毕业年份。"""
    grad = c_scalar(avail.get("graduation"))
    if not grad:
        return _check("graduation", "skipped", "还没填毕业时间")
    years = [int(x) for x in _GRAD_YEAR.findall(view["jd"] + view["experience"] + view["degree"])]
    if not years:
        return _check("graduation", "skipped", "岗位没写届别", "", mine=str(grad))
    my_year = int(str(grad)[:4])
    if my_year not in years:
        return _check("graduation", "fail", "届别不符", f"要求 {'/'.join(map(str, years))} 届",
                      required=years, mine=my_year)
    return _check("graduation", "pass", "届别符合", f"{my_year} 届")


def _city(view: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """城市白名单与异地接受度。"""
    cities = c_values(target.get("cities"))
    mode = c_mode(target.get("cities"))
    if mode == "unknown":
        return _check("city", "skipped", "还没设置目标城市")
    if mode == "any":
        return _check("city", "pass", "城市不限", view["city"])
    if not cities:
        return _check("city", "skipped", "还没设置目标城市")
    job_city = _norm_city(view["city"])
    if not job_city:
        return _check("city", "unknown", "岗位没给城市", "")
    from boss_filter.validation import option_map
    names = option_map("city")
    if "100010000" in cities or "全国" in cities:
        return _check("city", "pass", "城市不限", view["city"])
    wanted = {_norm_city(names.get(c, c)) for c in cities}
    if job_city in wanted:
        return _check("city", "pass", "城市在目标内", view["city"])
    relocation = c_scalar(target.get("accepts_relocation"))
    if relocation is True:
        return _check("city", "pass", "接受异地线下", view["city"], relocation=True)
    if relocation is False:
        return _check("city", "fail", "不在目标城市且不接受异地", view["city"])
    return _check("city", "unknown", "不在目标城市，且没设置是否接受异地", view["city"])


def _work_mode(view: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """工作方式：方案明确要远程/现场时，岗位描述得对得上。"""
    modes = c_values(target.get("work_modes"))
    if not modes:
        return _check("work_mode", "skipped", "还没设置工作方式")
    jd = view["jd"]
    job_remote = any(word in jd for word in _REMOTE_WORDS)
    if job_remote and "remote" not in modes:
        return _check("work_mode", "fail", "岗位可远程，但方案不要远程", "远程")
    if not job_remote and "remote" in modes and "onsite" not in modes:
        return _check("work_mode", "unknown", "方案只要远程，岗位没写是否可远程", view["city"])
    return _check("work_mode", "pass", "工作方式符合", "远程" if job_remote else "现场")


def _salary(view: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """薪资：口径来自方案，未知按方案配置处理。"""
    conf = plan.get("salary") or {}
    result = salary_mod.compare(view["salary"], conf)
    state = result["state"]
    if state == "pass" and result.get("reason"):
        return _check("salary", "pass", result["reason"], view["salary"], **{"parsed": result.get("parsed")})
    if state == "pass":
        return _check("salary", "pass", "薪资符合", view["salary"])
    if state == "unknown":
        return _check("salary", "unknown", result.get("reason") or "薪资无法判断", view["salary"])
    return _check("salary", "fail", result.get("reason") or "薪资不符合", view["salary"])


def _hard_conflicts(view: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """个人额外否决词：迁移旧的固定冲突（"8 个月""每周 6 天"）时留的口子。

    这是**个人偏好**，不是通用规则：新用户不继承，默认空列表。
    """
    words = c_values(req.get("hard_conflicts"))
    if not words:
        return _check("hard_conflicts", "skipped", "没有额外否决词")
    haystack = view["jd"] + view["title"]
    for word in words:
        if word and re.search(word, haystack):
            return _check("hard_conflicts", "fail", "命中个人否决词", word, field="hard_conflicts")
    return _check("hard_conflicts", "pass", "没有命中否决词")
