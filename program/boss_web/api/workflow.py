"""工作流面板：把 boss-auto 的额度、采集状态、语料库与配置读出来给网页看。

只读接口，不改任何状态。数据源是 boss-auto 目录下的 JSON / JSONL 台账，
目录由环境变量 `BOSS_WORKFLOW_DIR` 指定（缺省取工作区的 boss-auto）。
读不到就返回 `ok:false` 和原因，不抛 500——面板要在各种残缺状态下也能打开。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..services.greeting_prompt import resolve_greeting_requirement


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _default_state_dir() -> Path | None:
    """没有配置时的默认：**没有默认**。

    这里以前回落到原用户机器上的固定路径。通用版不再猜：缺 `BOSS_WORKFLOW_DIR`
    就返回 None，由调用方回 `工作流未配置` 并说明设置哪个变量——同一台机器上换个
    用户、或换台机器安装，都不会串到别人的数据目录。
    """
    return None


def api_base() -> str:
    """控制台地址；仅供报告缓存的键使用（不用于发起 HTTP）。"""
    return (os.environ.get("BOSS_API_BASE") or "http://127.0.0.1:8787").rstrip("/")


def workflow_dir() -> Path | None:
    """用户状态目录（配置、台账、语料、运行记录）。取不到返回 None，不抛异常。

    刻意不调用工作流的 `workflow_env`：那个模块把「代码目录」和「状态目录」
    绑在同一个变量上。程序这边只需要状态目录，而且**必须在缺少工作流目录时也能启动**，
    所以在这里按两个独立环境变量解析：
      BOSS_WORKFLOW_DIR   状态目录（缺省：迁移兼容的本机旧路径）
      BOSS_WORKFLOW_CODE  代码目录（缺省：同状态目录）
    """
    return _env_path("BOSS_WORKFLOW_DIR") or _default_state_dir()


def workflow_code_dir(state: Path | None) -> Path | None:
    """工作流代码目录（deliver_round_v2.py 所在处）。

    解析顺序：``BOSS_WORKFLOW_CODE`` → 状态目录 → 程序自带的 ``workflow/``
    （打包态；源码树见 :func:`boss_web.services.workspace_setup.bundled_workflow_dir`）
    → ``None``。只有真能读到 ``deliver_round_v2.py`` 才认，不猜目录。
    """
    code = _env_path("BOSS_WORKFLOW_CODE")
    if code and (code / "deliver_round_v2.py").is_file():
        return code
    if state and (state / "deliver_round_v2.py").is_file():
        return state
    from ..services import workspace_setup

    bundled = workspace_setup.bundled_workflow_dir()
    if bundled is not None and (bundled / "deliver_round_v2.py").is_file():
        return bundled
    return None


def workflow_unavailable() -> dict[str, Any]:
    """工作流不可用时的统一回包：告诉人缺什么，而不是让整个应用起不来。"""
    return {
        "ok": False,
        "error": "工作流未配置：设置 BOSS_WORKFLOW_DIR（状态目录）与 "
                 "BOSS_WORKFLOW_CODE（代码目录）后重试",
        "dir": str(workflow_dir() or ""),
    }


router = APIRouter()

def _read_json(path: Path, default: Any = None) -> Any:
    """缺失的可选快照用默认值；损坏或类型错误必须让面板明确报错。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取 {path.name}：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 必须是 JSON 对象")
    return value


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    """取 JSONL 末尾 limit 条；坏行跳过。"""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            rows = []
            for line in deque(fh, maxlen=limit):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            return rows
    except OSError:
        return []


def _mtime(path: Path) -> str:
    """文件最后修改时间；不存在返回空串。"""
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return ""


def _collector(root: Path, page_cap: int = 0) -> dict[str, Any]:
    """采集器心跳：新鲜度决定网页上显示"运行中 / 已停止"。

    `running` 只说"进程还活着"，不等于"还在翻页"：撞到每日页数上限后，
    采集器还在补 JD、探活跃、起匹配——心跳照样新鲜，但 `pages` 不再增长。
    所以另外给 `page_cap` / `capped`，让面板能显示第三种状态，
    而不是让人看着"运行中 + 页数不动"猜是不是卡住了。
    """
    hb = _read_json(root / "runs" / "collector-heartbeat.json", {}) or {}
    at = str(hb.get("at") or "")
    age = None
    if at:
        try:
            then = dt.datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
            age = int((dt.datetime.now() - then).total_seconds())
        except ValueError:
            age = None
    pages = hb.get("pages")
    capped = bool(page_cap and isinstance(pages, (int, float)) and pages >= page_cap)
    # 心跳 10 分钟内算在跑；采集器一轮 5 分钟，留一倍余量
    return {
        "at": at,
        "age_seconds": age,
        "running": bool(age is not None and age <= 600),
        "cycle": hb.get("cycle"),
        "pages": pages,
        "page_cap": int(page_cap) if page_cap else None,
        "capped": capped,
        "jd": hb.get("jd"),
        "backlog": hb.get("backlog"),
        "match_running": hb.get("match_running"),
        "query": hb.get("query"),
        "note": hb.get("note"),
    }


def _config(root: Path) -> dict[str, Any]:
    """面板要给人看的那些设置。

    值优先取**有效方案快照**（执行链路真正读的那份）；没有工作区才退回 JSON 文件。
    这样"面板上看到的门槛"和"发送时用的门槛"不会是两个数。
    """
    wf = _read_json(root / "config" / "workflow.json", {}) or {}
    se = _read_json(root / "config" / "search.json", {}) or {}
    rounds = wf.get("rounds") or {}
    effective: dict[str, Any] = {}
    try:
        from ..services import workflow_context

        snap = workflow_context.snapshot()
        if snap:
            plan = snap.get("plan") or {}
            runtime = plan.get("runtime") or {}
            search = snap.get("search") or {}
            salary = plan.get("salary") or {}
            effective = {
                "source": "plan",
                "min_score": runtime.get("min_score"),
                "daily_cap": runtime.get("daily_cap"),
                "chunk": runtime.get("chunk"),
                "chunk_gap": runtime.get("chunk_gap"),
                "batch_size": runtime.get("batch_size"),
                "salary_gate": {"daily_min": salary.get("min"),
                                "daily_max": salary.get("max"),
                                "period": salary.get("period")},
                "activity_within_days": (runtime.get("activity") or {}).get("within_days"),
                "queries": search.get("keywords") or [],
                "base_filters": search.get("base") or {},
                "daily_page_cap": search.get("daily_page_cap"),
                "cycle_sleep_sec": search.get("cycle_sleep_sec"),
                "pages_per_query": search.get("pages_per_query"),
                "jd_batch": search.get("jd_batch"),
                "jd_interval": search.get("jd_interval"),
                "daily_jd_cap": search.get("daily_jd_cap"),
                "activity_batch": search.get("activity_batch"),
                "activity_interval": search.get("activity_interval"),
                "plan_id": plan.get("id"),
                "plan_revision": plan.get("revision"),
            }
    except Exception:  # noqa: BLE001 - 快照读不到就退回文件，不让面板 500
        effective = {}
    base = {
        "workflow_version": wf.get("version"),
        "search_version": se.get("version"),
        "min_score": wf.get("min_score"),
        "daily_cap": wf.get("daily_cap"),
        "chunk": wf.get("chunk"),
        "chunk_gap": wf.get("chunk_gap"),
        "batch_size": wf.get("batch_size"),
        "bridge_timeout_sec": wf.get("bridge_timeout_sec"),
        "salary_gate": wf.get("salary_gate") or {},
        "activity_within_days": (wf.get("activity") or {}).get("within_days"),
        "rounds": {
            key: {
                "cap": (val or {}).get("cap"),
                "prepare": (val or {}).get("prepare"),
                "refill_pages": (val or {}).get("refill_pages"),
                "refill": (val or {}).get("refill"),
            }
            for key, val in rounds.items()
        },
        "cycle_sleep_sec": se.get("cycle_sleep_sec"),
        "pages_per_query": se.get("pages_per_query"),
        "jd_batch": se.get("jd_batch"),
        "jd_interval": se.get("jd_interval"),
        "activity_batch": se.get("activity_batch"),
        "activity_interval": se.get("activity_interval"),
        "daily_page_cap": se.get("daily_page_cap"),
        "daily_jd_cap": se.get("daily_jd_cap"),
        "queries": se.get("queries") or [],
        "base_filters": se.get("base") or {},
        "source": "files",
    }
    return {**base, **{k: v for k, v in effective.items() if v is not None}}


def _rounds(root: Path) -> list[dict[str, Any]]:
    """最近几轮投递：准入、写稿、成功/失败与额度变化。"""
    rows = _tail_jsonl(root / "rounds.jsonl", 40)
    out = []
    for row in reversed(rows[-12:]):
        out.append({
            "time": row.get("time"),
            "round": row.get("round"),
            "mode": row.get("mode"),
            "config_version": row.get("config_version"),
            "eligible": row.get("eligible"),
            "prepared": row.get("prepared"),
            "ready": row.get("ready"),
            "ok": row.get("ok"),
            "failed": row.get("failed"),
            "skipped": row.get("skipped"),
            "aborted": row.get("aborted"),
            "used_before": row.get("used_before"),
            "used_after": row.get("used_after"),
        })
    return out


def _events(root: Path) -> list[dict[str, Any]]:
    """台账最近事件；只展示人看得懂的几个字段。"""
    rows = _tail_jsonl(root / "greeting" / "queue.jsonl", 200)
    out = []
    for row in reversed(rows[-40:]):
        text = str(row.get("text") or "")
        out.append({
            "at": row.get("at") or row.get("ts") or "",
            "event": row.get("event"),
            "job_id": row.get("job_id"),
            "score": row.get("score"),
            "reason": str(row.get("reason") or "")[:80],
            "chars": len(text) if text else 0,
        })
    return out


def _bridge_module(code_dir: Path):
    """按路径加载工作流的桥接器，用它的组装函数保证预览与真实请求一字不差。

    桥接器自己 `import workflow_env`，所以加载前要把**代码目录**放进 sys.path；
    并且要确认拿到的就是代码目录里的那一份，不能被别的目录同名模块占了缓存。
    """
    code = str(code_dir)
    expected = (code_dir / "workflow_env.py").resolve()
    added = code not in sys.path
    if added:
        sys.path.insert(0, code)
    try:
        loaded = sys.modules.get("workflow_env")
        if loaded is not None:
            got = Path(getattr(loaded, "__file__", "") or "").resolve()
            if got != expected:
                sys.modules.pop("workflow_env", None)  # 不吃别的目录的同名模块
        path = code_dir / "greeting" / "writer" / "bridge.py"
        spec = importlib.util.spec_from_file_location("boss_workflow_bridge", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"加载桥接器失败：{path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        got = sys.modules.get("workflow_env")
        if got is None or Path(got.__file__).resolve() != expected:
            actual = getattr(got, "__file__", "（未加载）")
            raise RuntimeError(f"workflow_env 来源不对：期望 {expected}，实际 {actual}")
        module.apply_policy()
        return module
    finally:
        if added and code in sys.path:
            sys.path.remove(code)


@router.get("/prompt-preview")
def prompt_preview() -> dict[str, Any]:
    """预览写招呼语时真正发给模型的完整提示词（结构说明 + 当前生效规则）。"""
    code = workflow_code_dir(workflow_dir())
    requirement = resolve_greeting_requirement()
    if code is None:
        return {**workflow_unavailable(), "requirement": requirement,
                "instruction": "", "max_len": 0}
    try:
        module = _bridge_module(code)
        instruction = module.build_instruction(requirement=requirement)
        max_len = int(getattr(module, "MAX_GREETING_LENGTH", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - 面板不能因为预览失败就 500
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "requirement": requirement, "instruction": "", "max_len": 0}
    policy = getattr(module, "POLICY", {}) or {}
    return {
        "ok": True,
        "error": "",
        "requirement": requirement,
        "instruction": instruction,
        "max_len": max_len,
        "policy_version": policy.get("version"),
        "requirement_chars": len(requirement),
        "instruction_chars": len(instruction),
    }


def _pricing(wf: dict[str, Any]) -> dict[str, Any]:
    """现在处于哪个计价窗口：高峰只抓取，半价才跑匹配。

    规则与工作流侧的 is_peak_pricing 保持一致（周一至周五 09-12、14-18 为高峰）。
    """
    conf = wf.get("pricing") or {}
    days = conf.get("peak_weekdays") or [1, 2, 3, 4, 5]
    windows = conf.get("peak_windows") or ["09:00-12:00", "14:00-18:00"]
    holidays = {str(x) for x in (conf.get("holidays") or [])}
    now = dt.datetime.now()
    peak = False
    if now.isoweekday() in days and now.date().isoformat() not in holidays:
        cur = now.hour * 60 + now.minute
        for spec in windows:
            try:
                start, end = (int(x.split(":")[0]) * 60 + int(x.split(":")[1])
                              for x in str(spec).split("-"))
            except (ValueError, TypeError, IndexError):
                continue
            if start <= end and start <= cur <= end:
                peak = True
            elif start > end and (cur >= start or cur <= end):
                peak = True
    # 下一个切换点：用于告诉人"什么时候会开始匹配"
    nxt = ""
    if now.isoweekday() in days:
        cur = now.hour * 60 + now.minute
        points = sorted({int(s.split(":")[0]) * 60 + int(s.split(":")[1])
                         for spec in windows for s in str(spec).split("-")})
        later = [p for p in points if p > cur]
        if later:
            nxt = f"{later[0] // 60:02d}:{later[0] % 60:02d}"
    return {"peak": peak, "label": "高峰" if peak else "半价",
            "now": now.strftime("%H:%M"), "next_switch": nxt,
            "note": conf.get("note") or ""}


def _match_live() -> dict[str, Any]:
    """程序里匹配任务的实时状态（工作流侧心跳可能滞后）。"""
    try:
        from ..services.match_task import match_tasks

        task = match_tasks.snapshot() or {}
    except Exception:  # noqa: BLE001 - 状态拿不到不影响其它卡片
        return {"running": False, "status": "", "done": 0, "total": 0}
    prog = task.get("progress") or {}
    status = str(task.get("status") or "")
    return {
        "running": status in {"running", "queued", "starting"},
        "status": status,
        "done": int(prog.get("done") or 0),
        "total": int(prog.get("total") or 0),
    }


#: /sendable 的短缓存：跑一次报告要 2–4 秒，面板 30 秒刷一次，没必要每次都跑
_REPORT_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


def clear_report_cache() -> None:
    """/sendable 的缓存作废：写稿/发送/匹配任务跑完后必须重算，否则面板显示的是旧数。"""
    _REPORT_CACHE.update(at=0.0, data=None, key=None)


def _scored_by_resume() -> dict[str, set[str]]:
    """每个岗位被哪些简历打过分的映射（只读一次库，别在循环里反复解析 payload）。"""
    import boss_db

    rows = boss_db.acquire().execute(
        "SELECT resume_id, payload FROM analysis WHERE status IN ('done', 'running')"
    ).fetchall()
    per_job: dict[str, set[str]] = {}
    for row in rows:
        rid = str(row["resume_id"] or "")
        raw = row["payload"]
        if not raw:
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            continue
        for item in (data.get("matches") or []):
            jid = str(item.get("encrypt_job_id") or "")
            if jid:
                per_job.setdefault(jid, set()).add(rid)
    return per_job


def _scoring_coverage(resume_id: str) -> dict[str, Any]:
    """按简历统计"打过没有"：换简历后旧分数不作数，但也不能把那些岗位当成"从没打过"。

    旧简历打过的 390 个岗位如果被当成"没打过"，要么重复花钱重打，要么永远不重打——
    两种都浪费。这里把三类分开数清楚，面板据此显示真实进度。
    """
    if not resume_id:
        return {"ok": False, "error": "缺少 resume_id"}
    try:  # 只读汇总；库读不到就让面板少显示一行，不要 500
        per_job = _scored_by_resume()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    current = sum(1 for rids in per_job.values() if resume_id in rids)
    return {
        "ok": True,
        "resume_id": resume_id,
        "scored_current": current,
        "scored_other_only": len(per_job) - current,
        "scored_any": len(per_job),
    }


def unscored_job_ids(resume_id: str, limit: int = 2000) -> list[str]:
    """还没打过任何分数、但有 JD 的岗位 id（补分按钮用，按新到旧排）。

    旧简历的分数照样有效，所以"打过"= 任何一份简历打过；只有完全没分的才需要补。
    """
    import boss_db

    per_job = _scored_by_resume()
    rows = boss_db.acquire().execute(
        "SELECT encrypt_job_id FROM jobs WHERE COALESCE(job_desc, '') <> '' "
        "ORDER BY fetched_at DESC"
    ).fetchall()
    out: list[str] = []
    for row in rows:
        jid = str(row[0])
        if not jid or jid in per_job:
            continue
        out.append(jid)
        if len(out) >= limit:
            break
    return out


@router.get("/sendable")
def sendable(force: bool = False) -> dict[str, Any]:
    """现在到底能发多少、能发哪些。

    直接调用工作流的 `--report-json`：用的是发送时同一套准入/就绪判断，
    所以这里显示的数字就是这一轮真会发出去的数字，不是面板另算一份。
    """
    now = time.time()
    root = workflow_dir()
    code = workflow_code_dir(root)
    if root is None or code is None:
        return workflow_unavailable()
    cache_key = (str(root), str(code), api_base())
    cached = _REPORT_CACHE.get("data")
    if (not force and cached and _REPORT_CACHE.get("key") == cache_key
            and now - float(_REPORT_CACHE.get("at") or 0) < 15):
        return {**cached, "cached": True, "age": int(now - float(_REPORT_CACHE["at"]))}
    script = code / "deliver_round_v2.py"
    if not script.is_file():
        return {"ok": False, "error": f"找不到 {script}"}
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(script), "--round", "manual", "--report-json"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180, check=False,
            # 子进程默认按 GBK 写标准输出，这里强制 UTF-8，否则中文全成乱码
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"跑报告失败：{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "error": f"报告进程失败（退出码 {proc.returncode}）",
                "returncode": proc.returncode}
    line = ""
    for raw in (proc.stdout or "").splitlines():
        raw = raw.strip()
        if raw.startswith("{"):
            line = raw
    if not line:
        return {"ok": False, "error": "报告没有输出",
                "stderr": (proc.stderr or "")[-300:], "returncode": proc.returncode}
    try:
        data = json.loads(line)
    except ValueError as exc:
        return {"ok": False, "error": f"报告不是合法 JSON：{exc}"}
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return {"ok": False, "error": "报告缺少布尔类型的 ok 字段"}
    if data.get("ok"):
        data["coverage"] = _scoring_coverage(str(data.get("resume_id") or ""))
    _REPORT_CACHE.update(at=now, data=data, key=cache_key)
    return {**data, "cached": False, "age": 0}


def workspace_ready(root: Path | None) -> bool:
    """工作区能不能进入执行。

    新工作区以 `workspace.json` 为准（资料/方案在里面，`config/workflow.json` 可有可无）；
    旧工作区没有 workspace.json 时仍按老标记判断，迁移前的目录照样能用。
    """
    if root is None:
        return False
    return (root / "workspace.json").is_file() or (root / "config" / "workflow.json").is_file()


def _overview(root: Path) -> dict[str, Any]:
    """工作流总览：额度、采集、语料库、配置、最近轮次与台账事件。"""
    usage = _read_json(root / "greeting" / "usage.json", {}) or {}
    corpus = _read_json(root / "corpus" / "stats.json", {}) or {}
    wf = _read_json(root / "config" / "workflow.json", {}) or {}
    cap = int(wf.get("daily_cap") or 0)
    today = dt.date.today().isoformat()
    # usage.json 是"最后写过的那一天"的额度；跨天没写过时它的日期是旧的，
    # 直接把 71 当成今日已发会误导人，所以跨天一律按 0 展示，原值单独留着。
    stale = bool(usage.get("date") and str(usage.get("date")) != today)
    confirmed = 0 if stale else int(usage.get("confirmed") or 0)
    reserved = 0 if stale else int(usage.get("reserved") or 0)
    cfg = _config(root)
    return {
        "ok": root.is_dir(),
        "error": "" if root.is_dir() else f"工作流目录不存在：{root}",
        "dir": str(root),
        "server_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quota": {
            "date": usage.get("date"),
            "stale": stale,
            "confirmed": confirmed,
            "reserved": reserved,
            "confirmed_raw": int(usage.get("confirmed") or 0),
            "reserved_raw": int(usage.get("reserved") or 0),
            "daily_cap": cap,
            "remaining": max(0, cap - max(confirmed, reserved)) if cap else None,
        },
        "collector": _collector(root, int(cfg.get("daily_page_cap") or 0)),
        "pricing": _pricing(wf),
        "match": _match_live(),
        "match_tasks": _match_tasks(),
        "corpus": {
            "generated_at": corpus.get("generated_at"),
            "total_jobs": corpus.get("total_jobs"),
            "with_jd": corpus.get("with_jd"),
            "jd_ratio": corpus.get("jd_ratio"),
            "by_day": corpus.get("by_day") or {},
            "greeted": corpus.get("已打招呼"),
            "not_greeted": corpus.get("未打招呼"),
            "index_mtime": _mtime(root / "corpus" / "index.jsonl"),
        },
        "config": _config(root),
        "rounds": _rounds(root),
        "events": _events(root),
        "last_run": _read_json(root / "last-run.json", {}) or {},
        "files": {
            "queue": _mtime(root / "greeting" / "queue.jsonl"),
            "usage": _mtime(root / "greeting" / "usage.json"),
            "rounds": _mtime(root / "rounds.jsonl"),
        },
    }


def _match_tasks(limit: int = 15) -> list[dict[str, Any]]:
    """最近的匹配任务：采集器每起一批匹配就会生成一条分析记录。

    面板要让人一眼看到"系统刚才跑了什么"，所以这里直接读程序的 analysis 表，
    与「匹配 → 分析历史」是同一份数据，不另存一份。
    """
    try:
        from ..services.resume_store import list_analyses

        rows = list_analyses()
    except Exception:  # noqa: BLE001 - 读不到不影响其它卡片
        return []
    out = []
    for row in rows[:limit]:
        out.append({
            "analysis_id": row.get("analysis_id"),
            "created_at": row.get("created_at"),
            "job_count": row.get("job_count"),
            "top_score": round(float(row.get("top_score") or 0), 1),
            "status": row.get("status"),
            "model": row.get("model"),
        })
    return out


@router.get("/overview")
def overview() -> dict[str, Any]:
    """Expose unreadable configuration as an error, never as a zero/healthy snapshot."""
    root = workflow_dir()
    if root is None:
        return workflow_unavailable()
    try:
        return _overview(root)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        return {"ok": False, "error": f"工作流配置或快照无法读取：{exc}", "dir": str(root)}


@router.get('/feedback')
def feedback_report() -> dict[str, Any]:
    """Read persisted reply observations; never contact BOSS on page refresh."""
    root = workflow_dir()
    if root is None:
        return workflow_unavailable()
    try:
        from boss_core.feedback import read_report
        from ..services.feedback_reader import read_control
        from boss_login.account import current_account
        return {'ok': True, **read_report(root), 'read_control': read_control(),
                'current_account': current_account()}
    except (OSError, ValueError, TypeError) as exc:
        return {'ok': False, 'error': f'回复记录读取失败：{exc}'}


@router.get('/feedback/records')
def feedback_records(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                     status: Literal['all', 'reply', 'none', 'unknown'] = 'all'):
    root = workflow_dir()
    if root is None:
        return workflow_unavailable()
    from ..services.feedback_review import list_records
    return list_records(root, offset=offset, limit=limit, status=status)


@router.get('/feedback/records/{job_id}')
def feedback_record(job_id: str):
    root = workflow_dir()
    if root is None:
        return workflow_unavailable()
    from ..services.feedback_review import detail
    from ..errors import NotFoundError
    row = detail(root, job_id)
    if row is None:
        raise NotFoundError('未找到该岗位的已确认发送记录')
    return {'ok': True, **row}


class FeedbackResumeBody(BaseModel):
    confirmed: Literal[True]


@router.post('/feedback/resume')
def resume_feedback(body: FeedbackResumeBody):
    from ..services.feedback_reader import set_pause
    return {'ok': True, 'read_control': set_pause(False, '用户明确恢复；仍需另外启动同步任务')}
