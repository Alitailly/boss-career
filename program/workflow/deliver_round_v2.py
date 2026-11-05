#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BOSS 投递 v2.1：岗位准入 / 写稿 / 发送三态分离 + 版本化配置 + 事件台账。

与 v2.0（2b6ab3f0…）的差别，按外部审查报告 02 号专项（R1–R8）与任务书：

- R1 运行参数只有一个机器可读真源 `config/workflow.json`，调度只传 `--round`；
- R2 写作规则只有 `greeting/policy.json`（带 version）与 `greeting/profile.json`，
  批次 md、桥接请求、校验全部读它，不再各写一份 200 字；
- R3/R4 反馈走独立 `greeting/feedback.jsonl`，按消息年龄窗口统计，读不到就记未知；
- R5 状态机拆成 greeting_status / delivery_status，反馈事件不再覆盖发送状态；
- R6 每次运行单独落 `runs/<run_id>.json`，备稿不覆盖发送记录；
- R8 队列坏行不再静默跳过，默认停止发送并报行号（`--repair-queue` 才隔离）。

保留 v2.0 已修的 B1–B8：命名互斥体、先占额度再请求、冻结态、逐项结果核对、
账本/名单损坏即停、先筛再截准备预算、JD 哈希与分数绑定。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

from workflow_env import CODE_ROOT, api_base, ensure_core_path, workspace_root

ensure_core_path()
from boss_core import compat as core  # noqa: E402 - 先放好核心库路径再导入
from boss_core.jsonio import digest as core_digest  # noqa: E402
from boss_core.models import c_scalar, c_values  # noqa: E402
from boss_core.strategies import resolve as resolve_strategy  # noqa: E402

BASE = api_base()
HERE = workspace_root()
WS = HERE.parent
QUEUE = HERE / "greeting" / "queue.jsonl"
USAGE = HERE / "greeting" / "usage.json"
FEEDBACK = HERE / "greeting" / "feedback.jsonl"
LOCK = HERE / "greeting" / ".lock"
ROUNDS = HERE / "rounds.jsonl"
SUMMARY = HERE / "last-run.json"
RUNS = HERE / "runs"
BRIDGE = CODE_ROOT / "greeting" / "writer" / "bridge.py"
REQ_DIR = HERE / "greeting" / "requests"
CFG_PATH = HERE / "config" / "workflow.json"
POLICY_PATH = HERE / "greeting" / "policy.json"
PROFILE_PATH = HERE / "greeting" / "profile.json"
PREFS_PATH = HERE / "greeting" / "preferences.json"
BLOCKLIST = HERE / "blocklist.json"
ACTIVITY = HERE / "greeting" / "activity.json"

# 活跃门槛：BOSS 详情里的 brandComInfo.activeTime（招聘者最近活跃时间）。
# 由 probe_activity.py 抓、写到 ACTIVITY；这里只读。关掉用 --no-activity。
ACTIVITY_GATE = True

PHONE = re.compile(r"\d{11}")
PLACEHOLDER = re.compile(r"\{[^}]*\}|XX|xx|某公司|某岗位")

_cache: dict[str, dict] = {}
STOP_FILE: Path | None = None
#: 授权窗口结束时间（epoch 秒，0 = 不设）。面板把授权的到期时间传进来：到点
#: 就停在"下一条还没发出去的消息"之前，不用等人去点暂停。
GRANT_DEADLINE: float = 0.0
_DEADLINE_SAID = False

#: 没有 `config/workflow.json` 时用的执行策略默认值（**不含个人假设**）。
#: 个人偏好（门槛/额度/时段/批大小）来自方案快照，这里只管"怎么跑"的形状。
DEFAULT_CONFIG: dict = {
    "version": "managed-default-v1",
    "rounds": {},
    "chunk": 1,
    "chunk_gap": 20,
    "batch_size": 6,
    "max_delivery_attempts": 2,
    "bridge_timeout_sec": 540,
}


def deadline_passed() -> bool:
    """授权窗口是不是已经过了。只在这个进程里判，不读授权文件。"""
    global _DEADLINE_SAID
    if GRANT_DEADLINE <= 0 or time.time() < GRANT_DEADLINE:
        return False
    if not _DEADLINE_SAID:
        _DEADLINE_SAID = True
        print(f"[{now()}] 授权窗口已到期（{dt.datetime.fromtimestamp(GRANT_DEADLINE):%Y-%m-%d %H:%M:%S}），"
              f"停在下一批之前", flush=True)
    return True


def stop_requested() -> bool:
    """要不要在下一批之前停手：面板的停止标记，或者授权窗口到期。"""
    if STOP_FILE is not None and STOP_FILE.exists():
        return True
    return deadline_passed()


def wait_or_stop(seconds: float) -> bool:
    if STOP_FILE is None and GRANT_DEADLINE <= 0:
        time.sleep(seconds)
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_requested():
            return True
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    return stop_requested()


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return dt.date.today().isoformat()


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _load_json(path: Path, what: str) -> dict:
    """读配置类 JSON；坏文件直接停，不退回默认值。"""
    if not path.exists():
        raise RuntimeError(f"{what} 不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"{what} 损坏：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{what} 结构不对（应为对象）")
    return data


def load_config() -> dict:
    """运行参数：个人偏好读有效快照，执行策略（轮次/预算形状）优先读 workflow.json。

    新工作区没有 `config/workflow.json` 时不再直接报"配置不存在"：执行策略落回
    `DEFAULT_CONFIG`（中性、不含个人值），个人偏好仍然全部来自方案快照。
    这样"空目录 → setup → 建资料 → 建方案 → 跑"能走通，也不会把旧用户的个人值带给新用户；
    真要覆盖执行策略，仍然写 `config/workflow.json`，优先级高于默认值。
    """
    if "config" not in _cache:
        raw = _read_config_file()
        effective = core.delivery_config(snapshot_state())
        merged = {**DEFAULT_CONFIG, **raw,
                  **{k: v for k, v in effective.items() if k != "rounds"}}
        merged["rounds"] = raw.get("rounds") or DEFAULT_CONFIG["rounds"]
        merged["snapshot_fingerprint"] = snapshot_state().get("fingerprint")
        _cache["config"] = merged
    return _cache["config"]


def _read_config_file() -> dict:
    """读执行策略文件；没有这个文件返回空字典，由 `DEFAULT_CONFIG` 兜底。"""
    return _load_json(CFG_PATH, "工作流配置") if CFG_PATH.exists() else {}


def snapshot_state() -> dict:
    """当前有效快照：候选人资料 + 求职方案 + 策略版本。

    没有方案就直接停：旧版本会退回 `config/workflow.json` 的个人值，那正是这次要拆掉的
    "静默回退个人默认"。
    """
    if "snapshot" not in _cache:
        snap = core.load(workspace_root())
        if snap is None:
            raise RuntimeError(
                "没有生效的求职方案：先创建并激活方案，或运行迁移工具生成 workspace.json")
        _cache["snapshot"] = snap
    return _cache["snapshot"]


def load_policy() -> dict:
    """写作规则：来自当前绑定的写作策略（旧 policy.json 已迁进策略里）。"""
    if "policy" not in _cache:
        data = core.delivery_policy(snapshot_state())
        if not int(data.get("max_len") or 0):
            raise RuntimeError("写作策略缺 max_len")
        _cache["policy"] = data
    return _cache["policy"]


def load_profile() -> dict:
    """候选人事实：来自活动候选人版本。"""
    if "profile" not in _cache:
        data = core.delivery_profile(snapshot_state())
        facts = data.get("facts") or []
        if not facts or any(not f.get("id") or not f.get("text") for f in facts):
            raise RuntimeError("候选人事实缺 id/text")
        _cache["profile"] = data
    return _cache["profile"]


def load_activity() -> dict:
    """招聘者最近活跃时间（`probe_activity.py` 写的侧文件）。

    坏文件按「一条记录都没有」处理：结果是没岗位准入、不发消息，属于安全侧。
    不缓存，因为每轮都可能被 probe 更新。
    """
    if not ACTIVITY.exists():
        return {}
    try:
        return json.loads(ACTIVITY.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"活跃记录读不了（{exc}），按全部未探过处理")
        return {}


def facts() -> list[tuple[str, str]]:
    base = [(str(f["id"]), str(f["text"])) for f in load_profile()["facts"]]
    return base + availability_facts()


def load_preferences() -> dict:
    """个人求职口径（到岗时间 / 每周天数 / 实习时长 / 城市 / 异地）。

    数据来自有效快照（候选人可用安排 + 方案目标城市/异地接受度），不再读写
    `greeting/preferences.json`。版本号沿用迁移前那个字符串：内容没变，旧稿就还能复验。
    """
    if "prefs" not in _cache:
        snap = snapshot_state()
        candidate = snap.get("candidate") or {}
        plan = snap.get("plan") or {}
        avail = candidate.get("availability") or {}
        target = plan.get("target") or {}
        data = {
            "version": prefs_version(snap),
            "start": c_scalar(avail.get("start")),
            "days_per_week": c_scalar(avail.get("days_per_week")),
            "months": c_scalar(avail.get("months")),
            "cities": c_values(target.get("cities")),
            "remote": c_scalar(target.get("accepts_relocation")),
        }
        _cache["prefs"] = data
    return _cache["prefs"]


def prefs_version(snap: dict | None = None) -> str:
    """口径版本：内容与迁移时一致就沿用旧版本号，真的改了才变（实现见 `boss_core.compat`）。"""
    return core.preferences_version(snap or snapshot_state())


def availability_facts() -> list[tuple[str, str]]:
    """口径 → 事实条目（P 开头）。写稿可以引用，事实校验也据此放行 5 天/6 个月这类数字。"""
    p = load_preferences()
    rows: list[tuple[str, str]] = []
    start = str(p.get("start") or "").strip()
    if start:
        rows.append(("P1", f"到岗时间：{start}"))
    days = p.get("days_per_week")
    if isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 7:
        rows.append(("P2", f"每周可到岗 {days} 天"))
    months = p.get("months")
    if isinstance(months, int) and not isinstance(months, bool) and 1 <= months <= 36:
        rows.append(("P3", f"可实习 {months} 个月"))
    cities = [str(c).strip() for c in (p.get("cities") or []) if str(c).strip()]
    if cities:
        rows.append(("P4", "可接受城市：" + "、".join(cities[:5])))
    remote = p.get("remote")
    if isinstance(remote, bool):
        rows.append(("P5", "异地线下：" + ("接受" if remote else "不接受")))
    return rows


def availability_clause() -> str:
    """写稿要求里关于"可用时间"的那一段：口径没填就不许写。"""
    rows = availability_facts()
    if rows:
        return ("\n\n【可用时间口径】只能按事实清单里 P 开头的条目写（"
                + "；".join(t for _, t in rows)
                + "）。没列出的到岗时间、每周天数、实习月数、城市、异地一律不写。")
    return ("\n\n【可用时间口径】本人没配置到岗/实习时长：不要写每周几天、实习几个月、"
            "能否异地、可到岗时间；只写简历里有的届别与在读状态。")


def fact_ids() -> set[str]:
    return {fid for fid, _ in facts()}


def api(method: str, path: str, payload: dict | None = None, timeout: float = 30.0):
    resp = requests.request(method, BASE + path, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.content else {}


class Busy(Exception):
    pass


class Lock:
    """Windows 命名互斥体：只查询/持有，不对任何进程发信号（B1）。"""

    def __init__(self, name: str = "dsh-boss-deliver-v2") -> None:
        self.name = name
        self.handle = None

    def __enter__(self):
        import ctypes

        self.handle = ctypes.windll.kernel32.CreateMutexW(None, True, self.name)
        if not self.handle:
            raise Busy("创建互斥体失败")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(self.handle)
            raise Busy("已有实例在跑（命名互斥体被占用）")
        return self

    def __exit__(self, *exc):
        import ctypes

        if self.handle:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
        return False


def append_event(event: str, job_id: str = "", **data) -> None:
    from boss_core.delivery_ledger import append
    append(QUEUE, event, job_id, **data)


def load_queue(repair: bool = False) -> list[dict]:
    """读事件台账；坏行默认让整轮停下（R8：不许静默丢发送证据）。"""
    if not QUEUE.exists():
        return []
    out: list[dict] = []
    bad: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(QUEUE.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError as exc:
            bad.append((lineno, line[:120], str(exc)))
            continue
        if not isinstance(rec, dict):
            bad.append((lineno, line[:120], "不是对象"))
            continue
        out.append(rec)
    if bad:
        if not repair:
            detail = "；".join(f"第 {n} 行：{why}" for n, _, why in bad[:5])
            raise RuntimeError(
                f"队列有 {len(bad)} 行损坏，停止发送以保留证据（{detail}）。"
                "确认后可加 --repair-queue 隔离坏行再跑。"
            )
        aside = QUEUE.with_name(f"queue.corrupt-{stamp()}.jsonl")
        with aside.open("a", encoding="utf-8") as fh:
            for lineno, raw, why in bad:
                fh.write(json.dumps({"lineno": lineno, "error": why, "raw": raw},
                                    ensure_ascii=False) + "\n")
        print(f"[warn] 已隔离 {len(bad)} 行坏行到 {aside.name}")
    return out


GREETING_EVENTS = {
    "validated", "revalidated", "rejected", "write_failed", "write_missing",
    "patch_failed", "verify_failed", "stale_greeting", "stale_jd", "stale_score",
    "stale_policy", "stale_profile", "stale_selection", "stale_prefs", "needs_review", "voided",
    "fact_reviewed",
}
DELIVERY_EVENTS = {"sent", "failed", "skipped", "unknown", "reserved", "sending"}

GREETING_KEYS = ("text", "sha", "jd_sha", "score", "policy_version", "profile_version",
                 "selection_version", "prefs_version", "writer", "model", "checks")


def fold(events: list[dict]) -> dict[str, dict]:
    """把事件折叠成三态：greeting_status / delivery_status（反馈单独存）。

    旧版「最后一条事件就是状态」会让一条 reply 事件覆盖发送状态（R5），这里分开。
    """
    state: dict[str, dict] = {}
    for ev in events:
        jid = str(ev.get("job_id") or "")
        if not jid:
            continue
        kind = str(ev.get("event") or "")
        cur = state.setdefault(jid, {
            "job_id": jid, "greeting_status": "", "delivery_status": "",
            "greeting": {}, "delivery": {}, "last_event": "", "last_at": "",
        })
        cur["last_event"] = kind
        cur["last_at"] = ev.get("at") or cur["last_at"]
        if kind in GREETING_EVENTS:
            cur["greeting_status"] = kind
            if kind in {"validated", "revalidated"}:
                cur["greeting"] = {k: ev.get(k) for k in GREETING_KEYS}
                cur["greeting"]["at"] = ev.get("at")      # 写稿时间：面板要显示"旧稿写于…"
            elif kind in {"rejected", "needs_review", "voided"}:
                cur["greeting"] = {"reason": ev.get("reason") or ev.get("issues"),
                                   "issues": ev.get("issues") or [],
                                   "text": ev.get("text") or ""}
            elif kind in {"write_failed", "write_missing"}:
                cur["greeting"] = {**cur.get("greeting", {}),
                                   "error": str(ev.get("error") or ev.get("reason") or "")[:200],
                                   "error_at": ev.get("at")}
            elif kind in {"stale_greeting", "stale_jd", "stale_score", "stale_policy",
                          "stale_profile", "stale_selection", "stale_prefs"}:
                cur["greeting"] = {**cur.get("greeting", {}), "stale": kind,
                                   "stale_queued": ev.get("queued"), "stale_current": ev.get("current"),
                                   "stale_at": ev.get("at")}
        elif kind in DELIVERY_EVENTS:
            cur["delivery_status"] = kind
            payload = {k: v for k, v in ev.items() if k not in {"event", "job_id", "at"}}
            if kind == "failed":
                payload["attempts"] = int(cur.get("delivery", {}).get("attempts") or 0) + 1
            elif kind in {"sent", "skipped"}:
                payload["attempts"] = int(cur.get("delivery", {}).get("attempts") or 0)
            cur["delivery"] = payload
    return state


FROZEN_DELIVERY = {"sent", "unknown", "reserved", "sending"}


def delivery_blocked(st: dict | None, max_attempts: int) -> bool:
    st = st or {}
    status = st.get('delivery_status')
    return status in FROZEN_DELIVERY or (status == 'failed' and int((st.get('delivery') or {}).get('attempts') or 0) >= max_attempts)


def frozen_for_prepare(st: dict | None, max_attempts: int) -> bool:
    """这个岗位能不能再写稿：已发/未知/在途一律冻结；失败超过次数也不再写。"""
    if not st:
        return False
    if st.get("greeting_status") in {"validated", "revalidated"}:
        return True
    return delivery_blocked(st, max_attempts)


def load_usage() -> dict:
    if USAGE.exists():
        try:
            data = json.loads(USAGE.read_text(encoding="utf-8"))
        except ValueError as exc:
            # 账本损坏时停止发送，避免把已发条数清零后重复消耗额度（B5）
            raise RuntimeError(f"额度账本损坏，停止发送以核对：{exc}") from exc
        if data.get("date") != today():
            if int(data.get("reserved") or 0) > int(data.get("confirmed") or 0):
                raise RuntimeError("上一日存在未结算的额度占用，需先对账再继续")
            data = {"date": today(), "confirmed": 0, "reserved": 0}
        return data
    seed = {"date": today(), "confirmed": 0, "reserved": 0}
    # 2026-10-28 之前由 v1 发过 12 条，当天首次运行时继承，避免超发
    if today() == "2026-10-28":
        seed["confirmed"] = 12
        seed["reserved"] = 12
    return seed


def save_usage(u: dict) -> None:
    tmp = USAGE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(u, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, USAGE)


def all_known_ids() -> set[str]:
    known: set[str] = set()
    for meta in api("GET", "/api/match/analyses").get("items") or []:
        aid = meta.get("analysis_id")
        if not aid:
            continue
        for item in (api("GET", f"/api/match/analyses/{aid}", timeout=60).get("matches") or []):
            jid = str(item.get("encrypt_job_id") or "")
            if jid:
                known.add(jid)
    return known


def jd_text(job_id: str) -> str:
    """取完整 JD（不截断），失败回空串。"""
    cached = job_desc_map().get(job_id)
    if cached is not None:
        return cached
    try:
        job = api("GET", f"/api/jobs/{job_id}", timeout=20)
    except Exception:
        return ""
    return str(job.get("job_desc") or "").strip()


#: 岗位正文缓存：一次分页拉全库，别再为每个岗位打一次 HTTP（800 次 = 20 秒）
_JOB_DESC: dict[str, str] = {}
#: 同一趟分页顺手留下的整条岗位。判"被硬条件挡掉"要看学历/经验/薪资，
#: 已经有了分页结果就不该再为每个岗位单独请求一次。
_JOB_META: dict[str, dict] = {}


def job_desc_map(force: bool = False) -> dict[str, str]:
    """{job_id: job_desc} 全库映射；分页 200 条拉，进程内缓存。"""
    if _JOB_DESC and not force:
        return _JOB_DESC
    offset = 0
    while True:
        try:
            batch = api("GET", f"/api/jobs?limit=200&offset={offset}", timeout=30).get("items") or []
        except Exception:
            break
        for job in batch:
            jid = str(job.get("encrypt_job_id") or "")
            if jid:
                _JOB_DESC[jid] = str(job.get("job_desc") or "").strip()
                _JOB_META[jid] = job
        if len(batch) < 200:
            break
        offset += 200
    return _JOB_DESC


def jd_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# 岗位名里的分隔符与装饰词：正文只保留「核心职能 + 招聘身份」
TITLE_SPLIT = re.compile(r"[/｜|、,，()（）\-—–]+")
TITLE_DECOR = {
    "助理", "实习", "校招", "应届", "急招", "兼职", "全职", "包三餐", "双休",
    "上市公司", "有转正机会", "可转正", "转正", "远程", "线下", "线上",
}
TITLE_IDENTITY = ("实习生", "培训生", "管培生", "校招")


def title_for_message(title: str, limit: int = 20) -> str:
    """正文里用的岗位简称：保留核心职能与招聘身份，不照抄一长串斜杠并列。

    只在标题过长时压缩；「培训生」这类身份不改写成「实习生」。
    """
    raw = (title or "").strip()
    if not raw:
        return raw
    # 括号里多是面试方式/方向/福利，正文称呼不要带上
    inner_match = re.search(r"[（(]([^）)]+)[）)]", raw)
    inner = inner_match.group(1).strip() if inner_match else ""
    stripped = re.sub(r"[（(][^）)]*[）)]", "", raw).strip() or raw
    # 括号外只剩身份词（"实习生（AI应用开发方向）"）时，用括号里的方向当核心职能
    if stripped in {"实习生", "实习", "助理", "校招", "应届", "管培生", "培训生"} and inner:
        core = re.sub(r"[（()）]", "", inner).strip()
        return (core + stripped)[:limit]
    if len(stripped) <= limit and not re.search(r"[/｜|、]", stripped):
        return stripped
    segs = [s.strip() for s in TITLE_SPLIT.split(raw) if s.strip()]
    keep = [s for s in segs if s not in TITLE_DECOR] or segs
    # 纯身份词（"实习生""助理"）不能当核心职能，否则会得到"实习生"这种称呼
    pure = {x for x in TITLE_IDENTITY} | {"助理"}
    body_segs = [s for s in keep if s not in pure] or keep
    core = next((s for s in body_segs if any(x in s for x in TITLE_IDENTITY)), None)
    if core is None:
        core = max(body_segs, key=len)
    identity = next((x for x in TITLE_IDENTITY if any(x in s for s in segs)), "")
    out = core if (not identity or identity in core) else core + identity
    return out[:limit]


# 硬条件/要求类句子的关键词；用来从**全文**里抽原文证据，不做截断
def input_limits() -> dict:
    """写作通道的输入上限，只从 policy.json 的 `input` 段读（生成端与桥接端共用）。"""
    cfg = load_policy().get("input") or {}
    return {
        "brief": int(cfg.get("brief_chars") or 1200),
        "total": int(cfg.get("evidence_total_chars") or 500),
        "line": int(cfg.get("evidence_line_chars") or 280),
        "lines": int(cfg.get("evidence_max_lines") or 20),
    }


EVIDENCE_KEY = re.compile(
    r"要求|任职|职责|负责|必须|优先|加分|条件|学历|本科|硕士|大专|届|每周|天/周|"
    r"个月|实习期|经验|熟悉|掌握|技能|工具|语言|英语|到岗|转正|城市|现场|坐班"
)

# 硬条件（届别/学历/每周天数/时长/到岗）优先，绝不被预算截掉
HARD_EVIDENCE_KEY = re.compile(
    r"届|学历|本科|硕士|大专|博士|应届|在读|每周|天/周|出勤|工作日|个月|半年|一年|到岗|入职|城市|现场|坐班|线下|远程|转正|全职"
)

# 独立标题行与福利宣传：不占证据预算
HEADING_LINE = re.compile(
    r"^[\s0-9一二三四五六七八九十\.、（）()]*"
    r"(岗位职责|职位描述|工作职责|岗位要求|任职要求|任职资格|职位要求|任职条件|工作内容|"
    r"我们提供|福利待遇|薪资福利|公司介绍|团队介绍|加分项|优先条件|岗位亮点)"
    r"[:：]?$"
)
PERK_LINE = re.compile(
    r"五险一金|六险一金|补充医疗|年终奖|绩效奖金|带薪年假|带薪假|弹性工作|不打卡|双休|周末双休|"
    r"团建|零食|下午茶|餐补|交通补|房补|节日福利|生日福利|股票期权|扁平管理|六日|大小周|"
    r"加入我们|您可获得|留用机会|实战经验|成长空间|团队氛围|培训体系|晋升通道|"
    r"团队和谐|氛围nice|氛围好|价值观|真诚为人|公司文化|企业文化|团建活动|年度旅游"
)


def _clean_jd_lines(text: str) -> list[str]:
    """去空行、独立标题行、纯福利行；保留原文措辞。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or HEADING_LINE.match(line) or PERK_LINE.search(line):
            continue
        out.append(line)
    return out


def _split_units(lines: list[str], line_max: int) -> list[str]:
    """把保留的行切成不超过 line_max 的单元（桥接器按行校验）。"""
    units: list[str] = []
    for line in lines:
        if len(line) <= line_max:
            units.append(line)
            continue
        for part in re.split(r"(?<=[。；;！!？?])", line):
            part = part.strip()
            while len(part) > line_max:
                units.append(part[:line_max])
                part = part[line_max:]
            if part:
                units.append(part)
    return units


def jd_evidence(text: str, hints: list[str] | None = None, limit: int | None = None) -> str:
    """写稿方看到的 JD 证据。

    两种取法，取更省的那种：

    1. 清洗后原文 ≤ 预算 → **直接给全文**（短 JD 最省也最准，摘要反而更长）；
    2. 超长 → 先挑硬条件、再挑与岗位特征词/匹配点相关的职责句，最后用普通要求句补足。

    标题行和福利宣传不占预算；原文措辞不改。
    """
    body = (text or "").strip()
    if not body:
        return ""
    lim = input_limits()
    total = int(limit if limit is not None else lim["total"])
    line_max = lim["line"]
    max_lines = lim["lines"]

    lines = _clean_jd_lines(body)
    cleaned = "\n".join(lines)
    if len(cleaned) <= total:
        units = _split_units(lines, line_max)[:max_lines]
        if sum(len(u) for u in units) <= total:
            return "\n".join(units)

    units = _split_units(lines, line_max)
    # hint 按词元匹配（"数字化流程优化" → 数字化/流程/优化），避免整串子串匹配不上
    tokens: set[str] = set()
    for h in (hints or []):
        for token in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(h)):
            if len(token) >= 2:
                tokens.add(token)
            for i in range(len(token) - 2):
                tokens.add(token[i:i + 3])
    tokens.discard("")
    hard = [u for u in units if HARD_EVIDENCE_KEY.search(u)]
    hinted = [u for u in units if u not in hard
              and any(tok in u for tok in tokens)]
    soft = [u for u in units if u not in hard and u not in hinted and EVIDENCE_KEY.search(u)]
    picked: list[str] = []
    used = 0
    hard_budget = int(total * 0.6)
    for idx_group, group in enumerate((hard, hinted, soft)):
        for unit in group:
            if len(picked) >= max_lines:
                break
            if len(unit) > line_max:
                continue
            cap = hard_budget if idx_group == 0 else total
            if used + len(unit) + 1 > cap:
                continue
            picked.append(unit)
            used += len(unit) + 1
    if not picked:
        return body[:min(total, line_max)]
    return "\n".join(picked)


def deepseek_summary(j: dict) -> tuple[str, str]:
    """写稿方看到的岗位摘要：优先 jd_brief，没有就用 DeepSeek 的判断合成。

    合成时只保留「岗位做什么 + 匹配点」，到岗安排这类事实由 profile 覆盖，不再重复占篇幅。
    """
    brief = str(j.get("jd_brief") or "").strip()
    if brief:
        return brief, "DeepSeek 摘要"
    parts: list[str] = []
    verdict = str(j.get("verdict") or "").strip()
    if verdict:
        parts.append(verdict)
    pros = [str(p).strip() for p in (j.get("pros") or []) if str(p).strip()]
    if pros:
        parts.append("匹配：" + "；".join(pros[:2]))
    text = " ".join(parts)
    # 到岗/届别/时长属于本人事实，摘要里重复只会挤掉岗位信息
    for pat in (r"可立即到岗[^。；]*", r"每\s*周\s*\d+\s*天[^。；]*",
                r"连续\s*\d+\s*个月[^。；]*", r"实习资格无冲突[^。；]*"):
        text = re.sub(pat, "", text)
    text = re.sub(r"\s*[，,；;]\s*(?=[。．.]|$)", "", text)
    text = re.sub(r"[，,；;]{2,}", "，", text)
    return text.strip(), "DeepSeek 判断（这条分析没有单独摘要）"


def jd_payload(j: dict) -> tuple[str, str, str]:
    """一次算出 (摘要, 摘要标签, 证据)。

    JD 清洗后能整段放进证据预算时，直接给全文并**省掉摘要**——摘要加摘录反而更长。
    """
    points = match_points(j)
    evidence = jd_evidence(j.get("jd_text") or "", points)
    cleaned_len = len("".join((j.get("jd_text") or "").split()))
    if evidence and len(evidence) >= cleaned_len * 0.95:
        return "（JD 较短，已附全文，无需摘要）", "已附全文", evidence
    brief, label = deepseek_summary(j)
    return brief, label, evidence


def match_points(j: dict) -> list[str]:
    """真正的人岗匹配点：优先 matched_skills，其次 pros；不拿公司名/岗位名凑数。"""
    points = [str(x) for x in (j.get("matched_skills") or []) if str(x).strip()]
    if not points:
        points = [str(x) for x in (j.get("pros") or []) if str(x).strip()]
    return points[:3]


def load_blocklist() -> set[str]:
    """人工否决名单；文件损坏时停止，不静默当空名单。"""
    if not BLOCKLIST.exists():
        return set()
    try:
        data = json.loads(BLOCKLIST.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"blocklist.json 损坏，停止发送：{exc}") from exc
    ids = data.get("ids") or {}
    return {str(k) for k in ids}


def poll(path: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = api("GET", path, timeout=30)
        except Exception:
            time.sleep(3)
            continue
        if last.get("status") in {"done", "error", "cancelled"}:
            return last
        # 没有任何任务在跑（程序重启过、任务丢了）就别再等：否则会空转到超时
        if str(last.get("status") or "") == "idle" and not last.get("task_id"):
            return last
        time.sleep(3)
    return last


def _refill_crawl(pages: int) -> dict:
    with Lock("dsh-boss-collection"):
        task = api("POST", "/api/crawl/start", {
            "max_pages": pages, "start_page": 1, "interval": 4.0,
            "use_search": True, "fetch_details": False,
            "detail_interval": 4.0, "store_blacklisted": True,
        }, timeout=30)
        tid = str(task.get("task_id") or "")
        if not tid:
            raise RuntimeError("补货抓取任务未启动")
        result = poll("/api/crawl/status", 900)
        if str(result.get("task_id") or "") != tid or result.get("status") != "done":
            raise RuntimeError("补货抓取任务未完成或归属已变化")
        return result


def refill(resume_id: str, pages: int, max_new: int) -> dict:
    """补货：抓 pages 页 → 补 JD → 对未分析岗位做匹配（A6）。"""
    info: dict = {}
    if pages and pages > 0:
        fin = _refill_crawl(pages)
        info["crawl"] = fin.get("status")
        info["inserted"] = int((fin.get("progress") or {}).get("inserted") or 0)
        # 双写：抓完立刻把库里的新岗位同步到磁盘语料库；失败只记一条，不影响本轮发送
        try:
            import corpus_export as ce
            cs = ce.sync(api=api)
            info["corpus"] = {"added": cs.get("added"), "db_total": cs.get("db_total"),
                              "disk_total": cs.get("disk_total")}
        except Exception as exc:  # noqa: BLE001
            info["corpus"] = {"error": f"{type(exc).__name__}: {exc}"[:160]}
            print(f"（语料库同步失败，不影响本轮发送：{type(exc).__name__}: {exc}）")
    listing = api("GET", "/api/jobs?limit=200", timeout=60).get("items") or []
    known = all_known_ids()
    by_id = {str(j.get("encrypt_job_id")): j for j in listing if j.get("encrypt_job_id")}
    ids = [i for i in by_id if i not in known]
    # 元数据预筛：学历/经验明显不符的不补 JD、不匹配，省掉后面整条链路的钱和时间
    kept, dropped = [], {}
    for i in ids:
        why = prefilter_reason(by_id[i])
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            kept.append(i)
    new = kept[:max_new]
    info["new"] = len(new)
    info["prefilter_dropped"] = sum(dropped.values())
    if dropped:
        info["prefilter_reasons"] = dropped
    if not new:
        return info
    desc = api("POST", "/api/jobs/fetch-descriptions", {"limit": len(new), "interval": 1.0}, timeout=30)
    if desc.get("task_id"):
        poll("/api/jobs/fetch-descriptions/status", 1800)
    done = {str(j.get("encrypt_job_id")) for j in
            (api("GET", "/api/jobs?limit=200&jd_status=completed", timeout=60).get("items") or [])}
    ready = [i for i in new if i in done]
    info["jd_ready"] = len(ready)
    if not ready:
        return info
    if is_peak_pricing():
        info["match"] = "skipped_peak"
        print("（高峰计价时段，跳过匹配：DeepSeek 空闲时段半价，留到窗口里跑）")
        return info
    m = api("POST", "/api/match/analyze", {"resume_id": resume_id, "job_ids": ready}, timeout=30)
    if m.get("task_id"):
        fin = poll("/api/match/analyze/status", 3600)
        info["match"] = fin.get("status")
        info["matched"] = fin.get("done")
    return info


# 元数据预筛：不花一分钱就能判定「不用投」的条件冲突，挡在补 JD 与匹配之前。
# 判定逻辑在 boss_core.conditions：岗位条件 × 候选人/方案快照，返回三态 + 依据。
def daily_pay(desc: str) -> tuple[float, float] | None:
    """把薪资描述折成「日薪区间」；认不出返回 None（旧接口，仅保留给报表/旧测试）。"""
    from boss_core import salary as _salary

    parsed = _salary.parse(desc)
    if not parsed:
        return None
    conf = {"days_per_month": 22, "months_per_year": 12}
    monthly = _salary.to_monthly(parsed, conf)
    if monthly is None:
        return None
    days = 22.0
    return (round(monthly[0] / days, 2), round(monthly[1] / days, 2))


def prefilter_reason(job: dict) -> str:
    """该岗位的硬冲突原因；没有冲突返回空串。

    学历/年限/每周天数/实习时长/城市/薪资/个人排除项都由当次候选人与方案决定，
    不再写死"硕士/博士不要""1-3 年不要"。
    """
    return core.prefilter_reason(job, snapshot_state())


def unscored_split(scored_ids) -> tuple[list[str], list[str]]:
    """有 JD 但没打过分的岗位，拆成「真该打的」和「被硬条件挡掉的」。

    面板上「有 JD − 已打分」这个差，以前是混在一起报的：11-01 查下来 189 个里有 182 个
    是日薪门槛（100–250 元/天）/学历/经验先被 `prefilter_reason` 挡掉的，采集器永远不会去打；
    真正待办的只有几个。混着报就成了"这个数怎么补都补不平"。
    """
    descs = job_desc_map()
    todo: list[str] = []
    blocked: list[str] = []
    for jid, desc in descs.items():
        if not desc or jid in scored_ids:
            continue
        if prefilter_reason(_JOB_META.get(jid) or {}):
            blocked.append(jid)
        else:
            todo.append(jid)
    return todo, blocked


def _hhmm(text: str) -> int:
    hh, mm = str(text).split(":")
    return int(hh) * 60 + int(mm)


def is_peak_pricing(cfg: dict | None = None, now: dt.datetime | None = None) -> bool:
    """现在是不是 DeepSeek 的高峰计价时段。

    规则（2026-10-29 用户确认）：北京时间周一至周五（不含法定节假日）
    09:00–12:00、14:00–18:00 为高峰；其余时段、周末、节假日全天为空闲，
    空闲价格是高峰的一半。匹配不要求时效性，所以高峰时段只抓取不匹配。
    """
    conf = (cfg or load_config()).get("pricing") or {}
    days = conf.get("peak_weekdays") or [1, 2, 3, 4, 5]
    windows = conf.get("peak_windows") or ["09:00-12:00", "14:00-18:00"]
    holidays = {str(x) for x in (conf.get("holidays") or [])}
    now = now or dt.datetime.now()
    if now.date().isoformat() in holidays:
        return False
    if now.isoweekday() not in days:
        return False
    cur = now.hour * 60 + now.minute
    for spec in windows:
        try:
            start, end = (int(_hhmm(x)) for x in str(spec).split("-"))
        except (ValueError, TypeError):
            continue
        if start <= end and start <= cur <= end:
            return True
        if start > end and (cur >= start or cur <= end):
            return True
    return False


def analysis_index(resume_id: str) -> dict[str, dict]:
    """每个岗位只保留**最新一次分析**里的那条结果（A1）。

    关键点：不做历史回退。最新分析把岗位判成低分/否决/失败，就按最新结论处理，
    不能因为「更早那次给了高分」又把岗位放回队列。

    Returns:
        `{job_id: {item, analysis_id, created_at, jd_text, jd_sha}}`。
    """
    out: dict[str, dict] = {}
    # 一次拿全量索引：以前逐条 GET /api/match/analyses/{id}，800 条要 20 秒，
    # 面板看起来像"必须刷新才出数据"。
    try:
        bulk = api("GET", f"/api/match/index?resume_id={resume_id}", timeout=120).get("items") or {}
    except Exception:
        bulk = {}
    if bulk:
        for jid, row in bulk.items():
            item = (row or {}).get("item") or {}
            text = jd_text(jid)
            out[str(jid)] = {"item": item, "analysis_id": (row or {}).get("analysis_id"),
                             "created_at": float((row or {}).get("analysis_at") or 0),
                             "scored_resume_id": (row or {}).get("resume_id") or resume_id,
                             "scored_resume_title": (row or {}).get("resume_title") or "",
                             "jd_text": text, "jd_sha": jd_sha(text)}
        return out
    # 老版本程序没这个接口时退回逐条取（慢，但不会没数据）
    # B2：不要求整个 analysis 跑完——逐岗结果落盘即可用；合法性由条目自己的
    # stage/status/JD 指纹判定，这里只排除「出错/取消」的整包。
    metas = [m for m in (api("GET", "/api/match/analyses").get("items") or [])
             if str(m.get("status") or "") in {"done", "running"}
             and m.get("resume_id") == resume_id]
    metas.sort(key=lambda a: a.get("created_at") or 0, reverse=True)
    for meta in metas:
        aid = meta.get("analysis_id")
        created = float(meta.get("created_at") or 0)
        for it in api("GET", f"/api/match/analyses/{aid}", timeout=60).get("matches") or []:
            jid = str(it.get("encrypt_job_id") or "")
            if not jid or jid in out:
                continue  # 已经有更新的分析结果，旧的不参与判断
            text = jd_text(jid)
            out[jid] = {"item": it, "analysis_id": aid, "created_at": created,
                        "jd_text": text, "jd_sha": jd_sha(text)}
    return out


def analysis_block_reason(entry: dict, current_jd_sha: str) -> str:
    """这条分析结果能不能作为准入依据；不能则返回原因。

    - `stage`/`status`：只有「精析跑完且成功」才算数，粗筛分与失败都不算（A2）。
    - `source_jd_sha`：有记录且不等于当前 JD 指纹 → 说明评的不是这份 JD（A1），不认。
    - 没记录 `source_jd_sha`（早期版本）→ **照样用**，只标 `jd_unverified`。
      2026-11-01 用户定：不要为了历史缺陷做防御设计，旧分数有效；
      真正的重写判断交给写稿（正文按当前 JD 写）和发送前复核。
    """
    it = entry.get("item") or {}
    stage = str(it.get("stage") or "")
    status = str(it.get("status") or "")
    if not stage or not status:
        # 2026-11-01 用户定：旧版分数（没有 stage/status）照样有效。它们是当时按完整 JD 打的分，
        # 只是那时候还没有两段式字段。别再为了历史缺陷把它们判死。
        return ""
    if status != "succeeded":
        return f"status_{status}"
    if stage != "full":
        return f"stage_{stage}"
    src = str(it.get("source_jd_sha") or "")
    if src and src != current_jd_sha:
        return "stale_jd"
    return ""


def block_verdicts() -> list[str]:
    """模型结论里视为"这一轮不要投"的词，来自评分策略的 `stage2.block_verdicts`。

    旧个人策略里带着「时长待核实」「8 个月」，新用户不继承；策略没配就用通用的四个。
    """
    try:
        content = resolve_strategy(snapshot_state(), "matching").get("content") or {}
        words = (content.get("stage2") or {}).get("block_verdicts")
        if isinstance(words, list) and words:
            return [str(w) for w in words]
    except Exception:  # noqa: BLE001 - 策略读不到时用通用词表，不阻断准入
        pass
    return ["暂缓", "不建议", "不匹配", "谨慎投递"]


def collect_eligible(min_score: int, resume_id: str) -> list[dict]:
    """岗位准入：不要求已有话术；读取完整 JD 并记哈希（A1/A2/A5）。

    - 同岗只看**最新一次分析**的结论，不翻旧结果（A1）；
    - 只有 `stage=full` + `status=succeeded` + JD 指纹对得上才准 (A2)；
    - 活跃门槛（`ACTIVITY_GATE`）：BOSS 只给「招聘者最近活跃时间」，按最近活跃
      天数筛；没探过的默认不进，跑 `probe_activity.py --eligible` 补齐。
    """
    out = []
    blocked = load_blocklist()
    delivery_state = fold(load_queue())
    max_attempts = int(load_config().get('max_delivery_attempts') or 2)
    act_map = load_activity() if ACTIVITY_GATE else {}
    act_days = float((load_config().get("activity") or {}).get("within_days") or 7)
    skipped_inactive = skipped_unknown = skipped_prefilter = 0
    skipped_block = {}

    for jid, entry in analysis_index(resume_id).items():
        it = entry["item"]
        # A new analysis can lose delivered_at. The cross-analysis send ledger
        # still freezes sent/unknown/in-flight jobs, including panel counts.
        if delivery_blocked(delivery_state.get(jid), max_attempts):
            continue
        if jid in blocked:
            continue
        text = entry["jd_text"]
        sha = entry["jd_sha"]
        why = analysis_block_reason(entry, sha)
        if why:
            skipped_block[why] = skipped_block.get(why, 0) + 1
            continue
        if int(it.get("match_score") or 0) < min_score:
            continue
        if it.get("is_blacklisted") or it.get("company_blacklisted") or it.get("jd_blacklisted"):
            continue
        if str(it.get("jd_status") or "") != "completed" or it.get("auto_delivery_blocked"):
            continue
        if it.get("greet_in_cooldown") or it.get("delivered_at") or it.get("deliver_status") in {"ok", "sending"}:
            continue
        if any(word in str(it.get("verdict") or "") for word in block_verdicts()):
            continue
        # Analysis rows omit full JD/city fields; use the local job facts already fetched.
        if prefilter_reason({**it, **(_JOB_META.get(jid) or {}), "job_desc": text}):
            skipped_prefilter += 1
            continue
        act = act_map.get(jid) or {}
        active_age = None
        if ACTIVITY_GATE:
            if not act.get("active_at"):
                skipped_unknown += 1
                continue
            active_age = (time.time() - float(act["active_at"])) / 86400.0
            if active_age > act_days:
                skipped_inactive += 1
                continue
        out.append({
            "job_id": jid,
            "analysis_id": entry["analysis_id"],
            "analysis_at": entry["created_at"],
            "scored_resume_id": entry.get("scored_resume_id") or resume_id,
            "scored_resume_title": entry.get("scored_resume_title") or "",
            "jd_unverified": not str(it.get("source_jd_sha") or ""),
            "score_legacy": not str(it.get("stage") or ""),
            "company": it.get("brand_name") or "",
            "title": it.get("job_name") or "",
            "salary": (_JOB_META.get(jid) or {}).get('salary_desc') or it.get('salary_desc') or '',
            "city": (_JOB_META.get(jid) or {}).get('city_name') or '',
            "job_type": (_JOB_META.get(jid) or {}).get('job_type'),
            "score": it.get("match_score") or 0,
            "verdict": str(it.get("verdict") or "")[:160],
            "current_greeting": str(it.get("greeting") or ""),
            "jd_text": text,
            "jd_sha": sha,
            "candidate_material": it.get("candidate_material"),
            "scoring_rule": it.get("scoring_rule"),
            "evaluation_mode": it.get("evaluation_mode"),
            "jd_brief": str(it.get("jd_brief") or ""),
            "hard_constraints": it.get("hard_constraints") or [],
            "matched_skills": [str(x) for x in (it.get("matched_skills") or [])][:6],
            "pros": [str(x) for x in (it.get("pros") or [])][:4],
            "cons": [str(x) for x in (it.get("cons") or [])][:4],
            "advice": str(it.get("advice") or "")[:200],
            "active_desc": str(act.get("desc") or ""),
            "active_age_days": None if active_age is None else round(active_age, 2),
        })
    if skipped_block:
        detail = "，".join(f"{k} {v}" for k, v in sorted(skipped_block.items()))
        print(f"分析有效性：剔除 {sum(skipped_block.values())} 个（{detail}）")
    if ACTIVITY_GATE and (skipped_inactive or skipped_unknown):
        print(f"活跃筛选：剔除 {skipped_inactive} 个超 {act_days:g} 天未活跃、"
              f"{skipped_unknown} 个没探过活跃时间")
    if skipped_prefilter:
        print(f"硬条件筛选：剔除 {skipped_prefilter} 个（学历/经验/日薪不符）")
    out.sort(key=lambda x: -int(x["score"]))
    return out


def reanalysis_queue(min_score: int, resume_id: str, limit: int = 200) -> list[str]:
    """需要重新做分析的岗位：旧数据（无 stage/status）或 JD 已变、且分数看着够的。

    只把「可能合格」的挑出来重算，避免整库重跑；已发送/在途/冷却中的不动。
    """
    need: list[str] = []
    for jid, entry in analysis_index(resume_id).items():
        it = entry["item"]
        if int(it.get("match_score") or 0) < min_score:
            continue
        if it.get("greet_in_cooldown") or it.get("delivered_at") or it.get("deliver_status") in {"ok", "sending"}:
            continue
        if analysis_block_reason(entry, entry["jd_sha"]):
            need.append(jid)
        if len(need) >= limit:
            break
    return need


def build_request(batch_id: str, jobs: list[dict]) -> Path:
    """桥接请求：带 request_id / 版本；JD 用 DeepSeek 摘要 + 原文摘录，不再给全文。"""
    REQ_DIR.mkdir(parents=True, exist_ok=True)
    policy, profile = load_policy(), load_profile()
    lim = input_limits()
    payload = {
        "request_id": batch_id,
        "policy_version": policy["version"],
        "profile_version": profile["version"],
        "facts": [{"id": i, "text": t} for i, t in facts()],
        "requirements": policy["requirements"],
        "jobs": [
            {
                "job_id": j["job_id"],
                "company": j["company"],
                "title": j["title"],
                "title_for_message": title_for_message(j["title"]),
                "jd_brief": jd_payload(j)[0][:lim["brief"]],
                "jd_evidence": [ln for ln in jd_evidence(j["jd_text"], match_points(j)).splitlines() if ln],
                "jd_meta": {"jd_len": len(j.get("jd_text") or ""), "jd_sha": j["jd_sha"]},
                "hard_constraints": j.get("hard_constraints") or [],
                "match_points": match_points(j),
                "draft": "",
            }
            for j in jobs
        ],
    }
    path = REQ_DIR / f"{batch_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def bridge_timeout() -> int:
    """桥接超时：一个数同时给子进程超时和桥接器自己的 --timeout（否则先被杀的是外层）。"""
    cfg = load_config()
    return max(60, min(600, int(cfg.get("bridge_timeout_sec") or 540)))


def call_bridge(request_path: Path, timeout: float | None = None) -> dict:
    """调用桥接器；等待期间每 15 秒打一行心跳，避免几分钟没有任何输出像卡死。"""
    limit = int(timeout or bridge_timeout())
    proc = subprocess.Popen(
        [sys.executable, "-B", str(BRIDGE), "--request", str(request_path),
         "--timeout", str(limit)],
        cwd=str(WS), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    started = time.monotonic()
    while True:
        try:
            out, err = proc.communicate(timeout=15)
            break
        except subprocess.TimeoutExpired:
            waited = int(time.monotonic() - started)
            if waited >= 30:  # 30 秒内正常批次就返回了，不必刷屏
                print(f"    … 已等 {waited}s / 上限 {limit}s（模型这次偏慢，仍在跑）", flush=True)
            if waited >= limit + 30:
                proc.kill()
                proc.communicate()
                raise RuntimeError(f"桥接器等待超过 {limit + 30}s，已终止；本批不发送")
    if proc.returncode:
        raise RuntimeError(f"桥接器退出码 {proc.returncode}: {(out or err or '')[:300]}")
    summary = json.loads(out)
    result = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    spent = round(time.monotonic() - started, 1)
    slow = "  ← 偏慢" if spent > 120 else ""
    print(f"    批完成：{len(result.get('items') or [])} 条，{spent} 秒{slow}", flush=True)
    return result


def normalize_head(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?;；:：\-—_\"'“”‘’()（）]", "", text)[:8]


def validate(text: str, used_facts: list[str], recent_heads: set[str],
             policy: dict | None = None, check_heads: bool | None = None) -> list[str]:
    """格式校验；字数规则只认 policy.json（200 上限、正文非空）。"""
    p = policy or load_policy()
    max_len = int(p["max_len"])
    min_len = int(p.get("min_len") or 1)
    if check_heads is None:
        check_heads = bool(p.get("head_dedup", True))
    issues: list[str] = []
    body = (text or "").strip()
    if not body:
        return ["正文为空"]
    if not min_len <= len(body) <= max_len:
        issues.append(f"字数 {len(body)} 不在 {min_len}-{max_len}")
    if "\n" in body:
        issues.append("含换行")
    for word in p.get("forbidden") or []:
        if word in body:
            issues.append(f"含禁用词 {word}")
    if PHONE.search(body):
        issues.append("含疑似电话")
    if PLACEHOLDER.search(body):
        issues.append("含占位符")
    if any(fid not in fact_ids() for fid in used_facts):
        issues.append("引用了未知事实ID")
    head = normalize_head(body)
    if check_heads and head in recent_heads:
        issues.append(f"开头与近期重复：{head}")
    return issues


def hard_conflict_patterns() -> list[str]:
    """个人否决词（正则），来自方案 `requirements.hard_conflicts`。

    旧个人方案里是「8 个月 / 每周 6 天 / 全职到岗…」；新用户默认空，不做这层否决。
    """
    plan = snapshot_state().get("plan") or {}
    return [str(x) for x in c_values((plan.get("requirements") or {}).get("hard_conflicts"))]


def fact_issues(text: str, used_facts: list[str], hard: list[str], require_facts: bool) -> list[str]:
    """事实层的最低检查：引用了哪些事实、有没有和硬条件明显打架。

    这不是完整的事实核对，只是把「格式过了就等于事实核对过」的缺口记下来（A3）。
    """
    issues: list[str] = []
    if require_facts and not used_facts:
        issues.append("未给出引用的事实ID")
    patterns = hard_conflict_patterns()
    for h in hard:
        if not str(h).strip():
            continue
        for pattern in patterns:
            try:
                found = re.search(pattern, str(h))
            except re.error:
                continue
            if found and found.group(0) in text:
                issues.append(f"正文与硬条件冲突：{h}")
                break
    return issues


# 事实复核规则版本：改了判定逻辑就改这个号，历史事件保留可追溯
# v2：型号/版本号里的数字（bge-reranker-v2-m3）不再算作"事实里有的数字"
FACT_RULE = "dsh-rule-v2"
NUMBER = re.compile(r"\d+")
# 型号/版本号里的数字不算「编造的数字」：bge-reranker-v2-m3、v2.1、Python3 都不该被当成年限
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*")


def _plain_numbers(text: str) -> list[str]:
    """只取"独立数字"：先把带字母的型号/版本号整体抹掉，再看剩下的阿拉伯数字。"""
    return NUMBER.findall(IDENTIFIER.sub(" ", text or ""))


def ungrounded_numbers(text: str) -> list[str]:
    """正文里出现、但事实清单里没有的数字（防编造年限/人数/期数）。

    允许年份简写：事实有「2027」时，正文写「27届」不算编造；反过来也认。
    """
    known = " ".join(t for _, t in facts())
    known_numbers = set(_plain_numbers(known))
    variants = set(known_numbers)
    for n in known_numbers:
        if len(n) == 4 and n.startswith("20"):
            variants.add(n[2:])          # 2027 -> 27
        elif len(n) == 2:
            variants.add("20" + n)       # 27 -> 2027
    return [n for n in _plain_numbers(text) if n not in variants]


def review_facts(text: str, used_facts: list[str], hard: list[str]) -> tuple[bool, list[str], dict]:
    """桥接稿的事实复核（A3）：格式之外，还要过这四关才允许进待发队列。

    规则（`dsh-rule-v1`，确定性、可复算）：

    1. 必须引用事实 ID，且 ID 都在 `profile.json` 里；
    2. 正文里的阿拉伯数字必须能在事实清单里找到（防编造「3 年经验」「两轮面试」之类）；
    3. 不能和岗位硬条件明显打架（届别/时长/全职等）；
    4. 正文不少于 30 字。

    Returns:
        (是否通过, 问题列表, 证据字典)
    """
    reasons: list[str] = []
    if not used_facts:
        reasons.append("未引用任何事实ID")
    bad = [f for f in used_facts if f not in fact_ids()]
    if bad:
        reasons.append("引用了不存在的事实ID：" + ",".join(bad))
    ungrounded = ungrounded_numbers(text)
    if ungrounded:
        reasons.append("数字在事实清单里找不到：" + ",".join(ungrounded))
    reasons += fact_issues(text, used_facts, hard, require_facts=True)
    checks = {
        "rule": FACT_RULE,
        "facts": used_facts,
        "ungrounded_numbers": ungrounded,
        "hard_conflict": [r for r in reasons if r.startswith("正文与硬条件冲突")],
    }
    return (not reasons), reasons, checks


def patch_greeting(analysis_id: str, job_id: str, text: str) -> None:
    api("PATCH", f"/api/match/{analysis_id}/greeting",
        {"encrypt_job_id": job_id, "greeting": text}, timeout=30)


def program_greeting(analysis_id: str, job_id: str) -> str:
    detail = api("GET", f"/api/match/analyses/{analysis_id}", timeout=60)
    for it in detail.get("matches") or []:
        if str(it.get("encrypt_job_id")) == job_id:
            return str(it.get("greeting") or "")
    return ""


def program_items(analysis_id: str) -> dict[str, dict]:
    """一次性取一份分析里的岗位现状，用于发送前复核（B7）。"""
    detail = api("GET", f"/api/match/analyses/{analysis_id}", timeout=60)
    return {str(it.get("encrypt_job_id")): it for it in (detail.get("matches") or [])}


def analysis_snapshot(j: dict) -> dict:
    return {k: j.get(k) for k in ('analysis_id', 'analysis_at', 'score', 'verdict',
            'matched_skills', 'missing_skills', 'pros', 'cons', 'hard_constraints', 'jd_sha',
            'candidate_material', 'scoring_rule', 'evaluation_mode')}


def _greeting_meta(j: dict, text: str) -> dict:
    policy, profile = load_policy(), load_profile()
    return {
        'analysis_id': j.get('analysis_id'),
        'company': j.get('company'), 'title': j.get('title'),
        'analysis_snapshot': analysis_snapshot(j),
        "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "jd_sha": j.get("jd_sha"),
        "score": j.get("score"),
        "policy_version": policy["version"],
        "profile_version": profile["version"],
        "selection_version": policy.get("selection_version") or "selection-legacy",
        "prefs_version": load_preferences().get("version") or "",
    }


def prepare(eligible: list[dict], state: dict, budget: int, batch_size: int,
            recent_heads: set[str]) -> list[dict]:
    """桥接写稿：冻结态一律不写；格式过 + 事实引用过才进 validated。"""
    cfg = load_config()
    max_attempts = int(cfg.get("max_delivery_attempts") or 2)
    # Reconcile expiry before choosing work: a read-only panel report cannot mark
    # old validated drafts stale. Otherwise they stay frozen until a send attempt.
    ready_jobs(eligible, state, mutate=True)
    state = fold(load_queue())
    todo = [j for j in eligible if not frozen_for_prepare(state.get(j["job_id"]), max_attempts)]
    todo = todo[:budget]
    prepared: list[dict] = []
    for start in range(0, len(todo), batch_size):
        if stop_requested():
            break
        chunk = todo[start:start + batch_size]
        batch_no = start // batch_size + 1
        batch_id = f"b{today().replace('-', '')}-{batch_no}-{int(time.time()) % 10000}"
        total_batches = (len(todo) + batch_size - 1) // batch_size
        print(f"  [写稿] 批 {batch_no}/{total_batches}：{len(chunk)} 个岗位", flush=True)
        req = build_request(batch_id, chunk)
        try:
            result = call_bridge(req)
        except Exception as exc:  # 写稿失败只影响本批
            for j in chunk:
                append_event("write_failed", j["job_id"], batch_id=batch_id, error=str(exc)[:200])
            continue
        by_id = {str(it.get("job_id")): it for it in (result.get("items") or [])}
        for j in chunk:
            it = by_id.get(j["job_id"])
            if not it:
                append_event("write_missing", j["job_id"], batch_id=batch_id)
                continue
            text = str(it.get("greeting") or "").strip()
            used = [str(x) for x in (it.get("used_fact_ids") or [])]
            issues = validate(text, used, recent_heads)
            decision = str(it.get("decision") or "")
            status = str(it.get("validation_status") or "")
            if status == "rejected" or decision == "reject" or issues:
                bridge_issues = it.get('checks') or []
                if not isinstance(bridge_issues, list):
                    bridge_issues = [str(bridge_issues)]
                issues = list(dict.fromkeys([*issues, *map(str, bridge_issues)]))
                if it.get('blocked_reason'):
                    issues.append(str(it['blocked_reason'])[:200])
                append_event("rejected", j["job_id"], batch_id=batch_id, text=text,
                             issues=issues, status=status, decision=decision,
                             writer_input_trace=result.get("input_trace"), writer_input_sha=result.get("input_trace_sha"),
                             used_fact_ids=used, analysis_id=j.get("analysis_id"), analysis_snapshot=analysis_snapshot(j))
                continue
            if decision == "needs_review" or status == "needs_fact_review":
                # 桥接器只做结构校验，事实层必须由这里复核过才能放行（A3）
                ok, f_issues, checks = review_facts(text, used, j.get("hard_constraints") or [])
                append_event("fact_reviewed", j["job_id"], batch_id=batch_id, text=text,
                             rule=FACT_RULE, checks=checks, issues=f_issues)
                if not ok:
                    append_event("needs_review", j["job_id"], batch_id=batch_id, text=text,
                                 reason="事实层未通过", issues=f_issues,
                                 writer_input_trace=result.get("input_trace"), writer_input_sha=result.get("input_trace_sha"),
                                 used_fact_ids=used, analysis_id=j.get("analysis_id"), analysis_snapshot=analysis_snapshot(j))
                    continue
            try:
                patch_greeting(j["analysis_id"], j["job_id"], text)
            except Exception as exc:
                append_event("patch_failed", j["job_id"], batch_id=batch_id, error=str(exc)[:200])
                continue
            if program_greeting(j["analysis_id"], j["job_id"]) != text:
                append_event("verify_failed", j["job_id"], batch_id=batch_id)
                continue
            meta = _greeting_meta(j, text)
            append_event("validated", j["job_id"], batch_id=batch_id, text=text,
                         writer=result.get("writer", "http"), model=result.get("model"),
                         writer_input_trace=result.get("input_trace"), writer_input_sha=result.get("input_trace_sha"), used_fact_ids=used,
                         checks={"format": "ok", "facts": f"ok:{FACT_RULE}",
                                 "business": "llm_writer"},
                         **meta)
            recent_heads.add(normalize_head(text))
            prepared.append({**j, "text": text, "sha": meta["sha"]})
            time.sleep(0.2)
    return prepared


def ready_jobs(eligible: list[dict], state: dict, mutate: bool = True) -> list[dict]:
    """可发送集合：正文/哈希/JD/分数/版本全部对得上才算（A5/B7）。

    Args:
        mutate: 是否允许往台账追加 stale/revalidated 事件；dry-run 传 False 只看不改。
    """
    policy, profile = load_policy(), load_profile()
    cfg = load_config()
    max_attempts = int(cfg.get("max_delivery_attempts") or 2)
    out: list[dict] = []

    def note(event: str, **kw) -> None:
        if mutate:
            append_event(event, j["job_id"], **kw)

    for j in eligible:
        st = state.get(j["job_id"], {})
        if st.get("greeting_status") not in {"validated", "revalidated"}:
            continue
        g = st.get("greeting") or {}
        text = str(g.get("text") or "")
        if not text:
            note("stale_greeting", reason="台账里没有正文")
            continue
        ds = st.get("delivery_status")
        if ds in FROZEN_DELIVERY:
            continue
        if ds == "failed" and int((st.get("delivery") or {}).get("attempts") or 0) >= max_attempts:
            continue
        # 正文必须与程序里存的一致；JD 或分数变了就不发
        if program_greeting(j["analysis_id"], j["job_id"]) != text:
            note("stale_greeting", reason="程序内正文与队列不一致")
            continue
        if g.get("jd_sha") and g["jd_sha"] != j.get("jd_sha"):
            note("stale_jd", queued=g.get("jd_sha"), current=j.get("jd_sha"))
            continue
        if g.get("score") is not None and int(g["score"]) != int(j["score"]):
            note("stale_score", queued=g.get("score"), current=j["score"])
            continue
        # 画像升级（或旧稿没有画像来源）：旧稿的措辞是按旧事实库写的，必须重写。
        # 只对未发送、非在途/未决的岗位生效；sent/unknown 已在上面被 FROZEN_DELIVERY 挡住。
        if not g.get("profile_version") or g["profile_version"] != profile["version"]:
            note("stale_profile", queued=g.get("profile_version"),
                 current=profile["version"])
            continue
        # 个人口径（到岗/时长/城市）变了：旧稿里关于可用时间的措辞可能已经不成立
        if str(g.get("prefs_version") or "") != str(load_preferences().get("version") or ""):
            note("stale_prefs", queued=g.get("prefs_version"),
                 current=load_preferences().get("version") or "")
            continue
        # 选材策略变了（写什么变了，不只是措辞）：旧稿必须重写，不能贴新版本继续用
        if (g.get("selection_version") or "") != (policy.get("selection_version") or ""):
            note("stale_selection", queued=g.get("selection_version"),
                 current=policy.get("selection_version"))
            continue
        if not g.get("policy_version"):
            # 没有 policy 版本但有画像版本：按当前规则复验一次（可审计）
            issues = validate(text, [], set())
            if issues:
                note("rejected", text=text, issues=issues, reason="旧稿复验未过")
                continue
            meta = _greeting_meta(j, text)
            note("revalidated", text=text, writer=g.get("writer") or "legacy",
                 checks={"format": "ok", "facts": "not_provided", "business": "legacy"}, **meta)
            g = {**g, **meta}
        elif g["policy_version"] != policy["version"]:
            # 只有文风规则变了：200 字/禁用词仍过 → 只算文风漂移，不强制重写
            issues = validate(text, [], set())
            if issues:
                note("stale_policy", queued=g["policy_version"],
                     current=policy["version"], profile=g["profile_version"], issues=issues)
                continue
            meta = _greeting_meta(j, text)
            note("revalidated", text=text, writer=g.get("writer") or "legacy",
                 checks={"format": "ok", "facts": "not_provided",
                         "business": f"policy_drift:{g.get('policy_version')}->{policy['version']}"},
                 **meta)
            g = {**g, **meta}
        out.append({**j, "text": text, "sha": g.get("sha", ""),
                    "writer": g.get("writer"), "model": g.get("model"),
                    "policy_version": g.get("policy_version"),
                    "profile_version": g.get("profile_version")})
    return out


def recheck_group(items: list[dict], blocked: set[str]) -> tuple[list[dict], list[str]]:
    """每组发送前重新核对：否决名单、程序内正文、是否已被发过（B7）。"""
    keep: list[dict] = []
    notes: list[str] = []
    by_aid: dict[str, list[dict]] = {}
    for j in items:
        by_aid.setdefault(j["analysis_id"], []).append(j)
    for aid, group in by_aid.items():
        try:
            current = program_items(aid)
        except Exception as exc:
            notes.append(f"复核 {aid} 失败：{exc}")
            for j in group:
                append_event("unknown", j["job_id"], reason="发送前复核接口失败")
            continue
        for j in group:
            jid = j["job_id"]
            if jid in blocked:
                append_event("voided", jid, reason="在人工否决名单里")
                notes.append(f"{jid} 在否决名单，跳过")
                continue
            it = current.get(jid) or {}
            if it.get("delivered_at") or str(it.get("deliver_status")) in {"ok", "sending"}:
                append_event("sent", jid, source="program_state",
                             sender_account_id=it.get('sender_account_id') or '',
                             application_id=it.get('application_id') or '',
                             at_sent=str(it.get("delivered_at") or ""))
                notes.append(f"{jid} 程序里已标记投递，按已发送记账")
                continue
            if str(it.get("greeting") or "") != j["text"]:
                append_event("stale_greeting", jid, reason="发送前正文与程序不一致")
                notes.append(f"{jid} 正文被改过，跳过")
                continue
            if it.get("is_blacklisted") or it.get("company_blacklisted") or it.get("jd_blacklisted"):
                append_event("voided", jid, reason="公司/岗位在黑名单")
                notes.append(f"{jid} 已在黑名单，跳过")
                continue
            if it.get("greet_in_cooldown"):
                append_event("skipped", jid, reason="冷却期")
                continue
            try:
                current_job = api("GET", f"/api/jobs/{jid}", timeout=20)
                condition_reason = prefilter_reason(current_job)
            except Exception:
                notes.append(f"{jid} 当前岗位条件无法核对，跳过")
                continue
            if condition_reason:
                append_event("voided", jid, reason=f"当前硬条件不符合：{condition_reason}")
                notes.append(f"{jid} 当前硬条件不符合：{condition_reason}")
                continue
            keep.append(j)
    return keep, notes


# 只认「连接都没建起来」这一类失败：程序在 client.connect() 处直接抛错，消息没上线，
# 同轮重试一次是安全的。发送中途的 timeout 语义模糊，绝不自动重试。
PRESEND_ERROR = re.compile(r"连聊天通道失败")


def reconcile_intents(state: dict) -> int:
    """崩溃对账：只有 `sending` 意图、没有终态事件的岗位，补记 unknown。

    不释放额度占用（`usage.json` 里的 reserved 保持不动），也不重发；
    只是把「在途但不知结果」变成台账上看得见的 unknown，后续轮次照冻结处理。
    """
    terminal = {"sent", "failed", "skipped", "unknown"}
    fixed = 0
    for jid, st in state.items():
        if str(st.get("delivery_status") or "") != "sending":
            continue
        if str(st.get("last_event") or "") in terminal:
            continue
        append_event("unknown", jid, reason="崩溃恢复：只有发送意图，没有逐项结果")
        fixed += 1
    return fixed


def _deliver_once(items: list[dict], usage: dict, aid: str,
                  retried: bool = False) -> tuple[int, int, int, str, list[tuple[dict, str]]]:
    """发起一批发送并逐项结算；返回 (ok, failed, skipped, aborted, 可重试失败项)。

    A3 的三条硬约束：
    1. 请求前先落**逐岗** `sending` 意图与额度占用，崩在请求中间也能对账；
    2. 返回的岗位必须恰好属于本次请求集合且不重复，出现额外/重复 id 视为归属不明，
       整组按未决处理并停止后续组；
    3. 没有拿到明确结果（缺项/异常）的岗位保留未决占用，不释放。
    """
    account = str(items[0].get('_sender_account_id') or '') if items else ''
    version = str(items[0].get('_sender_session_version') or '') if items else ''
    identity = api('GET', '/api/auth/account')
    current = identity.get('sender_account_id') or ''
    if not current or account and account != current or version and version != identity.get('session_version'):
        return 0, 0, 0, '账号已变化或无法确认，请重新确认后启动新批次', []
    account = current
    import uuid
    applications = {j['job_id']: 'app_' + uuid.uuid4().hex for j in items}
    def emit(event, job_id, **fields):
        append_event(event, job_id, sender_account_id=account,
                     application_id=applications[job_id], **fields)
    ok = failed = skipped = 0
    batch_ids = [j["job_id"] for j in items]
    # 发送意图与额度占用先落盘，再发起请求（B2/A3）
    prior_pending = max(0, int(usage.get("reserved") or 0) - int(usage["confirmed"]))
    usage["reserved"] = usage["confirmed"] + prior_pending + len(items)
    save_usage(usage)
    for jid in batch_ids:
        item = next(j for j in items if j['job_id'] == jid)
        emit("sending", jid, batch=aid, analysis_snapshot=analysis_snapshot(item),
                     job_snapshot={k: item.get(k) for k in ('job_id', 'company', 'title', 'salary', 'city', 'job_type', 'jd_text', 'jd_sha')},
                     **{k: item.get(k) for k in
                     ('text', 'company', 'title', 'analysis_id', 'writer', 'model', 'profile_version')})

    try:
        task = api("POST", "/api/deliver/start", {
            "analysis_id": aid, "mode": "selected", "confirmed": False,
            "encrypt_job_ids": batch_ids, "sender_account_id": account,
            "sender_session_version": identity.get('session_version') or '',
            "application_ids": applications,
        }, timeout=30)
    except Exception as exc:
        for jid in batch_ids:
            emit("unknown", jid, reason="启动响应丢失", error=type(exc).__name__)
        return 0, 0, 0, "发送请求结果未知，保留占用并停止本轮", []
    task_id = task.get("task_id")
    if not task_id:
        for jid in batch_ids:
            emit("unknown", jid, reason=f"启动失败：{str(task)[:120]}")
        return 0, 0, 0, f"启动失败：{str(task)[:150]}", []
    try:
        final = wait_task(task_id)
    except Exception as exc:
        for jid in batch_ids:
            emit("unknown", jid, task_id=task_id, error=type(exc).__name__)
        return 0, 0, 0, "发送任务查询失败，保留占用并停止本轮", []
    interrupted = final.get('status') != 'done'

    raw_ids = [str(it.get("encrypt_job_id")) for it in (final.get("items") or [])]
    returned = set(raw_ids)
    duplicated = len(raw_ids) != len(returned)
    unexpected = returned - set(batch_ids)
    if duplicated or unexpected:
        reason = "响应归属不明：" + ("重复 id " if duplicated else "") + \
                 (f"额外 id {sorted(unexpected)[:3]}" if unexpected else "")
        for jid in batch_ids:
            emit("unknown", jid, task_id=task_id, reason=reason)
        # 整组按未决保留占用，不猜测哪些成功
        usage["reserved"] = max(int(usage.get("reserved") or 0), int(usage.get("confirmed") or 0) + len(items))
        save_usage(usage)
        return 0, 0, 0, reason + "；本组按未决处理，后续组不再发送", []

    missing = [j for j in batch_ids if j not in returned]
    for jid in missing:
        emit("unknown", jid, task_id=task_id, reason="任务 done 但缺逐项结果")
    retryable: list[tuple[dict, str]] = []
    for it in final.get("items") or []:
        jid = str(it.get("encrypt_job_id"))
        st = str(it.get("status"))
        if st == "ok":
            ok += 1
            src = next((x for x in items if x["job_id"] == jid), {})
            emit("sent", jid, task_id=task_id, at_sent=now(),
                         sha=src.get("sha"), writer=src.get("writer"),
                         model=src.get("model"),
                         **({"retried": True} if retried else {}))
        elif st == "skipped":
            skipped += 1
            emit("skipped", jid, task_id=task_id, reason=it.get("error"))
        elif st not in {'failed'}:
            emit('unknown', jid, task_id=task_id, reason=it.get('error') or '没有确定的逐项结果')
            missing.append(jid)
        else:
            err = str(it.get("error") or "")
            if not PRESEND_ERROR.search(err) and re.search(r"timeout|timed\s*out|超时", err, re.I):
                emit("unknown", jid, task_id=task_id, reason="发送中途超时，不能确认是否送达")
                missing.append(jid)
                continue
            failed += 1
            emit("failed", jid, task_id=task_id, error=err,
                         **({"retried": True} if retried else {}))
            if not retried and PRESEND_ERROR.search(err):
                match = next((j for j in items if j["job_id"] == jid), None)
                if match:
                    retryable.append((match, err))
    ok_in_group = sum(1 for it in (final.get("items") or []) if str(it.get("status")) == "ok")
    usage["confirmed"] = usage["confirmed"] + ok_in_group
    # 缺项（真相不明）继续占额度；明确失败/跳过的可以释放
    usage["reserved"] = usage["confirmed"] + prior_pending + len(missing)
    save_usage(usage)
    if interrupted:
        return ok, failed, skipped, f"任务 {task_id} 状态 {final.get('status')}，已保留逐项结果，后续不再发送", []
    if missing:
        return ok, failed, skipped, "任务 done 但缺逐项结果；保留未决占用并停止本轮", []
    return ok, failed, skipped, "", retryable


def send(ready: list[dict], usage: dict, cap: int, daily_cap: int, chunk: int,
         gap: float, approved: dict | None = None) -> tuple[int, int, int, str]:
    ok = failed = skipped = 0
    aborted = ""
    # 预算按「已确认 + 未决占用」算：unknown/reserved 没对账前不许再占额度（B2）
    occupied = max(int(usage.get("confirmed") or 0), int(usage.get("reserved") or 0))
    room = min(cap, daily_cap - occupied, len(ready))
    picked = ready[:max(0, room)]
    if stop_requested():
        return 0, 0, 0, '用户请求停止'
    if not picked:
        return 0, 0, 0, ''
    from boss_login.account import assert_task_environment
    try:
        assert_task_environment()
    except Exception as exc:
        return 0, 0, 0, str(exc)
    identity = api('GET', '/api/auth/account')
    account = identity.get('sender_account_id') or ''
    if not account:
        return 0, 0, 0, '无法确认发送账号，请先正常登录'
    picked = [{**item, '_sender_account_id': account, '_sender_session_version': identity.get('session_version') or ''} for item in picked]
    blocked = load_blocklist()
    for idx in range(0, len(picked), chunk):
        if stop_requested():
            return ok, failed, skipped, "用户请求停止"
        part = picked[idx:idx + chunk]
        part, notes = recheck_group(part, blocked)
        for n in notes:
            print(f"  [复核] {n}")
        groups: dict[str, list[dict]] = {}
        for j in part:
            groups.setdefault(j["analysis_id"], []).append(j)
        retryable: list[tuple[dict, str]] = []
        for aid, items in groups.items():
            if stop_requested():
                return ok, failed, skipped, "用户请求停止"
            if approved is not None:
                _cache.clear()
                if any(review_fingerprint(j) != approved.get(j["job_id"]) for j in items):
                    return ok, failed, skipped, "资料或设置已变化，请重新审核"
            a_ok, a_failed, a_skipped, a_aborted, a_retry = _deliver_once(items, usage, aid)
            ok += a_ok
            failed += a_failed
            skipped += a_skipped
            retryable += a_retry
            if a_aborted:
                aborted = a_aborted
                break
        if aborted:
            break
        # 连不上聊天通道的，同一轮隔 10 秒补一次；仍失败就等下一轮（attempts 上限 2）
        if retryable:
            print(f"  [重试] {len(retryable)} 条在建会话阶段失败，10 秒后补一次")
            if wait_or_stop(10):
                return ok, failed, skipped, "用户请求停止"
            again, _notes = recheck_group([j for j, _ in retryable], load_blocklist())
            by_aid: dict[str, list[dict]] = {}
            for j in again:
                by_aid.setdefault(j["analysis_id"], []).append(j)
            for aid, items in by_aid.items():
                if stop_requested():
                    return ok, failed, skipped, "用户请求停止"
                if approved is not None:
                    _cache.clear()
                    if any(review_fingerprint(j) != approved.get(j["job_id"]) for j in items):
                        return ok, failed, skipped, "资料或设置已变化，请重新审核"
                a_ok, a_failed, a_skipped, a_aborted, _ = _deliver_once(items, usage, aid, retried=True)
                ok += a_ok
                failed += a_failed
                skipped += a_skipped
                if a_aborted:
                    aborted = a_aborted
                    break
        if aborted:
            break
        if idx + chunk < len(picked) and gap > 0:
            if wait_or_stop(gap):
                return ok, failed, skipped, "用户请求停止"
    return ok, failed, skipped, aborted


def wait_task(task_id: str, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            last = api("GET", "/api/deliver/status", timeout=20)
        except Exception:
            time.sleep(3)
            continue
        if last.get("task_id") != task_id:
            return {"status": "changed", "task_id": last.get("task_id")}
        if last.get("status") in {"done", "error", "cancelled"}:
            return last
        time.sleep(2)
    return {"status": "timeout", "task_id": task_id}


def write_batch_file(eligible: list[dict], limit: int = 40) -> Path:
    """当日批次请求：给写稿方看的唯一输入。

    JD 不再整份转发：DeepSeek 已经读过全文出摘要/硬条件，这里再给一份
    **程序从全文抽的要求类原文句子**做证据。同一份 JD 只让一个模型读全文。
    """
    (HERE / "greeting").mkdir(parents=True, exist_ok=True)
    policy, profile = load_policy(), load_profile()
    cfg = load_config()
    path = HERE / "greeting" / f"批次-{today()}.md"
    lines = [
        f"# 批次 {today()}（候选 {min(limit, len(eligible))} 个）",
        "",
        f"- 写作规则版本：{policy['version']}",
        f"- 候选人事实版本：{profile['version']}",
        f"- 工作流版本：{cfg['version']}",
        f"- 要求：{policy['requirements']}{availability_clause()}",
        "- 关于 JD：你看到的是「DeepSeek 读全文后的摘要」+「程序从全文抽的要求原句」，"
        "不是全文。不要再要求全文；摘要与原句冲突时以原句为准。",
        "",
        "## 事实清单（只能引用这些，引用时写上 ID）",
        "",
    ]
    lines += [f"- {fid}: {text}" for fid, text in facts()]
    lines += [
        "",
        "## 回复格式（每个岗位一段，缺正文就不要写这一段）",
        "",
        "```",
        "## <job_id>",
        "事实：F1 F4",
        "正文：<不超过 200 字>",
        "```",
        "",
        "## 岗位",
        "",
    ]
    for j in eligible[:limit]:
        brief, summary_label, evidence = jd_payload(j)
        points = match_points(j)
        short_title = title_for_message(j["title"])
        total = len(j.get("jd_text") or "")
        given = len(brief) + len(evidence)
        lines += [
            f"### job_id: {j['job_id']}",
            f"公司：{j['company']} · 岗位：{j['title']} · 分数：{j['score']}",
            f"正文里称呼这个岗位用：{short_title}" + ("" if short_title == j["title"] else "（原标题太长，只保留核心职能与招聘身份）"),
            f"JD 哈希：{j['jd_sha']} · 原文字数：{total} · 本次提供约 {given} 字",
            "硬性条件（DeepSeek 摘录）：" + ("；".join(j.get("hard_constraints") or []) or "（未提取到）"),
            "匹配点（DeepSeek 技能匹配）：" + ("；".join(points) or "（未提取到）"),
            "",
            f"{summary_label}：{brief or '（没有，只能依据下面的原句）'}",
            "",
            "JD 要求类原句（从全文抽取，不是全文）：",
            evidence or "（没抽到要求类句子）",
            "",
            "（摘要或原句里看不出的条件，写成「待核实」，不要猜。）",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def read_replies() -> dict[str, dict]:
    """读当天回复文件：`## <job_id>`，可选 `事实:` 行与 `正文:` 行。"""
    path = HERE / "greeting" / f"回复-{today()}.md"
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].strip()
        elif current and line.startswith("正文"):
            body = line.split(":", 1)[-1].split("：", 1)[-1].strip()
            if body:
                out.setdefault(current, {})["text"] = body
        elif current and line.startswith("事实"):
            ids = line.split(":", 1)[-1].split("：", 1)[-1]
            out.setdefault(current, {})["facts"] = [x for x in re.split(r"[\s,，、]+", ids) if x]
    return out


def prepare_from_file(eligible: list[dict], state: dict, heads: set[str],
                      replies: dict[str, dict], allow_model_fallback: bool,
                      budget: int = 30) -> list[dict]:
    """按 job_id 取回复正文；冻结态跳过；先筛再截预算（B3/B8）。"""
    cfg = load_config()
    max_attempts = int(cfg.get("max_delivery_attempts") or 2)
    prepared: list[dict] = []
    todo = [j for j in eligible if not frozen_for_prepare(state.get(j["job_id"]), max_attempts)]
    todo = todo[:max(0, budget)]
    for j in todo:
        jid = j["job_id"]
        reply = replies.get(jid) or {}
        text = str(reply.get("text") or "").strip()
        writer, model = "codex", "codex-file"
        if not text and allow_model_fallback:
            text = j.get("current_greeting") or ""
            writer, model = "model", "deepseek-flash"
        if not text:
            continue
        used = reply.get("facts") or []
        issues = validate(text, used, heads)
        if issues:
            append_event("rejected", jid, text=text, issues=issues, writer=writer)
            continue
        if writer == "codex" and used:
            # 回复文件里写了「事实:」就走和桥接稿同一套复核（同标准，A4）
            ok, f_issues, checks = review_facts(text, used, j.get("hard_constraints") or [])
            append_event("fact_reviewed", jid, text=text, rule=FACT_RULE,
                         checks=checks, issues=f_issues)
            if not ok:
                append_event("needs_review", jid, text=text, reason="事实层未通过",
                             issues=f_issues)
                continue
        elif writer == "codex":
            f_issues = fact_issues(text, used, j.get("hard_constraints") or [], require_facts=False)
            if f_issues:
                append_event("needs_review", jid, text=text, reason="事实层未通过", issues=f_issues)
                continue
        try:
            patch_greeting(j["analysis_id"], jid, text)
        except Exception as exc:
            append_event("patch_failed", jid, error=str(exc)[:200])
            continue
        if program_greeting(j["analysis_id"], jid) != text:
            append_event("verify_failed", jid)
            continue
        meta = _greeting_meta(j, text)
        append_event("validated", jid, text=text, writer=writer, model=model,
                     checks={"format": "ok",
                             "facts": f"ok:{FACT_RULE}" if used else "not_provided",
                             "business": "file_channel"},
                     **meta)
        heads.add(normalize_head(text))
        prepared.append({**j, "text": text, "sha": meta["sha"]})
    return prepared


def record_run(mode: str, args, payload: dict) -> dict:
    """每次运行单独落一条；备稿不再覆盖发送记录（R6）。"""
    RUNS.mkdir(parents=True, exist_ok=True)
    run_id = f"{today().replace('-', '')}-{args.round}-{mode}-{stamp()}"
    record = {"run_id": run_id, "date": today(), "time": now(), "round": args.round,
              "mode": mode, "config_version": load_config()["version"], **payload}
    path = RUNS / f"{run_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    with (RUNS / "index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: record[k] for k in
                             ("run_id", "date", "time", "round", "mode", "config_version")},
                            ensure_ascii=False) + "\n")
    (RUNS / f"last-{mode}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    if mode == "send":
        SUMMARY.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        with ROUNDS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", default="manual", help="09/14/manual；决定用哪一组默认参数")
    ap.add_argument("--resume-id", default=None)
    ap.add_argument("--min-score", type=int, default=None)
    ap.add_argument("--cap", type=int, default=None)
    ap.add_argument("--daily-cap", type=int, default=None)
    ap.add_argument("--chunk", type=int, default=None)
    ap.add_argument("--chunk-gap", type=float, default=None)
    ap.add_argument("--max-prepare", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=None)
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--send-ready-only", action="store_true", help="仅发送指定且已审核的稿件，不生成新稿")
    ap.add_argument("--approved-file", default="", help="面板生成的稿件审核指纹文件")
    ap.add_argument("--expected-profile", default="", help="任务创建时的资料版本；不一致则拒绝")
    ap.add_argument("--stop-file", default="", help="面板停止后续批次的标记")
    ap.add_argument("--grant-deadline", type=float, default=0.0,
                    help="授权窗口结束的 epoch 秒；到点停在下一批之前（0 = 不限）")
    ap.add_argument("--writer", choices=["file", "bridge"], default=None,
                    help="file=读 Codex 当天回复文件；bridge=程序直接调 GPT 写稿；默认按轮次配置")
    ap.add_argument("--no-fallback", action="store_true", help="回复缺失时不退回程序现生成的正文")
    ap.add_argument("--no-batch", action="store_true", help="不写批次请求文件")
    ap.add_argument("--no-bridge", action="store_true", help="等同 --writer file（旧参数兼容）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refill", action="store_true", help="先抓新岗位、补 JD 并匹配（A6）；默认按配置")
    ap.add_argument("--no-refill", action="store_true")
    ap.add_argument("--refill-pages", type=int, default=None)
    ap.add_argument("--max-new", type=int, default=None)
    ap.add_argument("--repair-queue", action="store_true", help="隔离队列坏行后再跑")
    ap.add_argument("--only", default="", help="只处理这些 job_id（逗号分隔），用于定点验证/重试")
    ap.add_argument("--regen", action="store_true",
                    help="配合 --only 重写这些岗位的话术：先作废旧稿，再重新写；已发送/结果未知的绝不重写")
    ap.add_argument("--print-config", action="store_true")
    ap.add_argument("--report-json", action="store_true",
                    help="只算「现在能发多少、能发哪些」，打一行 JSON 给网页面板；不写稿不发送不改台账")
    ap.add_argument("--no-activity", action="store_true",
                    help="关掉活跃筛选（默认要求 recruitment 最近活跃在 activity.within_days 天内）")
    args = ap.parse_args()

    global STOP_FILE, GRANT_DEADLINE
    if args.stop_file:
        candidate = Path(args.stop_file).resolve()
        if not candidate.is_relative_to((RUNS / "panel-jobs").resolve()):
            print("[7] 停止标记必须位于 runs/panel-jobs")
            return 7
        STOP_FILE = candidate
    if args.grant_deadline:
        GRANT_DEADLINE = float(args.grant_deadline)
        if deadline_passed():
            print(f"[{now()}] 启动时授权窗口已经到期，本轮不发送", flush=True)
            return 8

    global ACTIVITY_GATE
    if args.no_activity:
        ACTIVITY_GATE = False

    rnd = (cfg.get("rounds") or {}).get(args.round) or (cfg.get("rounds") or {}).get("manual") or {}
    args.resume_id = args.resume_id or cfg.get("resume_id") or load_profile()["resume_id"]
    args.min_score = cfg["min_score"] if args.min_score is None else args.min_score
    args.cap = int(rnd.get("cap", cfg["daily_cap"])) if args.cap is None else args.cap
    args.daily_cap = int(cfg["daily_cap"]) if args.daily_cap is None else args.daily_cap
    args.chunk = int(cfg.get("chunk") or 25) if args.chunk is None else args.chunk
    args.chunk_gap = float(cfg.get("chunk_gap") or 0) if args.chunk_gap is None else args.chunk_gap
    args.max_prepare = int(rnd.get("prepare", 30)) if args.max_prepare is None else args.max_prepare
    args.batch_size = int(cfg.get("batch_size") or 6) if args.batch_size is None else args.batch_size
    if args.writer is None:
        args.writer = str(rnd.get("writer") or "bridge")
    args.refill_pages = int(rnd.get("refill_pages", 8)) if args.refill_pages is None else args.refill_pages
    args.max_new = int(rnd.get("max_new", 120)) if args.max_new is None else args.max_new
    if args.no_bridge:
        args.writer = "file"
    if args.refill:
        do_refill = True
    elif args.no_refill:
        do_refill = False
    else:
        do_refill = bool(rnd.get("refill"))

    if args.print_config:
        print(json.dumps({"config": cfg, "round": args.round, "resolved": {
            "cap": args.cap, "daily_cap": args.daily_cap, "prepare": args.max_prepare,
            "chunk": args.chunk, "refill": do_refill}}, ensure_ascii=False, indent=2))
        return 0
    if args.report_json:
        return report(args)
    if args.chunk <= 0 or args.cap <= 0 or args.daily_cap <= 0 or args.max_prepare < 0:
        print("参数非法：chunk/cap/daily-cap 必须为正数，max-prepare 不能为负")
        return 7
    if not 1 <= args.batch_size <= 15:
        # 桥接器一次最多收 15 个岗位；超了整批会被拒，不如在这里直接停
        print(f"参数非法：batch_size 必须在 1–15（当前 {args.batch_size}），桥接器上限 15")
        return 7

    try:
        with Lock():
            return run(args, do_refill)
    except Busy as exc:
        print(f"[5] {exc}")
        return 5
    except RuntimeError as exc:
        print(f"[7] {exc}")
        return 7


def review_fingerprint(job: dict) -> str:
    """Bind human approval to the exact text, job evidence and active settings."""
    value = {k: job.get(k) for k in ("job_id", "analysis_id", "text", "jd_sha", "score")}
    value.update(profile=load_profile().get("version"), policy=load_policy().get("version"),
                 selection=load_policy().get("selection_version"), workflow=load_config().get("version"),
                 preferences=load_preferences().get("version") or "")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approved_ready(args, eligible: list, state: dict) -> list:
    path = Path(args.approved_file).resolve()
    if not path.is_relative_to((RUNS / "panel-jobs").resolve()):
        raise RuntimeError("审核文件必须位于 runs/panel-jobs")
    payload = _load_json(path, "稿件审核")
    expected = payload.get("approved")
    if not isinstance(expected, dict) or not expected or len(expected) > 40:
        raise RuntimeError("缺少有效的已审核稿件")
    if payload.get("profile_version") != load_profile().get("version"):
        raise RuntimeError("资料已变化，请重新审核稿件")
    ready = {j["job_id"]: j for j in ready_jobs(eligible, state, mutate=False)}
    for jid, fingerprint in expected.items():
        if jid not in ready or review_fingerprint(ready[jid]) != fingerprint:
            raise RuntimeError("稿件或岗位条件已变化，请刷新并重新审核：" + str(jid))
    return [ready[jid] for jid in expected]


def not_ready_reason(j: dict, st: dict) -> dict:
    """只读判断：这个够分岗位为什么还不能发，以及**具体差在哪**。

    返回 {kind, label, detail, since}：label 是一眼能看懂的一句，detail 是拿得出的证据
    （旧值 → 新值、写稿时间、失败原因），面板两行都显示。
    绝不写台账（报告要能安全地反复跑）。
    """
    policy, profile = load_policy(), load_profile()
    status = str(st.get("greeting_status") or "")
    g = st.get("greeting") or {}
    write_at = str(g.get("at") or g.get("error_at") or "")

    def out(kind: str, label: str, detail: str = "", since: str = "") -> dict:
        return {"kind": kind, "label": label, "detail": detail, "since": since or write_at}

    if not status:
        return out("new", "还没写招呼语", "岗位够分、筛过、没发过；写稿会生成第一版")
    if status in {"write_failed", "write_missing", "patch_failed", "verify_failed"}:
        err = str(g.get("error") or "").strip()
        return out("write_failed", "上次写稿失败", err or "写稿进程没有返回可用正文")
    if status == "rejected":
        issues = g.get("issues") or []
        if isinstance(issues, str):
            issues = [issues]
        return out("rejected", "上次写稿没通过校验", "；".join(str(x) for x in issues)[:200])
    if status == 'needs_review':
        issues = g.get('issues') or []
        return out('needs_review', '事实待核对，暂不进入待发送', '；'.join(str(x) for x in issues)[:200])
    if status in {"stale_profile"}:
        return out("stale_profile", "旧稿是按换简历前的资料写的",
                   f"旧稿资料版本 {g.get('stale_queued') or g.get('profile_version') or '未记录'} → "
                   f"当前 {profile.get('version')}")
    if status == "stale_score":
        return out("stale_score", "分数重新打过，旧稿按旧分写的",
                   f"旧稿按 {g.get('stale_queued') or g.get('score')} 分 → 现在 {j.get('score')} 分")
    if status == "stale_jd":
        return out("stale_jd", "JD 更新过，旧稿是按旧 JD 写的",
                   f"写稿时 JD {str(g.get('stale_queued') or g.get('jd_sha') or '未记录')[:12]} → "
                   f"现在 {str(j.get('jd_sha') or '')[:12]}")
    if status == "stale_prefs":
        return out("stale_prefs", "到岗口径改过，旧稿还是旧说法",
                   f"口径版本 {g.get('stale_queued') or '未记录'} → {load_preferences().get('version') or '未配置'}")
    if status == "stale_selection":
        return out("stale_selection", "选材规则改过，旧稿的写法不再符合",
                   f"{g.get('stale_queued')} → {policy.get('selection_version')}")
    if status == "stale_policy":
        return out("stale_policy", "写作规则改过，需要复验",
                   f"{g.get('stale_queued') or g.get('policy_version')} → {policy.get('version')}")
    if status.startswith("stale_"):
        return out("stale", "稿子已过期，需重写", str(g.get("stale") or status))
    if status not in {"validated", "revalidated"}:
        return out("other", "需重写", f"台账状态：{status}")

    text = str(g.get("text") or "")
    if not text:
        return out("no_text", "台账里没有正文", "写稿记录在，但正文丢了")
    if g.get("jd_sha") and g["jd_sha"] != j.get("jd_sha"):
        return out("stale_jd", "JD 更新过，旧稿是按旧 JD 写的",
                   f"写稿时 JD {str(g['jd_sha'])[:12]} → 现在 {str(j.get('jd_sha') or '')[:12]}")
    if g.get("score") is not None and int(g["score"]) != int(j["score"]):
        return out("stale_score", "分数重新打过，旧稿按旧分写的",
                   f"旧稿按 {g['score']} 分 → 现在 {j['score']} 分")
    if not g.get("profile_version") or g["profile_version"] != profile["version"]:
        return out("stale_profile", "旧稿是按换简历前的资料写的",
                   f"旧稿资料版本 {g.get('profile_version') or '未记录'} → 当前 {profile.get('version')}")
    if str(g.get("prefs_version") or "") != str(load_preferences().get("version") or ""):
        return out("stale_prefs", "到岗口径改过，旧稿还是旧说法",
                   f"口径版本 {g.get('prefs_version') or '未记录'} → {load_preferences().get('version') or '未配置'}")
    if (g.get("selection_version") or "") != (policy.get("selection_version") or ""):
        return out("stale_selection", "选材规则改过，旧稿的写法不再符合",
                   f"{g.get('selection_version') or '未记录'} → {policy.get('selection_version')}")
    if g.get("policy_version") != policy["version"]:
        return out("stale_policy", "写作规则改过，需要复验",
                   f"{g.get('policy_version') or '未记录'} → {policy['version']}")
    return out("unknown", "待复核（发送前会再核一次）", "")


def pending_row(j: dict, st: dict) -> dict:
    """一条「够分但还没发」的记录：分数来源 + 为什么卡住 + 具体差在哪（给面板直接用）。"""
    why = not_ready_reason(j, st)
    return {
        "job_id": j["job_id"], "score": j["score"],
        "company": j["company"], "title": j["title"],
        "analysis_id": j.get("analysis_id"),
        "scored_by": j.get("scored_resume_title") or "",
        "score_legacy": bool(j.get("score_legacy")),
        "reason": why["label"], "reason_kind": why["kind"],
        "reason_detail": why["detail"], "reason_since": why["since"],
        "review_draft": str((st.get('greeting') or {}).get('text') or '') if why['kind'] == 'needs_review' else '',
    }


def report(args) -> int:
    """算「现在能发多少、能发哪些」，打一行 JSON 给网页面板看。

    只读：不写稿、不发送、不改台账；准入与就绪的判断走的是发送时同一套函数，
    所以面板上的数字就是这一轮真会发出去的数字，不是另算一份。
    """
    out: dict = {"ok": False, "error": "", "at": stamp(), "min_score": args.min_score,
                 "daily_cap": args.daily_cap}
    try:
        health = api("GET", "/api/system/health", timeout=10)
        auth = api("GET", "/api/auth/status", timeout=15)
    except Exception as exc:  # noqa: BLE001 - 面板要能显示"接口挂了"
        out["error"] = f"接口不可用：{type(exc).__name__}: {exc}"
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not health.get("db"):
        out["error"] = "控制台健康检查异常"
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not auth.get("logged_in"):
        out["error"] = "BOSS 登录失效"
        print(json.dumps(out, ensure_ascii=False))
        return 0
    buffer = io.StringIO()
    try:
        usage = load_usage()
        with contextlib.redirect_stdout(buffer):  # collect_eligible 会打剔除明细，别污染 JSON
            events = load_queue()
            state = fold(events)
            eligible = collect_eligible(args.min_score, args.resume_id)
            ready = ready_jobs(eligible, state, mutate=False)
        stats = api("GET", "/api/jobs/stats", timeout=20) or {}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"计算失败：{type(exc).__name__}: {exc}"
        out["detail"] = buffer.getvalue()[-300:]
        print(json.dumps(out, ensure_ascii=False))
        return 0
    ready_ids = {j["job_id"] for j in ready}
    pending_rows = [pending_row(j, state.get(j['job_id']) or {})
                    for j in eligible if j['job_id'] not in ready_ids]
    pending_reason_counts: dict[str, int] = {}
    for row in pending_rows:
        kind = row['reason_kind']
        pending_reason_counts[kind] = pending_reason_counts.get(kind, 0) + 1
    idx = analysis_index(args.resume_id)
    analyzed = len(idx)
    # 「有 JD 但没打过分」要分开报：被日薪/学历/经验挡掉的，采集器永远不会去打，
    # 跟真待办混在一起，面板上就成了"补不平的数"。
    unscored_todo, unscored_blocked = unscored_split(set(idx))
    # 「有 JD」按真正拿到正文算。接口的 missing_desc 只统计"一次都没试过"的，
    # 试过但回空 JD 的那几条会被算成"有 JD"，漏斗就少几格对不平（11-01 差 7 个）。
    db_total = int(stats.get("jobs") or 0)
    with_jd = len([d for d in job_desc_map().values() if d])
    if not with_jd:  # 分页拉不到时退回旧口径，别把整条漏斗显示成 0
        with_jd = max(0, db_total - int(stats.get("missing_desc") or 0))
    out.update({
        "ok": True,
        "resume_id": args.resume_id,
        "db_total": db_total,
        "missing_desc": max(0, db_total - with_jd),
        "never_tried_desc": int(stats.get("missing_desc") or 0),
        "with_jd": with_jd,
        "analyzed": analyzed,
        "unmatched": max(0, with_jd - analyzed),
        "unscored_with_jd": len(unscored_todo) + len(unscored_blocked),
        "unscored_todo": len(unscored_todo),
        "unscored_blocked": len(unscored_blocked),
        "unscored_todo_ids": unscored_todo[:80],
        "salary_gate": (load_config().get("salary_gate") or {}),
        "eligible": len(eligible),
        "ready": len(ready),
        "eligible_not_ready": len([j for j in eligible if j["job_id"] not in ready_ids]),
        "sent_today": int(usage.get("confirmed") or 0),
        "reserved": int(usage.get("reserved") or 0),
        "round_prepare": args.max_prepare,
        "rows": [{"job_id": j["job_id"], "score": j["score"],
                  "company": j["company"], "title": j["title"],
                  "analysis_id": j.get("analysis_id"),
                  "scored_by": j.get("scored_resume_title") or "",
                  "score_legacy": bool(j.get("score_legacy")),
                  "chars": len(j.get("text") or ""), "text": j.get("text") or "",
                  "review_hash": review_fingerprint(j)} for j in ready[:40]],
        "pending_total": len([j for j in eligible if j["job_id"] not in ready_ids]),
        "pending_reason_counts": pending_reason_counts,
        "pending": pending_rows[:40],
    })
    print(json.dumps(out, ensure_ascii=False))
    return 0


def run(args, do_refill: bool) -> int:
    if args.dry_run and (getattr(args, "regen", False) or args.repair_queue):
        print("[7] --dry-run 不能与 --regen 或 --repair-queue 同用；预览不会修改稿件或台账")
        return 7
    if getattr(args, "expected_profile", "") and args.expected_profile != load_profile().get("version"):
        print("[7] 求职资料已更新，请重新创建任务")
        return 7
    if stop_requested():
        return 8
    try:
        health = api("GET", "/api/system/health", timeout=10)
    except Exception as exc:
        print(f"[3] 控制台不可用：{exc}")
        return 3
    if not health.get("db"):
        print("[3] 控制台健康检查异常")
        return 3
    try:
        auth = api("GET", "/api/auth/status", timeout=15)
    except Exception as exc:
        print(f"[7] 登录状态接口异常：{exc}")
        return 7
    if not auth.get("logged_in"):
        print("[4] BOSS 登录失效")
        return 4

    usage = load_usage()
    events = load_queue(repair=args.repair_queue)
    state = fold(events)
    reconciled = 0 if args.dry_run else reconcile_intents(state)
    if reconciled:
        print(f"崩溃对账：{reconciled} 条只有发送意图、没有结果，已记 unknown 并保留未决占用")
        events = load_queue(repair=False)
        state = fold(events)
    refill_info: dict = {}

    # 备稿模式显式要求补货时，先补货再选候选；普通发送轮补货仍放在最后。
    if args.prepare_only and do_refill and not args.dry_run:
        refill_info = refill(args.resume_id, args.refill_pages, args.max_new)

    eligible = collect_eligible(args.min_score, args.resume_id)
    only = [x.strip() for x in str(getattr(args, "only", "") or "").split(",") if x.strip()]
    if only:
        wanted = set(only)
        have = {j["job_id"] for j in eligible}
        missing = [x for x in only if x not in have]
        eligible = [j for j in eligible if j["job_id"] in wanted]
        print(f"--only 生效：请求 {len(only)} 个，命中 {len(eligible)} 个"
              + (f"，不在准入池：{','.join(missing[:5])}" if missing else ""))

    if getattr(args, "regen", False):
        if not args.only:
            print("[7] --regen 必须配合 --only 使用（不给范围就不许批量作废旧稿）")
            return 7
        for j in eligible:
            st = state.get(j["job_id"], {})
            if st.get("delivery_status") in FROZEN_DELIVERY:
                print(f"  [跳过] {j['job_id']} 已发送/结果未知，不重写")
                continue
            if st.get("greeting_status") in {"validated", "revalidated"}:
                append_event("voided", j["job_id"], reason="按 --regen 重写")
                print(f"  [作废] {j['job_id']} 旧稿已标记 voided，准备重写")
        state = fold(load_queue())

    heads = {normalize_head(str(ev.get("text") or "")) for ev in events
             if ev.get("event") in {"validated", "revalidated"}}

    budget = max(0, args.max_prepare)
    if args.dry_run:
        ready = ready_jobs(eligible, state, mutate=False)
        print(f"准入 {len(eligible)}，已就绪 {len(ready)}，今日已发 {usage['confirmed']}"
              f"（dry-run 不写稿、不发送、不改台账）")
        for j in ready[:args.cap]:
            print(f"  READY {j['score']} {j['company']} / {j['title']} ({len(j['text'])}字)")
        return 0

    if getattr(args, "send_ready_only", False):
        if args.prepare_only or do_refill or getattr(args, "regen", False) or args.repair_queue:
            raise RuntimeError("审核批次只能发送，不能同时生成、补货或修复")
        ready = approved_ready(args, eligible, state)
        before = usage["confirmed"]
        approved = {j["job_id"]: review_fingerprint(j) for j in ready}
        ok, failed, skipped, aborted = send(ready, usage, args.cap, args.daily_cap, args.chunk, args.chunk_gap, approved=approved)
        record_run("send", args, {"eligible": len(eligible), "prepared": 0, "ready": len(ready),
            "ok": ok, "failed": failed, "skipped": skipped, "aborted": aborted,
            "used_before": before, "used_after": usage["confirmed"], "source": "panel-reviewed"})
        print(f"审核批次完成：成功 {ok} / 失败 {failed} / 跳过 {skipped}；{aborted}")
        if aborted == "用户请求停止":
            return 8
        return 7 if aborted or failed or ok < len(ready) else 0

    before = usage["confirmed"]
    ok = failed = skipped = 0
    aborted = ""
    initial_ids: set[str] = set()
    remaining_cap = args.cap
    if not args.prepare_only:
        initial = ready_jobs(eligible, state)
        occupied = max(int(usage["confirmed"]), int(usage.get("reserved") or 0))
        initial = initial[:max(0, min(args.cap, args.daily_cap - occupied))]
        if initial:
            initial_ids = {j["job_id"] for j in initial}
            print(f"优先发送已有就绪项：{len(initial)}")
            ok, failed, skipped, aborted = send(
                initial, usage, len(initial), args.daily_cap, args.chunk, args.chunk_gap)
            remaining_cap -= len(initial)
            state = fold(load_queue())

    if not aborted and not args.no_batch and eligible:
        path = write_batch_file(eligible)
        print(f"批次文件：{path.name}")
    if aborted:
        prepared = []
    elif args.writer == "bridge" and budget:
        prepared = prepare(eligible, state, budget, args.batch_size, heads)
    else:
        prepared = prepare_from_file(eligible, state, heads, read_replies(),
                                     not args.no_fallback, budget)
    state = fold(load_queue())

    ready = [j for j in ready_jobs(eligible, state) if j["job_id"] not in initial_ids]
    print(f"准入 {len(eligible)}，本轮写稿 {len(prepared)}，可发送 {len(ready)}，"
          f"今日已发 {usage['confirmed']}")

    if args.prepare_only:
        record_run("prepare", args, {
            "eligible": len(eligible), "prepared": len(prepared), "ready": len(ready),
            "ok": 0, "failed": 0, "skipped": 0, "aborted": "",
            "note": "备稿轮，没有发送；发送数字看 last-send.json",
        })
        return 8 if stop_requested() else 0

    if not aborted and ready and remaining_cap > 0:
        if initial_ids and args.chunk_gap > 0:
            time.sleep(args.chunk_gap)
        more_ok, more_failed, more_skipped, aborted = send(
            ready, usage, remaining_cap, args.daily_cap, args.chunk, args.chunk_gap)
        ok += more_ok
        failed += more_failed
        skipped += more_skipped

    # B4：补货放在发送之后——已有 ready 项不等待抓取/匹配
    if do_refill and not args.dry_run and not aborted:
        try:
            refill_info = refill(args.resume_id, args.refill_pages, args.max_new)
            print("补货（为下一轮准备）：" + json.dumps(refill_info, ensure_ascii=False))
        except Exception as exc:
            print(f"[7] 补货失败（不影响本轮已发结果）：{exc}")
            refill_info = {"error": str(exc)[:200]}

    record_run("send", args, {
        "eligible": len(eligible), "prepared": len(prepared), "ready": len(ready) + len(initial_ids),
        "ok": ok, "failed": failed, "skipped": skipped, "aborted": aborted,
        "used_before": before, "used_after": usage["confirmed"],
        "refill": refill_info,
    })
    print(f"本轮完成：成功 {ok} / 失败 {failed} / 跳过 {skipped}；今日累计 {usage['confirmed']}")
    if aborted:
        print(f"ABORTED: {aborted}")
        return 7
    if ok == 0 and usage["confirmed"] >= args.daily_cap:
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
