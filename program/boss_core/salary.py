"""薪资口径：保留原始单位，按方案配置折算后比较。

为什么单独一个模块：旧代码把月薪按 22 天硬折成日薪，再拿实习日薪上限去卡全职岗位，
全职用户会继承实习的上限。这里改成：

1. 解析出的**原始数字、周期、币种**全部保留（`raw` / `period` / `value_min` / `value_max`）；
2. 只在 `compare()` 里折算，折算参数（每月天数、每年月数）来自方案；
3. 认不出的薪资不是 0，按方案里 `salary.unknown`（allow / unknown / block）处理。
"""

from __future__ import annotations

import re
from typing import Any

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT_MULT = {"万": 10000.0, "w": 10000.0, "W": 10000.0, "k": 1000.0, "K": 1000.0, "千": 1000.0}

_MONTHLY_WORDS = ("月", "/月", "每月")
_ANNUAL_WORDS = ("年", "/年", "年薪")
_DAILY_WORDS = ("天", "日", "/天", "/日")
_HOURLY_WORDS = ("小时", "时", "/时")

_NEGOTIABLE = ("面议", "薪资面议", "待遇面议", "面谈")


def parse(text: str | None) -> dict[str, Any] | None:
    """把薪资描述解析成结构化区间；认不出返回 None。

    支持示例：`150-200元/天`、`10-15K`、`8千-1.2万`、`3-5万/月`、`20-30万/年`、`15K·13薪`。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    if any(word in raw for word in _NEGOTIABLE):
        return {"raw": raw, "period": None, "negotiable": True,
                "value_min": None, "value_max": None, "currency": "CNY", "bonus_months": None}

    period = "month"
    if any(word in raw for word in _HOURLY_WORDS):
        period = "hour"
    elif any(word in raw for word in _DAILY_WORDS):
        period = "day"
    elif any(word in raw for word in _ANNUAL_WORDS):
        period = "year"
    elif any(word in raw for word in _MONTHLY_WORDS):
        period = "month"

    body = raw.split("·")[0]
    bonus = None
    m_bonus = re.search(r"(\d+)\s*薪", raw)
    if m_bonus:
        bonus = int(m_bonus.group(1))

    pair = re.search(rf"{_NUM}\s*([万千kKW]?)\s*[-~到至]\s*{_NUM}\s*([万千kKW]?)(?![万千kKW])", body)
    if pair:
        v1, u1, v2, u2 = pair.groups()
        # 「10-15K」的单位写在区间末尾，前半段要继承它；「8千-1.2万」按各自单位算
        if not u1 and u2:
            u1 = u2
        if not u2 and u1:
            u2 = u1
        num1 = float(v1) * _UNIT_MULT.get(u1, 1.0)
        num2 = float(v2) * _UNIT_MULT.get(u2, 1.0)
        low, high = sorted((num1, num2))
    else:
        single = re.search(rf"{_NUM}\s*([万千kKW]?)(?![万千kKW])", body)
        if not single:
            return None
        low = high = float(single.group(1)) * _UNIT_MULT.get(single.group(2), 1.0)

    if low <= 0:
        return None
    return {"raw": raw, "period": period, "negotiable": False,
            "value_min": round(low, 2), "value_max": round(high, 2),
            "currency": "CNY", "bonus_months": bonus}


def to_monthly(parsed: dict[str, Any], salary_conf: dict[str, Any]) -> tuple[float, float] | None:
    """按方案口径折成月薪区间；周期不支持时返回 None（不猜）。"""
    period = parsed.get("period")
    low, high = parsed.get("value_min"), parsed.get("value_max")
    if low is None or high is None:
        return None
    days = float(salary_conf.get("days_per_month") or 22)
    months = float(salary_conf.get("months_per_year") or 12)
    if period == "month":
        return float(low), float(high)
    if period == "day":
        return float(low) * days, float(high) * days
    if period == "year":
        return float(low) / months, float(high) / months
    return None


def gate_monthly(salary_conf: dict[str, Any]) -> tuple[float | None, float | None]:
    """把方案的薪资门槛按它自己的周期折成月薪，**不能**拿日薪数字直接比月薪。"""
    low, high = salary_conf.get("min"), salary_conf.get("max")
    period = salary_conf.get("period") or "month"
    days = float(salary_conf.get("days_per_month") or 22)
    months = float(salary_conf.get("months_per_year") or 12)
    if period == "day":
        return (None if low is None else float(low) * days,
                None if high is None else float(high) * days)
    if period == "year":
        return (None if low is None else float(low) / months,
                None if high is None else float(high) / months)
    return (None if low is None else float(low), None if high is None else float(high))


def compare(job_salary: str | None, salary_conf: dict[str, Any]) -> dict[str, Any]:
    """岗位薪资 vs 方案要求，返回三态判定。

    `salary_conf.compare`：
        top    —— 用岗位区间的**上界**比方案上限（旧行为：400–600 一天的"实习"要挡掉）
        overlap—— 区间有交集就算过（更宽松）

    两边都先折成月薪再比：日薪门槛 × 22 天、年薪 ÷ 12，口径不同也能比较。
    """
    parsed = parse(job_salary)
    gate = {"min": salary_conf.get("min"), "max": salary_conf.get("max")}
    if parsed is None:
        return {"state": "unknown", "reason": "薪资认不出", "parsed": None, "gate": gate}
    if parsed.get("negotiable"):
        return {"state": _unknown_state(salary_conf), "reason": "薪资面议", "parsed": parsed, "gate": gate}

    monthly = to_monthly(parsed, salary_conf)
    if monthly is None:
        return {"state": "unknown", "reason": f"薪资周期 {parsed.get('period')} 暂不支持折算",
                "parsed": parsed, "gate": gate}

    lo_cap, hi_cap = gate_monthly(salary_conf)
    if lo_cap is None and hi_cap is None:
        return {"state": "pass", "reason": "方案未设薪资门槛", "parsed": parsed,
                "monthly": monthly, "gate": gate}

    mode = str(salary_conf.get("compare") or "top")
    job_low, job_high = monthly
    if mode == "top":
        too_low = lo_cap is not None and job_high < lo_cap
        too_high = hi_cap is not None and job_high > hi_cap
    else:
        too_low = lo_cap is not None and job_high < lo_cap
        too_high = hi_cap is not None and job_low > hi_cap

    if too_low:
        return {"state": "fail", "reason": f"薪资偏低（{parsed['raw']}）", "parsed": parsed,
                "monthly": monthly, "gate": gate}
    if too_high:
        return {"state": "fail", "reason": f"薪资偏高（{parsed['raw']}）", "parsed": parsed,
                "monthly": monthly, "gate": gate}
    return {"state": "pass", "reason": "", "parsed": parsed, "monthly": monthly, "gate": gate}


def _unknown_state(salary_conf: dict[str, Any]) -> str:
    """认不出薪资时按方案配置决定三态。"""
    policy = str(salary_conf.get("unknown") or "unknown")
    return {"allow": "pass", "unknown": "unknown", "block": "fail"}.get(policy, "unknown")


def unknown_state(salary_conf: dict[str, Any]) -> dict[str, Any]:
    """暴露给条件判定：没写薪资/面议时该怎么算。"""
    return {"state": _unknown_state(salary_conf),
            "reason": f"薪资未提供（方案设置：{salary_conf.get('unknown')}）"}
