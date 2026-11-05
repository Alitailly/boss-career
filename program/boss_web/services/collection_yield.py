"""采集效果：新增 JD 主指标、查询组合覆盖、低新增事件、可转交 AI 的摘要。

口径先说清楚，否则数字会骗人：

- **新增** ＝ `first_seen_at` 落在窗口内。`fetched_at` 是"最近一次见到"，重抓会覆盖它，
  所以它永远不能当新增用。
- **首次拿到正文** ＝ `jd_first_at` 落在窗口内。它和"新增岗位"是两件事：老卡片补全正文
  也算这一项，但不算新增。
- **再次见到** ＝ `fetched_at` 在窗口内且 `first_seen_at` 更早。这是重复访问，不是收益。
- **重复比例** ＝ 本窗口 `fetch_pages` 的 (raw - inserted) / raw。
- 关键词维度的收益**没有**归属记录（采集器只按词记页码，不按词记新增），
  所以这里如实标 `attribution: "not_recorded"`，不编造按词新增。

低新增判据是**可解释的**，不是"少于某个神奇数字就报警"：先看有没有在正常采集
（页数、报错），再看新增/页，最后区分"范围覆盖充分"与"执行异常"。启动期不判。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from boss_core.jsonio import append_jsonl, read_json, read_jsonl

#: 判低新增的默认判据。个人方案可以用 `collection.low_yield` 覆盖其中任意一项。
#:
#: 口径（2026-11-02 拍板）：**看"可匹配 JD 正文新增"，不看岗位卡片数**。
#: 卡片多但一条正文都没拿到，等于零可发岗位——那正是最该报警的情况，
#: 不能因为"卡片在涨"就判成有收益。
DEFAULT_LOW_YIELD: dict[str, float] = {
    #: 少于这么多页，说什么都不算数（抓一两页就下结论最误导）
    "min_pages": 10,
    #: 窗口短于这么多小时也不判（"刚试采一轮"不该被当成"长期没新增"）
    "min_hours": 3,
    #: 每页拿到的正文数低于这个算"收益偏低"
    "low_per_page": 0.3,
    #: 库内岗位少于这个数、且页数也不够时，只提示"还在启动期"，不报警
    "startup_jobs": 200,
}
#: 兼容旧名字（外部脚本/文档里引用过）
MIN_PAGES = DEFAULT_LOW_YIELD["min_pages"]
STARTUP_JOBS = DEFAULT_LOW_YIELD["startup_jobs"]
LOW_PER_PAGE = DEFAULT_LOW_YIELD["low_per_page"]
#: 「调整前后」要比的指标：只比口径相同、能直接相减的计数与比率。
COMPARE_KEYS = ("new_jobs", "new_with_jd", "jd_first", "seen_again",
                "pages", "per_page", "dup_ratio")

EVENTS = "runs/collection-events.jsonl"
STATE = "runs/collector-state.json"
#: 采集器把心跳写在另一个文件里（`collector.heartbeat`）。只读 STATE 拿不到 `at`，
#: 于是"采集器在跑"永远显示成没跑——面板和摘要都因此说错过。
HEARTBEAT = "runs/collector-heartbeat.json"


def _now_text(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() if now is None else now))


def _db():
    import boss_db

    return boss_db.acquire()


def _count(sql: str, params: tuple = ()) -> int:
    row = _db().execute(sql, params).fetchone()
    return int((row[0] if row else 0) or 0)


def summary(root: Path | None, *, hours: int = 24, now: float | None = None) -> dict[str, Any]:
    """窗口内的采集效果。窗口按本机时区、以 `hours` 小时折算。"""
    now = time.time() if now is None else now
    hours = max(1, min(24 * 30, int(hours or 24)))
    start_ts = now - hours * 3600
    start = _now_text(start_ts)
    today = _now_text(now)[:10]

    new_jobs = _count("SELECT COUNT(*) FROM jobs WHERE first_seen_at >= ? AND first_seen_at <> ''",
                      (start,))
    new_with_jd = _count(
        "SELECT COUNT(*) FROM jobs WHERE first_seen_at >= ? AND first_seen_at <> '' "
        "AND COALESCE(job_desc, '') <> ''", (start,))
    jd_first = _count("SELECT COUNT(*) FROM jobs WHERE jd_first_at >= ? AND jd_first_at <> ''",
                      (start,))
    seen_again = _count(
        "SELECT COUNT(*) FROM jobs WHERE fetched_at >= ? AND first_seen_at <> '' "
        "AND first_seen_at < ?", (start, start))
    total = _count("SELECT COUNT(*) FROM jobs")
    with_jd = _count("SELECT COUNT(*) FROM jobs WHERE COALESCE(job_desc, '') <> ''")
    # 升级前入库的行没有"首次见到"（空串）。它们既不能算新增，也不能被当成 0，
    # 单列一项让面板说明"这部分不计入新增"。
    unattributed = _count("SELECT COUNT(*) FROM jobs WHERE COALESCE(first_seen_at, '') = ''")

    pages = _count("SELECT COUNT(*) FROM fetch_pages WHERE fetched_at >= ?", (start,))
    raw = _count("SELECT COALESCE(SUM(raw_count),0) FROM fetch_pages WHERE fetched_at >= ?",
                 (start,))
    inserted_pages = _count(
        "SELECT COALESCE(SUM(inserted_count),0) FROM fetch_pages WHERE fetched_at >= ?", (start,))
    empty_pages = _count(
        "SELECT COUNT(*) FROM fetch_pages WHERE fetched_at >= ? AND COALESCE(raw_count,0) = 0",
        (start,))
    duplicates = max(0, raw - inserted_pages)
    dup_ratio = round(duplicates / raw, 3) if raw else 0.0

    by_day = _trend(now=now)

    state = _state(root)
    hb = _heartbeat(root)
    last_success = str(hb.get("at") or state.get("at") or "")
    result = {
        "ok": True, "window_hours": hours, "window_start": start, "as_of": _now_text(now),
        "today": today,
        "new_jobs": new_jobs, "new_with_jd": new_with_jd, "jd_first": jd_first,
        "seen_again": seen_again, "duplicates": duplicates, "dup_ratio": dup_ratio,
        "pages": pages, "raw": raw, "empty_pages": empty_pages,
        "per_page": round(new_jobs / pages, 3) if pages else 0.0,
        "per_page_jd": round(new_with_jd / pages, 3) if pages else 0.0,
        "total_jobs": total, "with_jd": with_jd,
        "unattributed_jobs": unattributed,
        "by_day": by_day,
        "collector": {"running": _collector_running(hb or state, now),
                      "last_at": last_success,
                      "query": str(hb.get("query") or state.get("query") or ""),
                      "note": str(hb.get("note") or state.get("note") or ""),
                      "page_cap": int(state.get("page_cap") or 0)},
        "attribution": "keyword 维度的新增没有记录，只有页码游标；见 coverage",
    }
    limits = thresholds(root)
    result["limits"] = limits
    result["phase"] = "startup" if total < int(limits["startup_jobs"]) else "running"
    result["low"] = judge(result, limits)
    return result


def _trend(days: int = 7, now: float | None = None) -> list[dict[str, Any]]:
    """最近 `days` 天的新增与首次正文，**两条事件轴各按自己的日期聚合**。

    以前按 `first_seen_at` 分组、再要求 `jd_first_at` 与它同一天：11 月 1 日入库、
    11 月 2 日才拿到正文的老卡片，会记到 11 月 1 日且 `jd_first=0`，11 月 2 日
    整天没有行——"首次取得正文"的按日趋势因此长期漏记。现在分别聚合再合并，
    缺的日期补 0，和 `summary()` 的 24 小时汇总用同一口径。
    """
    now = time.time() if now is None else now
    days = max(1, min(30, int(days or 7)))
    today = _now_text(now)[:10]
    start_day = time.strftime("%Y-%m-%d", time.localtime(now - (days - 1) * 86400))
    buckets: dict[str, dict[str, Any]] = {}
    for offset in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
        buckets[day] = {"date": day, "new_jobs": 0, "jd_first": 0}

    def _fill(column: str, target: str) -> None:
        rows = _db().execute(
            f"SELECT substr({column},1,10) AS d, COUNT(*) FROM jobs "
            f"WHERE COALESCE({column},'') >= ? AND {column} <= ? GROUP BY d",
            (start_day, today + " 23:59:59"),
        ).fetchall()
        for row in rows:
            day = str(row[0])
            bucket = buckets.get(day)
            if bucket is None:
                bucket = buckets[day] = {"date": day, "new_jobs": 0, "jd_first": 0}
            bucket[target] += int(row[1] or 0)

    _fill("first_seen_at", "new_jobs")
    _fill("jd_first_at", "jd_first")
    return [buckets[day] for day in sorted(buckets)]


def _state(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    try:
        value = read_json(Path(root) / STATE, {})
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _heartbeat(root: Path | None) -> dict[str, Any]:
    """心跳文件：`at` / `query` / `note` / `backlog` 只在这里。

    和 STATE 分开读，不合并成一个字典：心跳里的 `exhausted` 是个**数量**，
    STATE 里的同名字段是**列表**，合并会把覆盖统计弄坏。
    """
    if root is None:
        return {}
    try:
        value = read_json(Path(root) / HEARTBEAT, {})
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _collector_running(state: dict[str, Any], now: float) -> bool:
    """心跳 10 分钟内算在跑（和面板同一口径）。"""
    text = str(state.get("at") or "")
    if not text:
        return False
    try:
        stamp = time.mktime(time.strptime(text[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return False
    return 0 <= now - stamp <= 600


def thresholds(root: Path | None = None) -> dict[str, float]:
    """低新增判据：默认值 + 个人方案里的覆盖（`plan.search.extra.low_yield`）。

    判据写死在代码里，用户就没法按自己的搜索范围调；全放开又容易被一个错配置
    把报警关掉。所以：只接受数字、只在合理区间内生效，其余一律回退默认值。

    配置位置是方案的 `search.extra.low_yield`——方案里的未知键会被收进各段的
    `extra` 里，这是唯一能存住自定义判据又不改 schema 的地方。
    """
    limits = dict(DEFAULT_LOW_YIELD)
    if root is None:
        return limits
    try:
        from boss_core.workspace import Workspace

        plan = Workspace(root).active_plan()
    except Exception:  # noqa: BLE001 - 没有工作区/没激活方案就用默认值
        return limits
    search = ((plan or {}).get("search") or {})
    custom = (search.get("extra") or {}).get("low_yield") or {}
    if not isinstance(custom, dict):
        return limits
    for key, low, high in (("min_pages", 1, 1000), ("min_hours", 1, 720),
                           ("low_per_page", 0.0, 100.0), ("startup_jobs", 0, 1_000_000)):
        value = custom.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
            limits[key] = value
    return limits


def judge(metrics: dict[str, Any], limits: dict[str, float] | None = None) -> dict[str, Any]:
    """低新增判据：先看观察量够不够，再看**正文**收益，最后才谈"范围挖得差不多"。

    三件容易混的事分开说：

    * 观察量不够（页数少 / 窗口短）→ 不下结论；
    * 卡片在进但正文是 0 → `cards_without_jd`，该去查补正文那段，不是"范围挖完了"；
    * 正文也进得少 → `low_per_page`，这才轮到"要不要换词/加城市"。
    """
    limits = {**DEFAULT_LOW_YIELD, **(limits or {})}
    hours = float(metrics.get("window_hours") or 0)
    if hours < float(limits["min_hours"]):
        return {"low": False, "why": ["short_window"],
                "detail": f"窗口只有 {hours:g} 小时（少于 {limits['min_hours']:g} 小时），"
                          "先多观察一段时间"}
    pages = int(metrics.get("pages") or 0)
    if pages < int(limits["min_pages"]):
        startup = int(metrics.get("total_jobs") or 0) < int(limits["startup_jobs"])
        return {"low": False, "why": ["insufficient_pages"],
                "detail": f"窗口内只抓了 {pages} 页（少于 {limits['min_pages']:.0f} 页），"
                          "下结论太早了" + ("；库内岗位还少，属于启动期" if startup else "")}

    reasons: list[str] = []
    new_jobs = int(metrics.get("new_jobs") or 0)
    new_with_jd = int(metrics.get("new_with_jd") or 0)
    per_page_jd = new_with_jd / pages if pages else 0.0
    if new_with_jd == 0:
        reasons.append("zero_jd")
        if new_jobs:
            # 卡片在涨、正文为零：这是"抓不到正文"，不是"没有岗位"
            reasons.append("cards_without_jd")
    elif per_page_jd < float(limits["low_per_page"]):
        reasons.append("low_per_page")
    if float(metrics.get("dup_ratio") or 0) >= 0.9:
        reasons.append("high_duplicate")
    if not reasons:
        return {"low": False, "why": [], "detail": ""}
    hints: list[str] = []
    collector = metrics.get("collector") or {}
    if "cards_without_jd" in reasons:
        hints.append("卡片在进但一条正文都没拿到：先看补正文那一段（详情页失败、被拦、任务没跑）")
    if int(metrics.get("empty_pages") or 0):
        hints.append("有空结果页：可能是登录/风控/关键词太窄，先看执行异常")
    if not collector.get("running"):
        hints.append("采集器心跳已停：新增少可能只是没在跑")
    if "high_duplicate" in reasons:
        hints.append("重复比例很高：多半是同一批结果被反复翻页，不是真的没有新岗位")
    return {"low": True, "why": reasons,
            "detail": f"窗口 {metrics.get('window_hours')} 小时：新增 {new_jobs} 个岗位、"
                      f"其中 {new_with_jd} 个拿到正文 / {pages} 页"
                      f"（{round(per_page_jd, 3)} 个正文每页），"
                      f"重复 {metrics.get('duplicates')} 条（{metrics.get('dup_ratio')}）",
            "hints": hints,
            "next": ["看组合覆盖（哪些词/城市还没跑）",
                     "看执行异常（登录、验证、接口报错）",
                     "小规模试采：换同义词或未覆盖城市，比较新增"]}


def coverage(root: Path | None) -> dict[str, Any]:
    """关键词 × 城市的实际覆盖：哪些在跑、跑到第几页、翻完了没有、哪些只是候选。"""
    state = _state(root)
    depth = state.get("query_pages") or {}
    exhausted = {str(x) for x in (state.get("exhausted") or [])}
    keywords: list[dict[str, Any]] = []
    plan = None
    if root is not None:
        try:
            from boss_core.workspace import Workspace

            plan = Workspace(root).active_plan()
        except Exception:  # noqa: BLE001 - 没有工作区就没有"计划里的词"
            plan = None
    planned = [str(x) for x in ((plan or {}).get("search") or {}).get("keywords") or []]
    from boss_core.snapshot import compile_search
    from boss_filter.validation import option_map
    bases = compile_search(plan, {})["bases"] if plan else [{"city": ""}]
    names = option_map("city")
    cities = [names.get(b.get("city"), b.get("city")) for b in bases if b.get("city")]
    base_city = "、".join(cities)
    planned_keys = set()
    for word in planned:
        for base in bases:
            city = base.get("city") or ""
            key = f"{city} | {word}" if len(bases) > 1 else word
            planned_keys.add(key)
            pages = int(depth.get(key) or 0) if isinstance(depth, dict) else 0
            label = f"{names.get(city, city)} · {word}" if len(bases) > 1 else word
            keywords.append({"keyword": label, "query": word, "cursor_key": key, "enabled": True,
                             "depth": pages, "exhausted": key in exhausted,
                             "executed": pages > 0, "city": names.get(city, city)})
    for word in (depth.keys() if isinstance(depth, dict) else []):
        if str(word) not in planned_keys:
            keywords.append({"keyword": str(word), "enabled": False,
                             "depth": int(depth.get(word) or 0),
                             "exhausted": str(word) in exhausted, "executed": True,
                             "city": "",
                             "note": "历史游标，不归因到当前城市组合"})
    return {
        "ok": True,
        "keywords": keywords,
        "enabled_count": len([k for k in keywords if k["enabled"]]),
        "executed_count": len([k for k in keywords if k["executed"]]),
        "exhausted_count": len([k for k in keywords if k["exhausted"]]),
        "cities": cities,
        "search_city": base_city,
        "city_limit": "按城市×关键词逐个搜索，实际覆盖以各组合的游标记录为准",
        "pass_no": int(state.get("pass_no") or 0),
        "query": str(state.get("query") or ""),
        "filters_hash": str(state.get("filters_hash") or ""),
        "running": _collector_running(state, time.time()),
        "attribution": "not_recorded",
        "note": "多城市分别记录游标；历史无城市游标不分摊到各城市。条件改变会清游标，新增JD未按组合归因",
    }


def record_event(root: Path, metrics: dict[str, Any], *, now: float | None = None) -> dict | None:
    """低新增时落一条事件；同一窗口同一天只落一条，避免刷屏。"""
    judgement = metrics.get("low") or {}
    if not judgement.get("low"):
        return None
    now = time.time() if now is None else now
    day = _now_text(now)[:10]
    key = f"low_yield:{day}:{metrics.get('window_hours')}"
    path = Path(root) / EVENTS
    for row in read_jsonl(path):
        if str(row.get("dedupe") or "") == key and str(row.get("kind") or "") != "ack":
            return None
    event = {
        "id": f"ev_{int(now)}_{abs(hash(key)) % 10000:04d}",
        "kind": "low_yield", "status": "pending", "dedupe": key,
        "at": _now_text(now), "window_hours": metrics.get("window_hours"),
        "window_start": metrics.get("window_start"),
        "metrics": {k: metrics.get(k) for k in
                    ("new_jobs", "new_with_jd", "jd_first", "seen_again", "pages",
                     "per_page", "per_page_jd", "duplicates", "dup_ratio", "empty_pages")},
        "reasons": judgement.get("why") or [],
        "detail": judgement.get("detail") or "",
        "next": judgement.get("next") or [],
        "channel": "pending",  # 没有后台 AI 通道时就是"待接续"，不假装已被处理
    }
    append_jsonl(path, event)
    return event


def events(root: Path | None, *, limit: int = 50) -> list[dict[str, Any]]:
    """事件列表（新在前）。`ack` 行只用来标状态，不单独显示。"""
    if root is None:
        return []
    path = Path(root) / EVENTS
    rows = read_jsonl(path)
    acked: dict[str, dict] = {}
    for row in rows:
        if str(row.get("kind") or "") == "ack":
            acked[str(row.get("target") or "")] = row
    items: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("kind") or "") == "ack":
            continue
        item = dict(row)
        mark = acked.get(str(item.get("id") or ""))
        if mark is not None:
            item["status"] = "handled"
            # 处理时留下的"调整前后"对比，面板和 AI 都能直接看
            item["compare"] = compare(mark)
        items.append(item)
    items.reverse()
    return items[:limit]


def ack_event(root: Path | None, event_id: str, *, by: str = "unknown",
              note: str = "", keep: bool | None = None, hours: int | None = None,
              now: float | None = None) -> dict[str, Any]:
    """标记事件已被处理，并**顺手记下「调整前后」的同一组指标**。

    只记「谁处理了」不够：低新增事件的价值在于"改了搜索范围之后有没有变好"。
    所以 ack 时按事件原来的窗口再算一次，把事件当时的数字（before）和现在的
    数字（after）并列存下来，`keep` 记录这次调整是保留还是撤回。

    Args:
        root: 工作区根目录。
        event_id: 事件 id。
        by: 处理人标签（自报，不是身份认证）。
        note: 一句话说明改了什么。
        keep: 调整保留=True / 撤回=False / 还没结论=None。
        hours: 复查窗口；省略用事件自己的窗口。
        now: 时钟（测试用）。

    Returns:
        ``{"ok","id","status","before","after","verdict"}``。
    """
    if root is None:
        raise ValueError("还没有配置工作流目录")
    root = Path(root)
    known = {str(item.get("id")): item for item in events(root, limit=1000)}
    if event_id not in known:
        raise ValueError(f"没有这条采集事件：{event_id}")
    target = known[event_id]
    window = int(hours or target.get("window_hours") or 24)
    before = {key: (target.get("metrics") or {}).get(key) for key in COMPARE_KEYS}
    after: dict[str, Any] = {}
    try:
        current = summary(root, hours=window, now=now)
        after = {key: current.get(key) for key in COMPARE_KEYS}
    except Exception:  # noqa: BLE001 - 复查算不出来不该挡住"已处理"这个事实
        after = {}
    verdict = "unknown" if keep is None else ("keep" if keep else "rollback")
    append_jsonl(root / EVENTS, {
        "kind": "ack", "target": event_id, "status": "handled",
        "at": _now_text(now), "by": str(by or "unknown")[:80],
        "note": str(note or "")[:300],
        "window_hours": window,
        "before": before, "after": after, "verdict": verdict,
    })
    return {"ok": True, "id": event_id, "status": "handled", "before": before,
            "after": after, "verdict": verdict}


def compare(row: dict[str, Any]) -> dict[str, Any]:
    """把一条 ack 的 before/after 变成能看的差值。"""
    before = row.get("before") or {}
    after = row.get("after") or {}
    delta: dict[str, Any] = {}
    for key in COMPARE_KEYS:
        old, new = before.get(key), after.get(key)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            delta[key] = round(float(new) - float(old), 3)
        else:
            delta[key] = None
    return {"before": before, "after": after, "delta": delta,
            "verdict": str(row.get("verdict") or "unknown"),
            "by": str(row.get("by") or ""), "note": str(row.get("note") or ""),
            "at": str(row.get("at") or "")}


def digest(root: Path | None, *, hours: int = 24, now: float | None = None) -> dict[str, Any]:
    """一段可以直接粘给 AI 的采集摘要：含窗口、口径、关键计数和异常。"""
    data = summary(root, hours=hours, now=now)
    cov = coverage(root)
    low = data["low"]
    lines = [
        f"【采集摘要】窗口：最近 {data['window_hours']} 小时（{data['window_start']} 起，"
        f"截至 {data['as_of']}）",
        f"新增岗位：{data['new_jobs']} 个（其中拿到正文 {data['new_with_jd']} 个）；"
        f"首次拿到正文：{data['jd_first']} 个",
        f"再次见到（重复访问）：{data['seen_again']} 个；重复比例 {data['dup_ratio']}"
        f"（{data['duplicates']}/{data['raw']} 条）",
        f"抓取：{data['pages']} 页，空结果页 {data['empty_pages']} 个，"
        f"平均每页新增 {data['per_page']} 个岗位 / {data.get('per_page_jd', 0)} 个正文",
        f"库内：{data['total_jobs']} 个岗位，其中 {data['with_jd']} 个有正文"
        + (f"；另有 {data['unattributed_jobs']} 个升级前入库、首次见到时间未知（不计入新增）"
           if data["unattributed_jobs"] else ""),
        f"采集器：{'在跑' if data['collector']['running'] else '心跳已停'}"
        f"（最近心跳 {data['collector']['last_at'] or '无'}，"
        f"当前词 {data['collector']['query'] or '无'}）",
        f"搜索组合：启用 {cov['enabled_count']} 个，实际跑过 {cov['executed_count']} 个，"
        f"翻完 {cov['exhausted_count']} 个；城市：{cov['search_city'] or '未设置'}"
        f"（{cov['city_limit']}）",
        f"低新增判断：{'是' if low['low'] else '否'}"
        + (f"——{'；'.join(low['why'])}" if low["low"] else f"——{low['detail']}"),
        f"判据：看「可匹配 JD 正文新增」，至少 {data['limits']['min_pages']:.0f} 页、"
        f"窗口 ≥ {data['limits']['min_hours']:.0f} 小时、正文每页 ≥ {data['limits']['low_per_page']}",
    ]
    if low.get("hints"):
        lines.append("可能原因：" + "；".join(low["hints"]))
    if low.get("next"):
        lines.append("建议下一步：" + "；".join(low["next"]))
    lines.append(f"口径说明：{data['attribution']}")
    return {"ok": True, "text": "\n".join(lines), "metrics": data, "coverage": cov,
            "events": events(root, limit=10), "as_of": data["as_of"]}


def sweep(root: Path, *, hours: int = 24, now: float | None = None) -> dict[str, Any]:
    """算一次并（必要时）落事件；面板与计划任务都调这一个入口。"""
    data = summary(root, hours=hours, now=now)
    event = record_event(root, data, now=now)
    return {"ok": True, "metrics": data, "event": event,
            "pending": [e for e in events(root, limit=20) if e.get("status") == "pending"]}
