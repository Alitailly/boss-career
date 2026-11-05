#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""常驻采集器：按搜索矩阵持续采集 → 补 JD → 补匹配，把队列一直养着。

为什么要常驻：一轮里临时抓 180 个岗位再补 JD、再匹配，光这一段就要 30~40 分钟，
整轮就拖长了。摊到全天以后，发送轮只要「读现成的队列 → 写稿 → 发」。

它做什么（每轮循环）：
  1. 取搜索矩阵里的下一个关键词 × 一组 BOSS 原生筛选条件（在校生/本科/公司规模）
  2. 抓 2 页（不抓详情）
  3. 给最多 `jd_batch` 个没 JD 的岗位补 JD
  4. 给最多 `match_batch` 个「有 JD 但没匹配过」的岗位补匹配
  5. 睡 `cycle_sleep_sec` 再循环

它不做什么：不发消息、不写台账、不改额度、不改稿子。进程停了就停，不留副作用。

安全阀：
  - 当日抓取页数 / 补 JD 数有上限（config/search.json 的 daily_*_cap），超了只睡眠；
  - 程序接口冲突（已有任务在跑）就等，不抢；
  - 每个动作都写日志与心跳 `runs/collector-heartbeat.json`。

用法：
  python collector.py --once          # 只跑一个循环（测试用）
  python collector.py --hours 8       # 常驻，最多跑 8 小时
  python collector.py --dry-run       # 打印将要做的事，不请求
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from workflow_env import CODE_ROOT, workspace_root

HERE = workspace_root()
sys.path.insert(0, str(CODE_ROOT))
import deliver_round_v2 as dr  # noqa: E402

CFG_PATH = HERE / "config" / "search.json"
STATE = HERE / "runs" / "collector-state.json"
HEARTBEAT = HERE / "runs" / "collector-heartbeat.json"
LOG = HERE / "runs" / "collector.log"
ACTIVE_STATE = None

# 判分只在半价时段跑。一批起步后要跑几分钟，所以离下一个全价时段不足这么多分钟就不再新起，
# 免得 13:58 起的一批跨进 14:00 的全价区（2026-11-02）。
MATCH_LEAD_MIN = 10


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cfg() -> dict:
    """搜索条件与运行参数：由有效方案编译，不再直接读 `config/search.json`。

    迁移时旧 `search.json` 已整份写进方案，编译结果与原文件逐键一致，
    所以这一版换源不改变采集行为；改城市/关键词才走"清游标"那条路。
    """
    return dr.core.search_config(dr.snapshot_state())


def load_state() -> dict:
    """读当日状态；**坏文件不静默归零**（C4），直接抛错让上层停。"""
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))  # 坏 JSON 直接抛，不重置计数
    else:
        st = {}
    if not isinstance(st, dict):
        raise ValueError("采集状态必须是 JSON 对象")
    rollover(st)
    st.setdefault("query_pages", {})
    st.setdefault("exhausted", [])
    apply_filters(st, load_cfg())
    return st


def apply_filters(st: dict, cfg: dict) -> bool:
    """搜索条件指纹变了就清空游标：改了城市/关键词后不能接着翻旧条件的页码。

    第一次记录指纹（旧状态文件没有这个键）时**不清空**：旧配置本来就和迁移出来的一致，
    清掉等于把已经翻到的深度扔掉，正是 2026-11-01 要修的那个"每天从第 1 页重爬"。
    """
    fingerprint = str(cfg.get("filters_hash") or "")
    if not fingerprint:
        return False
    if "filters_hash" not in st:
        st["filters_hash"] = fingerprint
        return False
    if st.get("filters_hash") == fingerprint:
        return False
    log(f"搜索条件变了（{st.get('filters_hash')} → {fingerprint}），清空翻页游标与翻完标记")
    st["filters_hash"] = fingerprint
    st["query_pages"] = {}
    st["exhausted"] = []
    st["pass_no"] = 1
    st["query_index"] = 0
    return True


def rollover(st: dict) -> None:
    """安全边界换日；跨午夜在途任务仍属于原任务，不能因清计数而重复启动。

    2026-11-01：**翻页游标跨天保留**，只把当天的用量计数清零。
    依据（查 fetch_pages）：11-01 从第 1 页重爬，前 6 页只新增 2 个岗位（0.33/页）；
    而 10-31 同一关键词爬到第 7~20 页时每页新增 3~4 个。每天从第 1 页重走，
    页数全花在已经有记录的页上，这才是"采集慢"的主因。游标留着，第二天接着往下翻。
    """
    day = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    if st.get("date") != day:
        inflight = {k: v for k, v in st.items() if k.endswith("_task") and v}
        keep = {k: st.get(k) for k in ("query_pages", "exhausted", "query_index", "pass_no")}
        st.clear()
        st.update(date=day, pages=0, jd=0, jd_attempts=0, cycle=0,
                  query_index=int(keep.get("query_index") or 0),
                  query_pages=dict(keep.get("query_pages") or {}),
                  exhausted=list(keep.get("exhausted") or []),
                  pass_no=int(keep.get("pass_no") or 1), **inflight)


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def _pricing_now() -> str:
    """此刻是全价还是半价。判分只允许在半价（off）时段跑，心跳里也照实写。"""
    try:
        return "peak" if dr.is_peak_pricing() else "off"
    except Exception:  # noqa: BLE001 - 判不出来就别谎报半价
        return "unknown"


def heartbeat(st: dict, note: str = "") -> None:
    HB = HEARTBEAT
    HB.parent.mkdir(parents=True, exist_ok=True)
    HB.write_text(json.dumps({
        "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date": st.get("date"), "pages": st.get("pages"), "jd": st.get("jd"),
        "cycle": st.get("cycle"), "query_index": st.get("query_index"),
        "exhausted": len(st.get("exhausted") or []), "note": note,
        # 当前词的翻到第几页 / 第几轮重走：用来判断"今天抓的页是不是新页"
        "depth": int((st.get("query_pages") or {}).get(str(st.get("query") or "")) or 0),
        "pass_no": st.get("pass_no"),
        # 给网页工作流面板读的：待匹配积压、匹配任务是否在跑、当前搜索词
        "backlog": st.get("backlog"), "match_running": bool(st.get("match_task")),
        "query": st.get("query"),
        # peak=全价时段（此刻不判分）/ off=半价时段（此刻允许判分）
        "pricing": _pricing_now(),
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def put_search(query: str, cfg: dict, start_page: int, pages: int) -> dict:
    """写搜索条件（扁平结构；GET 是嵌套的，别直接回写）。"""
    b = cfg.get("base") or {}
    body = {
        "query": query,
        "city": b.get("city") or "",
        "jobType": b.get("jobType") or "",
        "salary": b.get("salary") or "",
        "experience": list(b.get("experience") or []),
        "degree": list(b.get("degree") or []),
        "industry": list(b.get("industry") or []),
        "scale": list(b.get("scale") or []),
        "payType": list(b.get("payType") or []),
        "partTime": list(b.get("partTime") or []),
        "stage": list(b.get("stage") or []),
        "position": list(b.get('position') or []),
        "multiBusinessDistrict": b.get('multiBusinessDistrict') or '',
        "multiSubway": b.get('multiSubway') or '',
        "pageSize": int(b.get("pageSize") or 15),
        "encrypt_expect_id": "",
        "max_pages": int(pages),
        "start_page": int(start_page),
        "page_interval": 4.0,
        "use_search": True,
        "fetch_details": False,
        "detail_interval": 3.0,
        "store_blacklisted": True,
        "expect_kind": "", "mix_expect_type": "", "expect_info": "", "expect_name": "",
    }
    return dr.api("PUT", "/api/collection/config", body, timeout=30)


def crawl(pages: int, start_page: int = 1) -> dict:
    return dr.api("POST", "/api/crawl/start", {
        "max_pages": pages, "start_page": start_page, "interval": 4.0,
        "use_search": True, "fetch_details": False,
        "detail_interval": 3.0, "store_blacklisted": True,
    }, timeout=30)


def busy(path: str) -> bool:
    try:
        st = dr.api("GET", path, timeout=10).get("status")
    except Exception:  # noqa: BLE001
        return True  # 查询失败不是空闲，不能再启动同类任务
    return st in {"running", "queued", "starting"}


def wait_task(path: str, limit_sec: float, task_id: str | None = None) -> dict:
    deadline = time.monotonic() + limit_sec
    while time.monotonic() < deadline:
        result = dr.api('GET', path, timeout=15)
        if ACTIVE_STATE is not None:
            heartbeat(ACTIVE_STATE, '等待任务进度：' + path + ' · ' + str(result.get('progress') or {}))
        if task_id and str(result.get('task_id') or '') != task_id:
            raise RuntimeError('任务已消失或归属变化，停止等待，不采用其他任务结果')
        if result.get('status') in {'done', 'error', 'cancelled', 'idle'}:
            return result
        time.sleep(2)
    raise RuntimeError('等待任务超时；不重新启动同类任务，请核对原任务状态')


def collect_pages(query: str, cfg: dict, start: int, pages: int) -> dict:
    """与发送轮 refill 共用抓取临界区：改配置、启动、收尾必须是同一个操作。"""
    try:
        with dr.Lock("dsh-boss-collection"):
            put_search(query, cfg, start, pages)
            task = crawl(pages, start_page=start)
            tid = str(task.get("task_id") or "")
            if not tid:
                raise RuntimeError("抓取任务未启动")
            fin = wait_task("/api/crawl/status", 900, task_id=tid)
            if fin.get("status") != "done":
                raise RuntimeError(f"抓取任务未完成：{fin.get('status')}")
            return fin
    except dr.Busy:
        log("另一轮正在抓取，本轮跳过抓取并继续处理积压")
        return {"status": "busy"}


def fetch_jd(limit: int, interval: float) -> dict:
    task = dr.api("POST", "/api/jobs/fetch-descriptions",
                  {"limit": limit, "interval": interval}, timeout=30)
    if task.get("task_id"):
        return wait_task("/api/jobs/fetch-descriptions/status", 3600, task_id=str(task["task_id"]))
    return task


def jd_pending_ids(max_n: int) -> list[str]:
    items = dr.api("GET", "/api/jobs?limit=200&jd_status=pending", timeout=60).get("items") or []
    return [str(j.get("encrypt_job_id")) for j in items[:max_n] if j.get("encrypt_job_id")]


def job_pages(jd_status: str, page: int = 200):
    """逐页遍历；重复页面视为接口契约异常，防止 offset 被忽略后无限循环。"""
    offset, seen = 0, set()
    while True:
        batch = dr.api("GET", f"/api/jobs?limit={page}&offset={offset}&jd_status={jd_status}",
                       timeout=60).get("items") or []
        if not batch:
            return
        ids = tuple(str(j.get("encrypt_job_id") or "") for j in batch)
        if ids in seen:
            raise RuntimeError("岗位分页重复，停止本轮以避免遗漏或循环")
        seen.add(ids)
        yield batch
        if len(batch) < page:
            return
        offset += page


def iter_jobs(jd_status: str, want: int = 200, page: int = 200) -> list[dict]:
    """分页取岗位（C4）：一次最多 200 条，够 want 或没有下一页就停。"""
    out: list[dict] = []
    for batch in job_pages(jd_status, page):
        out.extend(batch)
        if len(out) >= want:
            break
    return out[:want]


def unscored_with_jd(max_n: int, resume_id: str = "") -> list[str]:
    """有 JD、但还没有任何分数的岗位。

    2026-11-01 用户定：**以前的分数照样有效**，换简历不再作废旧分。
    所以这里只看"有没有打过分"，不看分数是哪份简历给的；`resume_id` 保留参数只为调用方便。
    """
    scored: set[str] = set()
    for meta in dr.api("GET", "/api/match/analyses").get("items") or []:
        if meta.get("status") not in {"done", "running"}:
            continue
        for it in dr.api("GET", f"/api/match/analyses/{meta.get('analysis_id')}",
                         timeout=60).get("matches") or []:
            jid = str(it.get("encrypt_job_id") or "")
            if jid:
                scored.add(jid)
    todo: list[str] = []
    seen: set[str] = set()
    if max_n <= 0:
        return todo
    # jd_status 只有 completed 才算"抓完"；但库里 594 个岗位 jd_status 还是 pending、
    # job_desc 却已经有正文（详情抓回来只写了正文没推状态）。判"有没有 JD"要和面板一致：
    # 看 job_desc 有没有内容，而不是看那个状态字段。
    for status in ("completed", "pending"):
        for batch in job_pages(status):
            for job in batch:
                jid = str(job.get("encrypt_job_id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                if not str(job.get("job_desc") or "").strip():
                    continue
                if jid in scored or dr.prefilter_reason(job):
                    continue
                todo.append(jid)
                if len(todo) >= max_n:
                    return todo
    return todo


def probe_activity(max_n: int, min_score: int, interval: float, ttl_hours: float = 24.0) -> int:
    """给「分数够、但还没探过 / 记录过期」的岗位补活跃时间。

    C4：活跃记录按 TTL 刷新（默认 24 小时），已有记录不能永远跳过；
    没有活跃记录的岗位会被 `collect_eligible` 挡在门外，所以这一步要常驻跑。
    """
    try:
        import probe_activity as pa  # 同目录脚本，抓详情要用程序自己的 client
    except Exception as exc:  # noqa: BLE001
        log(f"活跃探测不可用（{type(exc).__name__}: {exc}），跳过")
        return 0

    have = pa.load_activity()
    now = time.time()
    ttl = float(ttl_hours or 24.0) * 3600
    cand: list[str] = []
    for meta in dr.api("GET", "/api/match/analyses").get("items") or []:
        if meta.get("status") not in {"done", "running"}:
            continue
        for it in dr.api("GET", f"/api/match/analyses/{meta.get('analysis_id')}",
                         timeout=60).get("matches") or []:
            jid = str(it.get("encrypt_job_id") or "")
            if not jid or jid in cand:
                continue
            if int(it.get("match_score") or 0) < min_score:
                continue
            if it.get("greet_in_cooldown") or it.get("delivered_at"):
                continue
            rec = have.get(jid) or {}
            fresh = bool(rec) and (now - float(rec.get("checked_at_ts") or 0)) < ttl
            if fresh:
                continue
            cand.append(jid)
            if len(cand) >= max_n:
                break
        if len(cand) >= max_n:
            break
    if not cand:
        return 0

    from boss_jobs import create_client
    from boss_jobs.store import JobStore

    client, store = create_client(), JobStore()
    ok = 0
    data = dict(have)
    for jid in cand:
        job = store.get_job(jid)
        if job is None:
            continue
        try:
            detail = client.fetch_job_detail(security_id=job.security_id, lid=job.lid)
        except Exception as exc:  # noqa: BLE001
            log(f"  探活跃失败 {jid}: {type(exc).__name__}")
            from boss_jobs.request_control import restriction
            if restriction(exc):
                log('平台限制：停止本批活跃探测，保留暂停状态等待人工处理')
                break
            time.sleep(interval)
            continue
        sec, desc = pa.active_at_of(detail.raw)
        if sec:
            data[jid] = {"active_at": sec, "desc": desc,
                         "checked_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "checked_at_ts": time.time(),
                         "brand": str(job.brand_name or ""),
                         "title": str(job.job_name or "")}
            ok += 1
        time.sleep(interval)
    pa.save_activity(data)
    return ok


def match_task_status() -> dict:
    """当前匹配任务快照；查询失败返回 `{"error": ...}`（视为**未知**，不当空闲）。"""
    try:
        return dr.api("GET", "/api/match/analyze/status", timeout=15) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def check_running_match(st: dict) -> str:
    """按 task_id 复核上一轮启动的匹配任务（B3：不阻塞等待整批）。"""
    task = st.get("match_task") or {}
    tid = str(task.get("task_id") or "")
    if not tid:
        return ""
    snap = match_task_status()
    if snap.get("error"):
        # 查询失败 = 未知：既不清空任务，也不启动新任务
        return f"匹配状态查询失败（{snap['error']}），本轮不启动新匹配"
    status_now = str(snap.get("status") or "")
    if str(snap.get("task_id") or "") != tid:
        st["match_task"] = {}
        return f"匹配任务 {tid} 已被替换（当前 {snap.get('task_id')}）"
    if status_now in {"done", "error", "cancelled"}:
        prog = snap.get("progress") or {}
        done_n = snap.get("done")
        if done_n is None:
            done_n = prog.get("done")
        st["match_task"] = {}
        return f"上一批匹配收尾：{status_now}，完成 {done_n}"
    return ""


def cfg_mtime() -> tuple:
    """配置指纹：搜索条件、方案、策略、候选人资料任一变化都在安全边界重新加载。

    以前只盯 search/workflow/profile 三个文件，运行中改了写作策略或方案门槛，
    长驻进程里还是旧值。
    """
    def stamp(path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    paths = [CFG_PATH, dr.CFG_PATH, HERE / "workspace.json"]
    for sub in ("candidate/revisions", "plans/revisions", "strategies/revisions"):
        folder = HERE / sub
        if folder.is_dir():
            paths.extend(sorted(folder.glob("*.json")))
    return tuple(stamp(path) for path in paths)


def corpus_due(st: dict, cfg: dict, db_total: int) -> str:
    """这一轮要不要把库里的岗位同步到磁盘语料库（双写，返回空串=不用同步）。

    规则：库内条数一变就在**同一轮**同步；条数没变时，每 `corpus_export_every` 轮
    强制复核一次，兜住“条数没变但字段变了（补到 JD、状态变化）”的情况。
    同步失败时不更新 `corpus_total`，所以下一轮会自动重试，不靠人工重跑。
    """
    last = st.get("corpus_total")
    if last is None:
        return "首次同步"
    try:
        delta = int(db_total) - int(last)
    except (TypeError, ValueError):
        return "计数异常，强制复核"
    if delta > 0:
        return f"库内新增 {delta}"
    if delta < 0:
        return f"库内减少 {-delta}，强制复核"
    # corpus_export_every=0 是显式"不做定期复核"；只有缺失/None 才用默认 20。
    every_raw = cfg.get("corpus_export_every")
    every = int(20 if every_raw is None else every_raw)
    if every > 0 and int(st.get("cycle") or 0) % every == 0:
        return "定期复核"
    return ""


def job_total() -> int:
    """库里岗位总数（只要数字，不取内容）。测试里要打桩，别让它去连真接口。"""
    return int((dr.api("GET", "/api/jobs/stats", timeout=20) or {}).get("jobs") or 0)


def sync_corpus(api=None) -> dict:
    """把库里的岗位同步到磁盘语料库（双写）。

    api 显式传进来：测试打到桩上，不会因为一次 `one_cycle` 就把生产语料库改写。
    """
    import corpus_export as ce
    return ce.sync(api=api)


def search_targets(cfg: dict) -> list[dict]:
    """每城×每关键词独立游标，共享原有全日额度和采集间隔。"""
    bases = cfg.get("bases") or [cfg.get("base") or {}]
    return [{"key": f"{b.get('city', '')} | {q}" if len(bases) > 1 else q,
             "query": q, "base": dict(b)} for q in cfg.get("queries") or [] for b in bases]


def platform_allowed(scope: str) -> bool:
    from boss_login.account import assert_task_environment, AccountChangedError
    try:
        current = assert_task_environment()
        pinned = globals().get('_collector_identity')
        if pinned and current != pinned:
            return False
    except AccountChangedError:
        return False
    from boss_jobs.request_control import get_control
    state = get_control().status()
    return not state.get('account_paused') and scope not in (state.get('paused_scopes') or {})


def one_cycle(st: dict, cfg: dict, resume_id: str) -> str:
    global ACTIVE_STATE
    ACTIVE_STATE = st
    heartbeat(st, '本轮开始：检查任务与采集积压')
    rollover(st)
    targets = search_targets(cfg)
    queries = [t["key"] for t in targets]
    st["cycle"] = int(st.get("cycle") or 0) + 1
    note = ""

    # 先看上一轮启动的匹配跑完没有；没有就继续本轮采集（不阻塞等待）
    running = check_running_match(st)
    if running:
        log(running)
        note = running[:60]

    blocked = not platform_allowed('search')
    capped = blocked or int(st.get("pages") or 0) >= int(cfg.get("daily_page_cap") or 0)

    # 轮换关键词：跳过已翻到头的；全部翻完就开新一轮，从第 1 页重走捞新发布的岗位
    idx = int(st.get("query_index") or 0) % max(1, len(queries))
    for _ in range(len(queries)):
        if queries[idx] not in (st.get("exhausted") or []):
            break
        idx = (idx + 1) % max(1, len(queries))
    else:
        if queries and not capped:
            st["pass_no"] = int(st.get("pass_no") or 0) + 1
            st["exhausted"] = []
            st["query_pages"] = {}
            idx = 0
            log(f"关键词全部翻到头，开始第 {st['pass_no']} 轮重走（从第 1 页起）")
        else:
            idx = 0
            capped = True
    q = queries[idx] if queries else ""
    st["query_index"] = idx

    if capped:
        log('平台搜索已暂停，只处理本地积压' if blocked else
            f"今日抓取到上限 {cfg.get('daily_page_cap')} 页 / 无关键词，只消化积压")
        note = note or "只消化积压"
    else:
        pages_done = int((st.get("query_pages") or {}).get(q) or 0)
        max_pages = int((cfg.get("pages") or {}).get("max_pages_per_query") or 10)
        if pages_done >= max_pages:
            st.setdefault("exhausted", []).append(q)
            log(f"{q} 已翻 {pages_done} 页到头，换下一个")
        else:
            pages = min(int(cfg.get("pages_per_query") or 2),
                        int(cfg.get("daily_page_cap") or 0) - int(st.get("pages") or 0),
                        max_pages - pages_done)
            heartbeat(st, '正在采集岗位：' + q)
            target = targets[idx]
            fin = collect_pages(target["query"], {**cfg, "base": target["base"]}, pages_done + 1, pages)
            if fin.get("status") == "done":
                prog = fin.get("progress") or {}
                got = int(prog.get("pages") or 0)
                log(f"[{q}] 第 {pages_done + 1}–{pages_done + pages} 页：原始 {prog.get('raw_count')} "
                    f"新增 {prog.get('inserted')} 更新 {prog.get('updated')} 状态 {fin.get('status')}")
                note = f"crawl {q} +{prog.get('inserted')}"
                if got <= 0:
                    # 一页都没拿到：两种可能，判错都会出事。
                    #  - 接口回空列表 = 这个词真的没有下一页了 → 必须标记翻完，否则每轮都来问一次，永远卡在这
                    #  - 风控/任务被取消 = 不能当成"翻完"把整个词丢掉，游标也不能写成 0
                    why = str(fin.get("stopped_reason") or prog.get("stopped_reason") or "")
                    if "回空" in why:
                        st.setdefault("exhausted", []).append(q)
                        log(f"{q} 第 {pages_done + 1} 页接口回空（{why}），标记翻完")
                    else:
                        st["crawl_empty"] = int(st.get("crawl_empty") or 0) + 1
                        log(f"{q} 第 {pages_done + 1} 页一页都没拿到"
                            f"（第 {st['crawl_empty']} 次，{why or '无原因'}），游标不动，下轮重试同一页")
                else:
                    st["pages"] = int(st.get("pages") or 0) + got
                    st.setdefault("query_pages", {})[q] = pages_done + got
                    # 只要到一部分页 = BOSS 自己返回 has_more=0，这个词在本轮已到头，别再来一遍
                    if got < pages:
                        st.setdefault("exhausted", []).append(q)
                        log(f"{q} 搜索只返回 {got}/{pages} 页，本站已无下一页，标记翻完")
                # 抓完就先落盘：以前只在一轮结束才存状态，进程被杀（或我手动停）时
                # 这一轮的页数就丢了，面板"今日页数"永远比库里少几页。
                save_state(st)

    # 补 JD
    jd_used = max(int(st.get("jd") or 0), int(st.get("jd_attempts") or 0))
    if platform_allowed('details') and jd_used < int(cfg.get("daily_jd_cap") or 0):
        if busy("/api/jobs/fetch-descriptions/status"):
            log("补 JD 有任务在跑，跳过")
        else:
            limit = min(int(cfg.get("jd_batch") or 15), int(cfg.get("daily_jd_cap") or 0) - jd_used)
            st["jd_attempts"] = jd_used + limit
            heartbeat(st, '正在补充岗位详情')
            fin_jd = fetch_jd(limit, float(cfg.get("jd_interval") or 1.5))
            pg = fin_jd.get("progress") or {}
            st["jd"] = int(st.get("jd") or 0) + int(pg.get("ok") or 0)
            log(f"补 JD：尝试 {pg.get('total')} 成功 {pg.get('ok')} 失败 {pg.get('failed')}")
            note += f" +jd{pg.get('ok')}"

    # 补匹配：只启动、不等整批（B3）。判分要调模型、按量花钱，所以只在 DeepSeek 半价时段跑：
    # 半价 = 工作日 12:00–14:00、18:00–次日 09:00，周末与节假日全天（判定见 dr.is_peak_pricing）。
    # 全价时段一律不判分——积压再多也不加急。2026-11-02 用户拍板，删掉了原来的
    # match_force_backlog 出口（积压 >= 60 就在高峰开跑），那条路径让全价时段也会判分。
    todo_pool = unscored_with_jd(max(2000, int(cfg.get("match_batch") or 25)), resume_id)
    backlog = len(todo_pool)
    st["backlog"] = backlog
    st["query"] = q
    peak = dr.is_peak_pricing()
    st["pricing"] = "peak" if peak else "off"
    # 边界保护：一批判分起步后要跑几分钟，13:58 起一批会跨进 14:00 的全价区。
    # 所以离下一个全价时段不足 MATCH_LEAD_MIN 分钟就不新起。
    near_peak = (not peak) and dr.is_peak_pricing(
        now=dt.datetime.now() + dt.timedelta(minutes=MATCH_LEAD_MIN))
    if st.get("match_task"):
        log(f"匹配仍在跑（{backlog} 个待匹配），本轮不再启动新任务")
        note += f" backlog{backlog}"
    elif peak or near_peak:
        why = "全价时段" if peak else f"距全价不到 {MATCH_LEAD_MIN} 分钟"
        log(f"{why}，不判分（积压 {backlog} 个待匹配，等半价窗口）")
        note += f" backlog{backlog}"
    else:
        todo = todo_pool[:int(cfg.get("match_batch") or 25)]
        if todo:
            m = dr.api("POST", "/api/match/analyze",
                       {"resume_id": resume_id, "job_ids": todo}, timeout=30)
            tid = str(m.get("task_id") or "")
            if tid:
                st["match_task"] = {"task_id": tid, "jobs": len(todo),
                                    "started_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                log(f"已启动匹配：{len(todo)} 个（task {tid}），不等待，继续采集")
                note += f" +matchstart{len(todo)}"
            else:
                log(f"匹配没起来：{json.dumps(m, ensure_ascii=False)[:160]}")
        else:
            log("没有待匹配的岗位")

    # 语料库归档：双写。库内一有新增就在同一轮落盘，另按 corpus_export_every 定期复核
    db_total = -1
    try:
        db_total = job_total()
    except Exception as exc:  # noqa: BLE001
        log(f"取库内岗位数失败（本轮强制复核语料库）：{type(exc).__name__}: {exc}")
    due = corpus_due(st, cfg, db_total) if db_total >= 0 else "计数取不到，强制复核"
    if due:
        try:
            stats = sync_corpus(getattr(dr, "api", None))
            st["corpus_total"] = int(stats.get("db_total") or stats.get("total_jobs") or 0)
            st["corpus_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            gap = int(stats.get("missing_on_disk") or 0)
            log(f"语料库已同步（{due}）：新增 {stats.get('added')} 更新 {stats.get('updated')}，"
                f"库 {stats.get('db_total')} / 盘 {stats.get('disk_total')}"
                + (f"，仍缺 {gap}" if gap else ""))
            note += f" +corpus{stats.get('added')}"
        except Exception as exc:  # noqa: BLE001
            log(f"语料库同步失败（不退出，下一轮重试）：{type(exc).__name__}: {exc}")

    # 补活跃时间（分数够但没探过的）；activity_batch=0 是"不探活跃"，不能回退默认 20
    act_batch = cfg.get("activity_batch")
    act_batch = int(20 if act_batch is None else act_batch)
    if act_batch > 0 and platform_allowed('details'):
        try:
            # activity_min_score=0 是"不限分"，同样不能回退默认 80。
            score_raw = cfg.get("activity_min_score")
            ok_act = probe_activity(act_batch,
                                    int(0 if score_raw is None else score_raw),
                                    float(cfg.get("activity_interval") or 1.0))
            if ok_act:
                log(f"补活跃时间：{ok_act} 个")
                note += f" +act{ok_act}"
        except Exception as exc:  # noqa: BLE001
            log(f"补活跃时间出错（不退出）：{type(exc).__name__}: {exc}")
    return note


def deadline_for(hours: float, now: float | None = None) -> float:
    """算停机时刻。`--hours` 给 0 或负数 = 不限时（返回 inf），用于常驻采集。

    采集不调模型、不花钱，而且是原始数据库的唯一来源，所以默认就该一直跑；
    限时只在临时排查时才用（2026-11-02 用户要求）。
    """
    now = time.time() if now is None else now
    return float("inf") if hours <= 0 else now + hours * 3600


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--hours", type=float, default=8.0,
                    help="跑多少小时后自己停；0 或负数 = 不限时（常驻）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        with dr.Lock("dsh-boss-collector-1"):
            return run_collector(args)
    except dr.Busy:
        log("已有另一个采集器在跑（命名互斥体被占用），本次不启动")
        return 5


def run_collector(args) -> int:
    """只有锁的持有者可以读取后回写状态，拒绝启动的实例不做 finally 落盘。"""

    from boss_login.account import assert_task_environment
    global _collector_identity
    _collector_identity = assert_task_environment()
    cfg = load_cfg()
    try:
        st = load_state()
    except Exception as exc:  # noqa: BLE001
        log(f"状态文件损坏，拒绝启动（不静默归零）：{type(exc).__name__}: {exc}")
        return 3
    resume_id = str(dr.load_config().get("resume_id") or "")
    if not resume_id:
        log("工作流配置里没有 resume_id，停")
        return 2

    log(f"启动：{cfg.get('version')} · {len(cfg.get('queries') or [])} 个关键词 · "
        f"筛选 exp={cfg['base'].get('experience')} degree={cfg['base'].get('degree')} "
        f"scale={cfg['base'].get('scale')} salary={cfg['base'].get('salary') or '不限'!r}")
    if args.dry_run:
        log("dry-run：只打印矩阵")
        for q in cfg.get("queries") or []:
            log("  关键词 " + q)
        return 0

    deadline = deadline_for(args.hours)
    if deadline == float("inf"):
        log("不限时运行（--hours 0）：常驻采集，只做半价时段判分")
    seen_mtime = cfg_mtime()
    try:
        while time.time() < deadline:
            try:
                note = one_cycle(st, cfg, resume_id)
                heartbeat(st, note)
                log("循环结束：" + note)
            except Exception as exc:  # noqa: BLE001
                log(f"循环出错（不退出）：{type(exc).__name__}: {exc}")
                heartbeat(st, f"error {type(exc).__name__}")
            finally:
                save_state(st)
            if args.once:
                break
            # C4：配置在安全边界重新加载，并记录实际生效版本
            stamp = cfg_mtime()
            if stamp != seen_mtime:
                try:
                    # 必须先清缓存：`load_cfg()` 走 `dr.snapshot_state()`，它把快照缓存在
                    # `dr._cache` 里。以前这里先 load_cfg 再 clear，读回来的永远是进程启动
                    # 时那一版方案——日志写着"配置已重新加载"，实际一直是旧值（改上限不生效）。
                    dr._cache.clear()
                    next_cfg = load_cfg()
                    next_resume = str(dr.load_config().get("resume_id") or "")
                    if not next_resume:
                        raise RuntimeError("当前简历未配置")
                    cfg, resume_id = next_cfg, next_resume
                    seen_mtime = stamp
                    log(f"配置已重新加载：{cfg.get('version')}")
                    heartbeat(st, f"reload {cfg.get('version')}")
                except Exception as exc:  # noqa: BLE001
                    log(f"配置重载失败，继续用旧配置：{type(exc).__name__}: {exc}")
            time.sleep(float(cfg.get("cycle_sleep_sec") or 45))
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        save_state(st)
        heartbeat(st, "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
