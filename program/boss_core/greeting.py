"""招呼语的有效写作规则：把"固定约束 + 个人策略 + 披露设置"合成一份可读文本。

旧实现有五个来源（固定模板、程序设置、policy.json、批次 md、面板重生成），
谁覆盖谁靠提示词顺序，用户改一处看不到效果。这里改成显式三段合成，并把
冲突**报出来**而不是让模型自己取舍：

1. 系统约束（程序固定，不可被覆盖）：不编经历、不冒用招聘方口吻、必须引用事实编号、长度；
2. 个人策略（可替换）：语气、选材、篇幅偏好；
3. 披露设置：哪些资料允许写进正文（城市、可用时间、薪资、特定事实）。

产物带 `sha` 与逐段指纹，写进审核哈希；同一输入必然得到同一份规则。
"""

from __future__ import annotations

import re
from typing import Any

from . import templates
from .jsonio import digest
from .models import c_mode, c_scalar, c_values

#: 系统约束：任何策略都不能删掉这些。
SYSTEM_RULES = (
    "只写招呼语正文：不要解释、不要引号、不要代码围栏、不要换行。",
    "不得编造经历、公司、时间、数字或证书；每个能力都要能对应下面的某个事实编号。",
    "不得冒用招聘方口吻（不写“我们公司”“我们团队”“欢迎投递”“期待你的加入”）。",
    "不得写薪资要求、期望薪资或联系方式。",
    "用正文里的事实编号引用候选人材料；不要写出编号本身以外的新事实。",
)

#: 禁写表达式（程序做确定性检查，不交给模型）。
BANNED = ("我们公司", "我们团队", "欢迎投递", "期待你的加入", "薪资要求", "期望薪资")

_PHONE = re.compile(r"1[3-9]\d{9}")

#: 这些词出现在同一句里，说明是在"禁止/提醒"，不是在"使用"。
_NEGATION = ("不要", "不得", "不许", "禁止", "禁用", "禁写", "严禁", "避免", "别写",
             "不写", "勿", "切勿", "杜绝", "不可", "不能写", "不许写", "不要写")


def _uses_phrase(text: str, word: str) -> bool:
    """判断 `word` 在文本里是"被使用"还是"被禁止"。

    规则文本里本来就会有「不要写“我们公司”」这种句子；只按出现判断会误报。
    """
    for sentence in re.split(r"[。；;\n]", text):
        if word in sentence and not any(neg in sentence for neg in _NEGATION):
            return True
    return False


def _facts_for(disclosure: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按披露设置筛事实；`all` = 全部已确认事实。"""
    rule = disclosure.get("facts") or {"mode": "all"}
    mode = c_mode(rule)
    if mode == "list":
        allowed = set(c_values(rule))
        return [f for f in facts if str(f.get("id")) in allowed]
    if mode == "unknown":
        return []
    return list(facts)


def _availability_line(candidate: dict[str, Any], disclosure: dict[str, Any],
                       preset: dict[str, Any] | None = None) -> str:
    """可用时间/城市只按披露开关写；没开就不出现。

    "可实习 / 可持续"这类说法由模板预设决定（实习预设才写"可实习"），
    不把某一类求职者的话术套到所有人身上。
    """
    if not disclosure.get("availability", True):
        return ""
    avail = candidate.get("availability") or {}
    label = str((preset or {}).get("availability_month_label") or "可持续")
    parts = []
    start = c_scalar(avail.get("start"))
    if start:
        parts.append(f"到岗时间 {start}")
    days = c_scalar(avail.get("days_per_week"))
    if isinstance(days, (int, float)):
        parts.append(f"每周可到岗 {days:g} 天")
    months = c_scalar(avail.get("months"))
    if isinstance(months, (int, float)):
        parts.append(f"{label} {months:g} 个月")
    return "；".join(parts)


def compose(snapshot: dict[str, Any], *, panel_rule: str = "") -> dict[str, Any]:
    """合成有效写作规则。

    Args:
        snapshot: 有效快照（候选人 + 方案 + 策略）。
        panel_rule: 面板「系统配置 → 招呼语」里用户自己写的那段；空串表示没配。
            它排在系统约束之后、策略之前：人写的规则优先于策略模板，但不能越过红线。

    Returns:
        `{"text","sha","parts","conflicts","max_chars","disclosure","strategy_id"}`。
        有 `conflicts` 时调用方应停在人工确认，而不是直接写稿。
    """
    plan = snapshot.get("plan") or {}
    candidate = snapshot.get("candidate") or {}
    strategy = (snapshot.get("strategies") or {}).get("greeting") or {}
    content = strategy.get("content") or {}
    disclosure = plan.get("disclosure") or {}
    limits = content.get("length") or {}
    max_chars = int(limits.get("max_chars") or 200)
    facts = candidate.get("facts") or []
    allowed = _facts_for(disclosure, facts)

    parts: list[dict[str, Any]] = []
    deduplicated: list[dict[str, str]] = []
    personal_ids = {"preamble", "panel", "personal_prompt", "strategy", "selection"}

    def add(part_id: str, source: str, text: str) -> None:
        if text.strip():
            # 旧迁移会把同一要求同时留在 app_settings 和 style。
            # 只合并被另一来源完整逐行包含的文本，不猜测语义相似或冲突。
            # 行边界避免把「要像学生」误当成「不要像学生」的重复。
            if part_id in personal_ids:
                needle = text.strip().replace("\r\n", "\n")
                for prior in parts:
                    if prior["id"] not in personal_ids:
                        continue
                    haystack = prior["text"].strip().replace("\r\n", "\n")
                    if re.search(r"(?:^|\n)" + re.escape(needle) + r"(?=\n|$)", haystack):
                        deduplicated.append({"id": part_id, "covered_by": prior["id"]})
                        return
            parts.append({"id": part_id, "source": source, "text": text,
                          "chars": len(text), "sha": digest(text)[:12]})

    add("system", "program", "\n".join(f"- {rule}" for rule in SYSTEM_RULES))

    identity = _identity_block(disclosure, allowed)
    add("identity", "candidate", identity)

    # 旧版藏在 writer-prompt.txt 里的固定模板：迁移后作为策略的 preamble 出现，
    # 排在系统约束之后、用户面板规则之前，谁都能读到最后生效的全文。
    add("preamble", "strategy", str(content.get("preamble") or "").strip())

    add("panel", "app_settings", panel_rule.strip())

    # The same personal prompt must reach panel regeneration and background writing.
    from .strategies import context, render
    prompt = render(str(content.get("system") or ""), context(snapshot))
    add("personal_prompt", strategy.get("source") or "builtin", prompt)

    style = str(content.get("style") or content.get("requirements") or "").strip()
    add("strategy", strategy.get("source") or "builtin", style)

    selection = content.get("selection")
    add("selection", "strategy", str(selection or "").strip())

    # 冲突只检查"策略自己说的话"：系统约束里本来就会出现"不要写'我们公司'"这种句子，
    # 事实清单也是允许出现届别/学历的地方。
    claimed = "\n".join(filter(None, [str(content.get("preamble") or ""), prompt, style,
                                      str(selection or ""), panel_rule]))

    avail = _availability_line(candidate, disclosure,
                               templates.variables(templates.plan_preset_id(plan)))
    if avail:
        add("availability", "plan.disclosure", "可用安排：" + avail)

    facts_text = "\n".join(f"  {f.get('id')}: {f.get('text')}" for f in allowed)
    add("facts", "candidate", "# 可引用事实\n" + facts_text + "\n引用格式：正文末尾加「事实: F1,F2」")

    add("length", "program", f"正文不超过 {max_chars} 个字符（含标点）。")

    text = "\n\n".join(part["text"] for part in parts)
    conflicts = detect_conflicts(claimed, candidate, facts)
    return {
        "text": text,
        "sha": digest(text),
        "parts": parts,
        "conflicts": conflicts,
        "max_chars": max_chars,
        "disclosure": disclosure,
        "strategy_id": strategy.get("id"),
        "deduplicated": deduplicated,
        "facts": [f.get("id") for f in allowed],
    }


def _identity_block(disclosure: dict[str, Any], facts: list[dict[str, Any]]) -> str:
    """身份只从候选人事实里取；没有就不写，不默认成学生。"""
    if not facts:
        return ""
    return "以下事实是唯一可引用的经历与身份；没写到的不要补。"


def detect_conflicts(text: str, candidate: dict[str, Any], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出策略文本里"说了但事实里没有"的说法。

    只看可验证的硬信号：届别、学历、可工作时长（月）、每周天数、城市。命中即报，不自动修。
    """
    issues: list[dict[str, Any]] = []
    blob = " ".join(str(f.get("text") or "") for f in facts) + " " + str(
        (candidate.get("availability") or {}).get("graduation") or "")

    for year in sorted(set(re.findall(r"(20\d{2})\s*届", text))):
        if year not in blob:
            issues.append({"code": "strategy_claims_unconfirmed_fact", "field": "graduation",
                           "message": f"策略提到「{year} 届」，但候选人事实里没有这个届别",
                           "evidence": f"{year} 届"})

    degree = re.search(r"(本科|硕士|博士|大专)", text)
    if degree:
        word = degree.group(1)
        if word not in blob and not any(word in str(e.get("degree") or "")
                                        for e in candidate.get("education") or []):
            issues.append({"code": "strategy_claims_unconfirmed_fact", "field": "education",
                           "message": f"策略提到「{word}」，但候选人资料里没有这个学历",
                           "evidence": word})

    months = re.search(r"(\d{1,2})\s*个月", text)
    avail_months = c_scalar((candidate.get("availability") or {}).get("months"))
    if months and avail_months != int(months.group(1)):
        issues.append({"code": "strategy_claims_unconfirmed_fact", "field": "availability.months",
                       "message": f"策略写「{months.group(1)} 个月」，与候选人资料"
                                  f"（{avail_months if avail_months is not None else '未填'}）不一致"})

    if any(_uses_phrase(text, word) for word in BANNED):
        hit = next(word for word in BANNED if _uses_phrase(text, word))
        issues.append({"code": "strategy_contains_banned_phrase", "field": "greeting.strategy",
                       "message": f"策略里含禁写表达「{hit}」", "evidence": hit})

    if _PHONE.search(text):
        issues.append({"code": "strategy_contains_contact", "field": "greeting.strategy",
                       "message": "策略里含手机号"})
    return issues


def validate_text(text: str, *, max_chars: int, fact_ids: list[str]) -> dict[str, Any]:
    """对生成出来的正文做确定性检查（不调模型）。"""
    issues: list[dict[str, Any]] = []
    body = (text or "").strip()
    if not body:
        issues.append({"code": "empty", "message": "正文为空"})
    if len(body) > max_chars:
        issues.append({"code": "length_out_of_range",
                       "message": f"正文 {len(body)} 字，超过上限 {max_chars}"})
    if any(word in body for word in BANNED):
        issues.append({"code": "banned_phrase", "message": "含禁写表达"})
    if _PHONE.search(body):
        issues.append({"code": "contact", "message": "含手机号"})
    if re.search(r"\{\{|\}\}|<[^>]+>|待填", body):
        issues.append({"code": "placeholder", "message": "含占位符"})
    used = [x.strip() for x in re.findall(r"事实[:：]\s*([FfPp0-9,，\s]+)", body)]
    cited = [x.strip() for chunk in used for x in re.split(r"[,，\s]+", chunk) if x.strip()]
    unknown = [x for x in cited if x not in fact_ids]
    if unknown:
        issues.append({"code": "unknown_fact_id", "message": f"引用了不存在的事实：{'、'.join(unknown)}"})
    return {"ok": not issues, "issues": issues, "cited": cited, "chars": len(body)}
