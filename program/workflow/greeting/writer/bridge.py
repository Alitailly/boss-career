"""Bounded, on-demand greeting writer. No BOSS access or sending capability."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from workflow_env import api_base, app_source, workspace_root

GREETING_ROOT = workspace_root() / "greeting"
VERSION = "11"
# 字数上限与禁用词：优先读有效方案里的写作策略；没有工作区才退回 greeting/policy.json
MAX_GREETING_LENGTH = 200
POLICY: dict = {}
# 写稿模型不再固定：读程序「系统配置 → 模型」（与评分同一份配置）
SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+\S+|(?:api[_-]?key|cookie|authorization)\s*[:=]", re.I)
BANNED = ("我们公司", "我们团队", "欢迎投递", "期待你的加入", "可立即全职")


def _effective_policy() -> dict | None:
    """有效方案里的写作策略（唯一真源）；拿不到返回 None。

    工作区还没迁移时返回 None，调用方退回 policy.json——旧布局仍然能跑。
    """
    try:
        from workflow_env import ensure_core_path

        ensure_core_path()
        from boss_core import compat

        snap = compat.load(workspace_root())
        if not snap:
            return None
        effective = compat.delivery_policy(snap)
        if not int(effective.get("max_len") or 0):
            return None
        return json.loads(json.dumps(effective, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 读不到就走旧文件，不拦住写稿
        return None


def apply_policy() -> dict:
    """把字数上限、禁用词、规则文本换成**当前生效**的版本。"""
    global POLICY, MAX_GREETING_LENGTH, BANNED
    effective = _effective_policy()
    if effective is not None:
        POLICY = effective
        POLICY.setdefault("count_rule", "按 Python len() 计算，标点计入")
        MAX_GREETING_LENGTH = int(POLICY["max_len"])
        BANNED = tuple(POLICY.get("forbidden") or BANNED)
        return POLICY

    path = GREETING_ROOT / "policy.json"
    try:
        POLICY = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError(10, f"greeting/policy.json 缺失或损坏：{exc}") from exc
    if not int(POLICY.get("max_len") or 0):
        raise BridgeError(10, "greeting/policy.json 缺 max_len")
    MAX_GREETING_LENGTH = int(POLICY["max_len"])
    BANNED = tuple(POLICY.get("forbidden") or BANNED)
    return POLICY


class BridgeError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_json(path: Path, value) -> None:
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def checked_text(value, name: str, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value.strip() and not allow_empty):
        raise BridgeError(10, f"输入字段不合法：{name}")
    if SECRET.search(value):
        raise BridgeError(10, f"输入疑似包含凭据，拒绝处理：{name}")
    return value.strip()


def input_limits() -> dict:
    """写作通道输入上限：只读 policy.json 的 `input` 段，和生成端共用一套数字。"""
    cfg = POLICY.get("input") or {}
    return {
        "brief": int(cfg.get("brief_chars") or 1200),
        "total": int(cfg.get("evidence_total_chars") or 500),
        "line": int(cfg.get("evidence_line_chars") or 280),
        "lines": int(cfg.get("evidence_max_lines") or 20),
    }


def load_request(path: Path) -> dict:
    path = path.resolve()
    if not path.is_relative_to(GREETING_ROOT) or "data" in (part.lower() for part in path.parts):
        raise BridgeError(10, "请求文件必须位于 boss-auto/greeting/ 内，且不能位于 data/。")
    if path.stat().st_size > 65536:
        raise BridgeError(10, "请求超过 64 KiB；请拆成小批次。")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) - {"batch_id", "request_id", "policy_version",
                                                  "profile_version", "facts", "jobs", "requirements"}:
        raise BridgeError(10, "请求只允许 batch_id/request_id/policy_version/profile_version/facts/jobs/requirements。")
    batch = data.get("batch_id") or data.get("request_id") or ""
    if not isinstance(batch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", batch):
        raise BridgeError(10, "batch_id 必须为 1–80 位英文字母、数字、下划线或横线。")
    # 请求可以用 request_id 或 batch_id 命名，但下游统一按 batch_id 取（否则 KeyError）
    data["batch_id"] = batch
    facts, jobs = data.get("facts"), data.get("jobs")
    if not isinstance(facts, list) or not 1 <= len(facts) <= 30:
        raise BridgeError(10, "facts 必须有 1–30 项。")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 15:
        raise BridgeError(10, "jobs 必须有 1–15 项。")
    fact_ids, job_ids = set(), set()
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"id", "text"}:
            raise BridgeError(10, "每条事实必须且只能有 id/text。")
        fid = checked_text(fact["id"], "fact.id", 80)
        if fid != fact["id"] or fid in fact_ids:
            raise BridgeError(10, "事实 ID 重复或包含首尾空白。")
        fact_ids.add(fid)
        fact["text"] = checked_text(fact["text"], "fact.text", 500)
    for job in jobs:
        if not isinstance(job, dict) or set(job) - {"job_id", "company", "title",
                                                   "title_for_message", "jd_brief",
                                                   "jd_evidence", "jd_meta", "hard_constraints",
                                                   "match_points", "draft"}:
            raise BridgeError(10, "岗位字段不合法。")
        jid = checked_text(job.get("job_id"), "job_id", 160)
        if jid != job["job_id"] or jid in job_ids:
            raise BridgeError(10, "岗位 ID 重复或包含首尾空白。")
        job_ids.add(jid)
        job["title_for_message"] = checked_text(job.get("title_for_message", ""),
                                                "title_for_message", 40, True)
        for key, limit in (("company", 120), ("title", 160)):
            job[key] = checked_text(job.get(key), key, limit)
        lim = input_limits()
        job["jd_brief"] = checked_text(job.get("jd_brief", ""), "jd_brief", lim["brief"], True)
        evidence = job.setdefault("jd_evidence", [])
        if not isinstance(evidence, list) or len(evidence) > lim["lines"]:
            raise BridgeError(10, f"jd_evidence 最多 {lim['lines']} 条。")
        job["jd_evidence"] = [checked_text(x, "jd_evidence", lim["line"]) for x in evidence]
        if sum(len(x) for x in job["jd_evidence"]) > lim["total"]:
            raise BridgeError(10, f"jd_evidence 合计超过 {lim['total']} 字。")
        # JD 已经不整份转发了：摘要和原句至少要有一样，否则等于让写稿方凭空编
        if not job["jd_brief"].strip() and not job["jd_evidence"]:
            raise BridgeError(10, "jd_brief 与 jd_evidence 不能同时为空。")
        meta = job.setdefault("jd_meta", {})
        if not isinstance(meta, dict) or set(meta) - {"jd_len", "jd_sha"}:
            raise BridgeError(10, "jd_meta 字段不合法。")
        hard = job.setdefault("hard_constraints", [])
        if not isinstance(hard, list) or len(hard) > 6:
            raise BridgeError(10, "hard_constraints 最多 6 条。")
        job["hard_constraints"] = [checked_text(h, "hard_constraint", 200) for h in hard]
        job["draft"] = checked_text(job.get("draft", ""), "draft", 1000, True)
        points = job.setdefault("match_points", [])
        if not isinstance(points, list) or len(points) > 5:
            raise BridgeError(10, "match_points 最多 5 条。")
        job["match_points"] = [checked_text(p, "match_point", 200) for p in points]
    data["requirements"] = checked_text(data.get("requirements", ""), "requirements", 2000, True)
    # 版本过期就不写稿：写出来的稿子和当前规则对不上（A5）
    policy_version = str(data.get("policy_version") or "")
    if policy_version and policy_version != str(POLICY.get("version") or ""):
        raise BridgeError(10, f"请求的写作规则版本 {policy_version} 不是当前生效版本 "
                              f"{POLICY.get('version')}；请用最新批次文件重试。")
    return data


def response_schema(request: dict) -> dict:
    fields = {
        "job_id": {"type": "string", "enum": [j["job_id"] for j in request["jobs"]]},
        "status": {"type": "string", "enum": ["draft", "blocked"]},
        "greeting": {"type": "string"},
        "reason": {"type": "string"},
        "ending_type": {"type": "string", "enum": ["question", "statement", "invite"]},
        "used_fact_ids": {"type": "array", "items": {"type": "string", "enum": [f["id"] for f in request["facts"]]}},
        "blocked_reason": {"type": "string"},
    }
    item = {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    return {"type": "object", "properties": {"items": {"type": "array", "items": item}},
            "required": ["items"], "additionalProperties": False}


def validate_response(request: dict, raw: dict) -> list[dict]:
    if not isinstance(raw, dict) or set(raw) != {"items"} or not isinstance(raw["items"], list):
        raise BridgeError(13, "返回结构不合法。")
    wanted = {j["job_id"] for j in request["jobs"]}
    items = raw["items"]
    if any(not isinstance(i, dict) or not isinstance(i.get("job_id"), str) for i in items):
        raise BridgeError(13, "返回条目缺少合法岗位 ID。")
    ids = [i["job_id"] for i in items]
    if len(ids) != len(wanted) or len(set(ids)) != len(ids) or set(ids) != wanted:
        raise BridgeError(13, "返回岗位 ID 有遗漏、重复或额外条目。")
    allowed_facts = {f["id"] for f in request["facts"]}
    expected = set(response_schema(request)["properties"]["items"]["items"]["required"])
    prefixes: dict[str, list[int]] = {}
    rows = []
    for i, item in enumerate(items):
        if set(item) != expected or any(not isinstance(item.get(k), str) for k in expected - {"used_fact_ids"}):
            raise BridgeError(13, "返回条目字段不合法。")
        text = item["greeting"].strip()
        errors = []
        if item["status"] != "draft":
            errors.append("writer_blocked:" + item["blocked_reason"][:200])
        else:
            if item["blocked_reason"]:
                errors.append("inconsistent_blocked_reason")
            if not text or len(text) > MAX_GREETING_LENGTH:
                errors.append("length_out_of_range")
            if any(word in text for word in BANNED):
                errors.append("banned_phrase")
            if re.search(r"\{\{|\}\}|<[^>]+>|待填|占位符|\b1[3-9]\d{9}\b|[\r\n]", text):
                errors.append("placeholder_or_contact_or_newline")
            if SECRET.search(text):
                errors.append("suspected_credential")
            prefix = re.sub(r"[\W_]", "", text)[:8]
            prefixes.setdefault(prefix, []).append(i)
        used = item["used_fact_ids"]
        if not isinstance(used, list) or any(not isinstance(f, str) or f not in allowed_facts for f in used):
            errors.append("unknown_fact_id")
        elif item["status"] == "draft" and not used:
            errors.append("missing_fact_references")
        if not item["reason"].strip() or item["ending_type"] not in {"question", "statement", "invite"}:
            errors.append("missing_reason_or_ending")
        rows.append({**item, "greeting": text, "length": len(text), "checks": errors})
    # 开头去重跟 policy.head_dedup 走。2026-10-28 用户复核后默认关闭：
    # 强制差异化会让模型为了过检查而刻意换句式，反而更不像人写的。
    if POLICY.get("head_dedup"):
        for positions in prefixes.values():
            if len(positions) > 1:
                for i in positions:
                    rows[i]["checks"].append("duplicate_opening")
    for row in rows:
        row["validation_status"] = "rejected" if row["checks"] else "needs_fact_review"
        row["approved_for_sending"] = False
    return rows


def _composed_instruction(req: str) -> str | None:
    """用核心库合成一份完整写作指令（系统约束 + 策略 + 面板规则 + 披露 + 长度）。

    以前这里是"隐藏模板 writer-prompt.txt + 面板规则"两段硬拼，用户看不到全貌；
    迁移后隐藏模板进了个人策略的 preamble，合成结果就是最终规则。
    """
    try:
        from workflow_env import ensure_core_path

        ensure_core_path()
        from boss_core import compat, greeting

        snap = compat.load(workspace_root())
        if not snap:
            return None
        rules = greeting.compose(snap, panel_rule=req)
        if not rules.get("text"):
            return None
        return rules["text"]
    except Exception:  # noqa: BLE001 - 合成失败退回旧拼法，不拦住写稿
        return None


def build_instruction(requirement: str | None = None) -> str:
    """组装发给模型的完整指令。

    规则优先级：程序系统约束（不可覆盖）→ 面板「系统配置 → 招呼语」→ 方案写作策略
    → 披露设置。整段由核心库合成，谁都能读到最后生效的全文（`GET /api/ai/greeting-rules`）。
    """
    req = requirement if requirement is not None else fetch_requirement()
    composed = _composed_instruction(req)
    if composed:
        # 合成规则已包含用户的篇幅要求与当前硬上限。
        # 不再额外发明 170–185 之类的范围，避免覆盖个人对齐结果。
        return composed

    text = (HERE / "writer-prompt.txt").read_text(encoding="utf-8")
    text += "\n\n# 本次生效的写作规则（来自「系统配置 → 招呼语」，可在网页上修改）\n"
    text += f"规则版本：{POLICY.get('version')}\n" + req
    # 未迁移的兼容通道只补硬上限，不追加另一套个人篇幅目标。
    text += (f"\n字数：硬校验线 {MAX_GREETING_LENGTH} 字（超过整条作废）；"
             f"计数方式：{POLICY.get('count_rule') or ''}")
    return text


#: 程序地址；桥接器向它读「系统配置 → 招呼语」里当前生效的写作规则
APP_API = api_base()


def fetch_requirement(timeout: float = 8.0) -> str:
    """从程序读当前生效的写作规则。

    唯一真源在程序的设置里：用户能在网页上看到并修改。取不到就报错，
    不能悄悄回落到某份写死的文本——否则用户改了规则却看不到效果。
    """
    url = APP_API.rstrip("/") + "/api/system/greeting-prompt"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 本机地址
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 一律转成带人话的桥接错误
        raise BridgeError(10, f"读不到「系统配置 → 招呼语」的设置：{exc}") from exc
    text = str((data or {}).get("requirement") or "").strip()
    if not text:
        raise BridgeError(10, "「系统配置 → 招呼语」的规则为空，拒绝在没有写作规则时写稿。")
    return text


def _llm_client():
    """用程序自己的模型配置（系统配置 → 模型）建一个 HTTP 客户端。

    这样写稿和评分共用同一处模型设置：换模型只改一处，不需要两个通道各配一份。
    """
    src = str(app_source())
    if src not in sys.path:
        sys.path.insert(0, src)
    from boss_web.services.llm_client import LLMClient, extract_json
    from boss_web.services.llm_config_store import load_config

    cfg = load_config()
    if not (str(cfg.api_key or "").strip() and str(cfg.base_url or "").strip()
            and str(cfg.model or "").strip()):
        raise BridgeError(10, "模型还没配置：请到「系统配置 → 模型」填好 API Key / 接口地址 / 模型名。")
    return cfg, LLMClient(cfg), extract_json


def build_messages(request: dict, instruction: str) -> list[dict]:
    system = (instruction
              + "\n\n只输出一个 JSON 对象，不要解释文字或代码围栏。结构如下：\n"
              + json.dumps(response_schema(request), ensure_ascii=False))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "本次输入 JSON：\n" + canonical(request)},
    ]


def call_http(request: dict, instruction: str, timeout: int) -> tuple[list[dict], dict]:
    """走 OpenAI 兼容的 HTTP 接口写稿；返回 (校验后的条目, 元信息)。"""
    cfg, client, extract_json = _llm_client()
    messages = build_messages(request, instruction)
    started = time.monotonic()
    # 一批最多 6 条、每条 200 字，程序默认的 2048 输出上限可能截断 JSON，这里放宽
    budget = min(8192, max(int(getattr(cfg, "max_tokens", 0) or 0), 4096))
    data = None
    last = None
    for attempt in range(2):
        ask = messages
        if attempt:
            ask = messages + [{"role": "user", "content":
                               "上一次输出不是合法 JSON。这次只输出 JSON 本身，不要任何解释或代码围栏。"}]
        try:
            # 写稿必须整段返回 JSON：关掉思考，别让它把输出预算吃光
            last = client.chat_with_metadata(ask, timeout=float(timeout),
                                             max_tokens=budget, thinking=False)
        except Exception as exc:  # noqa: BLE001 - 统一转成桥接错误，绝不返回可用稿件
            raise BridgeError(12, f"模型调用失败：{type(exc).__name__}: {exc}") from exc
        data = extract_json(last.content)
        if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
            break
        data = None
    if data is None:
        snippet = (getattr(last, "content", "") or "")[:200]
        raise BridgeError(12, "模型没有返回合法 JSON；本次结果不可用于发送。"
                              f"（finish_reason={getattr(last, 'finish_reason', '')}，开头：{snippet!r}）")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise BridgeError(12, "模型返回的 JSON 里没有 items；本次结果不可用于发送。")
    rows = validate_response(request, {"items": items})
    rows = _shorten_overlong(rows, client, timeout, budget)
    # Shortening changes the text. Recompute length/checks before persistence;
    # otherwise a fixed draft retains its pre-shortening rejected status.
    fields = response_schema(request)['properties']['items']['items']['required']
    rows = validate_response(request, {'items': [{key: row[key] for key in fields} for row in rows]})
    meta = {
        "model": cfg.model,
        "base_url": cfg.base_url,
        "finish_reason": getattr(last, "finish_reason", ""),
        "usage": getattr(last, "usage", None),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    return rows, meta


def _shorten_overlong(rows: list[dict], client, timeout: int, budget: int) -> list[dict]:
    """把超字数的稿子请模型压一遍（最多两遍）；压不下来就原样返回，交给下游校验拒掉。

    实测按「上限 200」写会稳定超 5–20 字，只压一遍仍可能停在 205 上下；
    第二遍把目标再收紧并明确允许删掉最次要的那项附加能力，仍然只做删减、不碰事实。
    """
    for pass_no in range(2):
        over = [r for r in rows if len(str(r.get("greeting") or "")) > MAX_GREETING_LENGTH]
        if not over:
            break
        target = MAX_GREETING_LENGTH - 20 - pass_no * 10
        payload = [{"job_id": r["job_id"], "greeting": r["greeting"],
                    "chars": len(str(r["greeting"]))} for r in over]
        extra = ("这是第二遍：上一遍压缩后仍然超字。请优先删掉最次要的一项附加能力，"
                 "保留主经历、届别与实习安排。") if pass_no else ""
        ask = [
            {"role": "system", "content":
             f"下面几条招呼语超过了 {MAX_GREETING_LENGTH} 字上限。逐条压到 {target} 字以内（含标点），"
             "只删重复表述、空泛结尾和可有可无的修饰；不得删除事实、不得改变事实强度、不得新增数字或事实。"
             + extra +
             '只输出 JSON：{"items":[{"job_id":"...","greeting":"..."}]}，不要解释。'},
            {"role": "user", "content": canonical({"items": payload})},
        ]
        try:
            result = client.chat_with_metadata(ask, timeout=float(timeout),
                                               max_tokens=budget, thinking=False)
        except Exception:  # noqa: BLE001 - 压缩失败不影响原有产出（会被下游按字数拒掉）
            break
        from boss_web.services.llm_client import extract_json

        data = extract_json(result.content)
        fixed = {}
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            for it in data["items"]:
                jid = str(it.get("job_id") or "")
                text = str(it.get("greeting") or "").strip()
                if jid and text and len(text) <= MAX_GREETING_LENGTH:
                    fixed[jid] = text
        if not fixed:
            break
        out = []
        for r in rows:
            new_text = fixed.get(str(r.get("job_id") or ""))
            if new_text:
                r = {**r, "greeting": new_text, "shortened": True}
            out.append(r)
        rows = out
    # 两遍都压不下来：在句号处截断（只删尾巴，不新增、不改写），总比整条作废强
    final = []
    for r in rows:
        text = str(r.get("greeting") or "").strip()
        if len(text) > MAX_GREETING_LENGTH:
            cut = _trim_to_limit(text, MAX_GREETING_LENGTH)
            if cut != text:
                r = {**r, "greeting": cut, "trimmed": True}
        final.append(r)
    return final


def _cut_at(text: str, limit: int) -> str | None:
    """截到 limit 内的最后一个句末标点（含标点）；找不到合适断点返回 None。"""
    cut = max((text.rfind(ch, 0, limit) for ch in "。！？"), default=-1)
    return text[:cut + 1] if cut >= 60 else None


#: 身份短句（届别 / 学历 / 在读）：截断时不能把它一起丢掉，HR 就看不出你是哪届的
IDENTITY = re.compile(r"(?:19|20)\d{2}\s*届[^。，；]{0,18}|[^。，；]{0,6}(?:本科|硕士)在读[^。，；]{0,8}")


def _trim_to_limit(text: str, limit: int) -> str:
    """按句末标点截到上限内；如果截掉了届别/在读，就把那句原样挪到结尾（只搬不改）。"""
    trimmed = _cut_at(text, limit)
    if not trimmed:
        return text
    if IDENTITY.search(trimmed):
        return trimmed
    found = IDENTITY.search(text)
    if not found:
        return trimmed
    clause = found.group(0).strip("，,、 。")
    shorter = _cut_at(text, limit - len(clause) - 2)
    if not shorter:
        return trimmed
    out = shorter.rstrip("。！？") + "，" + clause + "。"
    return out if len(out) <= limit else trimmed


def invoke(request: dict, timeout: int, retry: bool = False) -> tuple[dict, bool, Path]:
    # 规则文本 = 固定写作说明 + 程序里当前生效的写作规则（R2：不让提示词和校验各写一份）
    instruction = build_instruction()
    fingerprint = hashlib.sha256(canonical({"version": VERSION,
        "policy": POLICY.get("version"), "prompt": instruction, "request": request}).encode("utf-8")).hexdigest()
    run_dir = GREETING_ROOT / "writer" / "runs" / request["batch_id"] / fingerprint[:20]
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "result.json"
    lock_path = run_dir / ".lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise BridgeError(14, "同一请求正在运行，或上次进程异常退出留下锁；确认原进程退出后再处理。")
    try:
        with os.fdopen(lock_fd, "w", encoding="utf-8") as lock:
            lock.write(str(os.getpid()))
        if result_path.exists() and not retry:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            if cached.get("request_hash") != fingerprint:
                raise BridgeError(13, "缓存指纹不匹配。")
            cached["items"] = validate_response(request, {"items": [
                {k: row[k] for k in response_schema(request)["properties"]["items"]["items"]["required"]}
                for row in cached["items"]]})
            return cached, True, result_path
        # 默认通道：与评分共用程序里的模型配置，不再依赖外部 CLI 的额度
        attempt = run_dir / ("attempt-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        attempt.mkdir()
        save_json(attempt / "request.json", request)
        # 留存和实际调用共用的组装结果；不含模型Key或HTTP请求头。
        trace_path = attempt / 'input.json'
        input_trace = {'schema':'greeting-input/v1','request':request,
                       'messages':build_messages(request,instruction)}
        save_json(trace_path, input_trace)
        rows, meta = call_http(request, instruction, timeout)
        save_json(attempt / "answer.json", {"items": rows})
        result = {
            "status": "drafts_returned", "batch_id": request["batch_id"],
            "input_trace": trace_path.relative_to(GREETING_ROOT).as_posix(),
            "input_trace_sha": hashlib.sha256(canonical(input_trace).encode("utf-8")).hexdigest(),
            "writer": "http", "writer_runtime": "openai_compatible",
            "model": meta.get("model"), "base_url": meta.get("base_url"),
            "request_hash": fingerprint, "prompt_version": VERSION,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "elapsed_seconds": meta.get("elapsed_seconds"), "usage": meta.get("usage"),
            "finish_reason": meta.get("finish_reason"),
            "validation_scope": "结构/ID/字数/禁用词/占位符/开头去重/事实引用ID；事实语义由DSH另审",
            "items": rows,
        }
        save_json(result_path, result)
        return result, False, result_path
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path, help="greeting/ 内脱敏批次 JSON")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retry", action="store_true", help="显式重新调用，产生额外消耗")
    args = parser.parse_args()
    try:
        if not 30 <= args.timeout <= 600:
            raise BridgeError(10, "timeout 必须在 30–600 秒。")
        apply_policy()
        request = load_request(args.request)
        result, hit, path = invoke(request, args.timeout, args.retry)
        rejected = sum(i["validation_status"] == "rejected" for i in result["items"])
        print(canonical({"status": result["status"], "batch_id": request["batch_id"], "result_path": str(path),
            "cache_hit": hit, "items": len(result["items"]), "rejected": rejected,
            "needs_fact_review": len(result["items"]) - rejected, "approved_for_sending": False}))
        return 0
    except BridgeError as exc:
        print(canonical({"status": "error", "code": exc.code, "message": str(exc)}))
        return exc.code
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(canonical({"status": "error", "code": 13, "message": "输入或调用结果不可用", "error_type": type(exc).__name__}))
        return 13


if __name__ == "__main__":
    raise SystemExit(main())
