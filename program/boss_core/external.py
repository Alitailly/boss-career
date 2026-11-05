"""外部 AI 工作项：把"这段活交给别人的模型做"变成可校验的输入/输出。

流程：程序准备输入（快照 + 策略 + schema + `input_hash`）→ 宿主 AI 执行 →
程序校验结果（顶层类型、岗位集合、schema、证据引用、重复提交、输入是否过期）→
合格后由业务适配器写进正式存储（分析结果 / 待审招呼语 / 面试会话 / 简历草稿）。

`input_hash` 绑定"当时用的资料/方案/策略/岗位文本"。提交时服务器**自己**重算当前
有效性（不靠调用方自报）：方案已被换掉、快照指纹变了、岗位 JD 换了，旧结果一律
判 `stale`，只留档，不能再当准入或发送依据（任务书 §5 的 `stale_input`）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .errors import Conflict, ExternalResultInvalid, NotFound, StaleInput
from .jsonio import digest, new_id, now_iso, read_json, write_json

#: 结果结构用**类型名**声明，方便原样写进工作项 JSON 给外部 AI 看。
#:
#: * `list`   顶层承载结果的键；`None` 表示单文档阶段（简历优化），不按岗位循环。
#: * `per_job` 是否要求"每个岗位一条结果"。
#: * `required` 顶层必填键（单文档阶段同时也是字段名）。
RESULT_SCHEMA: dict[str, dict[str, Any]] = {
    "matching": {
        "required": ["results"],
        "list": "results",
        "per_job": True,
        "item": {"job_id": "str", "score": "int", "verdict": "str"},
    },
    "greeting": {
        "required": ["results"],
        "list": "results",
        "per_job": True,
        "item": {"job_id": "str", "text": "str", "facts": "list"},
    },
    "interview": {
        "required": ["questions"],
        "list": "questions",
        "per_job": True,
        # 一个岗位可以出多道题：不做"每岗恰好一条"的约束
        "one_per_job": False,
        "item": {"job_id": "str", "question": "str", "type": "str"},
    },
    "resume_revision": {
        "required": ["markdown"],
        "list": None,
        "per_job": False,
        "item": {"markdown": "str"},
    },
}

_TYPES = {"str": str, "int": int, "list": list, "bool": bool}

#: 文本字段长度区间；只对"写给人看"的字段设限，分数另有区间。
_LIMITS: dict[tuple[str, str], tuple[int, int]] = {
    ("matching", "verdict"): (1, 120),
    ("greeting", "text"): (5, 2000),
    ("interview", "question"): (2, 500),
    ("resume_revision", "markdown"): (20, 20000),
}


def _item_schema(stage: str) -> dict[str, Any]:
    """该阶段允许的结果结构；未知阶段直接拒绝。"""
    if stage not in RESULT_SCHEMA:
        raise ExternalResultInvalid(f"不支持外部提交的阶段：{stage}", field="stage",
                                    detail={"supported": sorted(RESULT_SCHEMA)})
    return RESULT_SCHEMA[stage]


def _type_name(value: Any) -> str:
    """给人看的 JSON 类型名（bool 不能混成 int）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def create(ws, *, stage: str, jobs: list[dict[str, Any]], snapshot: dict[str, Any],
           strategy: dict[str, Any] | None, analysis_id: str | None = None,
           session_id: str | None = None, actor: str = "ai") -> dict[str, Any]:
    """准备一个工作项；不调模型、不产生发送副作用。"""
    schema = _item_schema(stage)
    if not jobs:
        raise ExternalResultInvalid("工作项至少要有一个岗位", field="jobs")
    clean = []
    for job in jobs:
        jid = str(job.get("job_id") or job.get("encrypt_job_id") or "")
        if not jid:
            raise ExternalResultInvalid("岗位缺 job_id", field="jobs")
        jd = str(job.get("jd") or job.get("job_desc") or "")
        clean.append({
            "job_id": jid,
            "title": str(job.get("title") or job.get("job_name") or ""),
            "company": str(job.get("company") or job.get("brand_name") or ""),
            "jd": jd,
            "jd_sha": _text_sha(jd),
        })
    work_id = new_id("work")
    source = str((strategy or {}).get("source") or "")
    payload = {
        "work_id": work_id,
        "stage": stage,
        "created_at": now_iso(),
        "created_by": actor,
        "status": "open",
        # 外部技能由宿主 AI 执行；内置/自定义提示词才由本机模型跑。
        "execution": "host_ai" if source == "external_skill" else "local",
        "analysis_id": analysis_id,
        "session_id": session_id,
        "snapshot": {
            "fingerprint": snapshot.get("fingerprint"),
            "versions": snapshot.get("versions"),
            "candidate": (snapshot.get("candidate") or {}).get("id"),
            "plan": (snapshot.get("plan") or {}).get("id"),
        },
        "strategy": {"id": (strategy or {}).get("id"),
                     "stage": (strategy or {}).get("stage"),
                     "source": source,
                     "content": (strategy or {}).get("content")},
        "jobs": clean,
        "schema": schema,
        "result": None,
    }
    payload["input_hash"] = _hash(payload)
    write_json(ws.tasks_dir() / f"{work_id}.json", payload)
    ws.audit(actor, "external.prepare", work_id,
             {"stage": stage, "jobs": len(clean), "input_hash": payload["input_hash"],
              "execution": payload["execution"]})
    return payload


def load(ws, work_id: str) -> dict[str, Any]:
    """读工作项；不存在抛 `NotFound`。"""
    path = ws.tasks_dir() / f"{work_id}.json"
    if not path.is_file():
        raise NotFound(f"找不到工作项：{work_id}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise NotFound(f"工作项文件损坏：{work_id}")
    return data


#: 业务适配器签名：`apply(ws, work, payload) -> dict`，返回可查询引用（analysis_id/draft_id…）。
Applier = Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, Any]]


def submit(ws, work_id: str, payload: dict[str, Any], *, actor: str = "ai",
           snapshot: dict[str, Any] | None = None,
           jd_lookup: Callable[[str], str] | None = None,
           verify_current: bool = True,
           apply: Applier | None = None) -> dict[str, Any]:
    """校验并保存外部结果，再交给业务适配器写入正式存储。

    校验顺序：工作项存在 → 未过期/未重复 → **服务器重算当前有效性** → 顶层类型 →
    岗位集合 → 字段类型/区间 → 证据引用 → 业务落库。任何一步不过都不写半份结果。

    Args:
        ws: 工作区。
        work_id: 工作项 id。
        payload: 外部 AI 交回的结果。
        actor: 审计标签。
        snapshot: 可选，调用方手上的快照；给了就要求与工作项一致（兼容旧调用）。
        jd_lookup: 可选，按 job_id 取当前 JD 文本；给了就核对 JD 是否被改过。
        verify_current: 是否做服务器侧有效性重算（默认做，测试底层时可关）。
        apply: 业务适配器；返回写进正式存储的引用。
    """
    work = load(ws, work_id)
    status = str(work.get("status") or "")

    if status == "stale":
        raise StaleInput("这份输入已经过期，结果只能留档，不能再当作当前依据",
                         field="work_id",
                         detail={"stale": work.get("stale") or {}})
    if status in {"submitted", "applied"}:
        if work.get("result_hash") == digest(payload):
            return {"ok": True, "work_id": work_id, "status": status, "duplicate": True,
                    "current": not _drift(ws, work, jd_lookup),
                    "applied": work.get("applied"),
                    "message": "同一份结果重复提交，已按幂等处理"}
        raise Conflict("这个工作项已经提交过结果", field="work_id",
                       detail={"submitted_at": work.get("submitted_at")})

    if snapshot is not None and snapshot.get("fingerprint") != work["snapshot"].get("fingerprint"):
        raise StaleInput("资料或方案已经变了，这份输入不能再用作当前依据",
                         field="snapshot", detail={"work_input": work["snapshot"].get("fingerprint"),
                                                   "now": snapshot.get("fingerprint")})

    if verify_current:
        drift = _drift(ws, work, jd_lookup)
        if drift:
            _mark_stale(ws, work, payload, drift, actor)
            raise StaleInput("这份输入在当前方案下已经过期：" +
                             "；".join(str(d.get("message") or "") for d in drift[:3]),
                             field=str(drift[0].get("code") or "snapshot"),
                             next_steps=["重新 prepare_work 取当前输入，再提交新结果"],
                             detail={"drift": drift})

    errors, clean = _validate(ws, work, payload)
    if errors:
        raise ExternalResultInvalid("提交的结果不合格", field=errors[0].get("field"),
                                    next_steps=["按 schema 补齐后重试"],
                                    detail={"errors": errors[:20]})

    applied: dict[str, Any] = {}
    if apply is not None:
        applied = apply(ws, work, clean) or {}

    result_hash = digest(payload)
    work.update({
        "status": "applied" if apply is not None else "submitted",
        "result": payload,
        "result_hash": result_hash,
        "submitted_at": now_iso(),
        "submitted_by": actor,
        "applied": applied or None,
    })
    write_json(ws.tasks_dir() / f"{work_id}.json", work)
    ws.audit(actor, "external.submit", work_id,
             {"result_hash": result_hash, "stage": work["stage"], "applied": applied or {}})
    return {"ok": True, "work_id": work_id, "status": work["status"],
            "result_hash": result_hash, **({"applied": applied} if applied else {})}


def _mark_stale(ws, work: dict[str, Any], payload: dict[str, Any],
                drift: list[dict[str, Any]], actor: str) -> None:
    """过期结果留档：状态置 stale，正文进 `rejections`，绝不进当前流程。"""
    history = list(work.get("rejections") or [])
    history.append({"at": now_iso(), "code": "stale_input", "drift": drift,
                    "payload": payload})
    work.update({
        "status": "stale",
        "stale": {"at": now_iso(), "drift": drift},
        "rejections": history[-5:],
    })
    write_json(ws.tasks_dir() / f"{work['work_id']}.json", work)
    ws.audit(actor, "external.stale", work["work_id"],
             {"drift": [d.get("code") for d in drift]})


def _drift(ws, work: dict[str, Any], jd_lookup: Callable[[str], str] | None = None) -> list[dict[str, Any]]:
    """服务器侧有效性：这份结果现在还能不能当依据。

    判据（任一不成立即过期）：
    1. 工作项记录的方案仍是当前活动方案——换了方案，旧结果不再对应当前口径；
    2. 用该方案重建的快照指纹与工作项一致——资料/策略/方案内容变了；
    3. 每个岗位的 JD 指纹仍是工作项里那个——JD 被重新抓取或编辑过。
    """
    from . import snapshot as snapshot_mod

    problems: list[dict[str, Any]] = []
    snap = work.get("snapshot") or {}
    plan_id = snap.get("plan")
    try:
        active = ws.active_plan()
    except Exception:  # noqa: BLE001 - 工作区损坏时按"无法确认"处理
        active = None
    if not plan_id:
        problems.append({"code": "no_plan", "message": "工作项没记录方案版本，无法确认当前有效性"})
    elif active is None or str(active.get("id")) != str(plan_id):
        problems.append({"code": "plan_changed",
                         "message": "活动方案已经换过（工作项用的是另一个版本）",
                         "work_plan": plan_id, "active_plan": (active or {}).get("id")})
    else:
        try:
            now = snapshot_mod.build(ws, plan_id=str(plan_id))
        except Exception as exc:  # noqa: BLE001 - 重建失败也算无法确认
            problems.append({"code": "snapshot_unavailable",
                             "message": f"当前方案重建快照失败：{exc}"})
        else:
            if now.get("fingerprint") != snap.get("fingerprint"):
                problems.append({"code": "snapshot_changed",
                                 "message": "资料或方案内容变了（快照指纹不同）",
                                 "work": snap.get("fingerprint"), "now": now.get("fingerprint")})
    if jd_lookup is not None:
        for job in work.get("jobs") or []:
            jid = str(job.get("job_id") or "")
            try:
                current = jd_lookup(jid)
            except Exception:  # noqa: BLE001 - 岗位被删/库读不到
                problems.append({"code": "job_missing", "job_id": jid,
                                 "message": f"岗位已不在库里：{jid}"})
                continue
            if _text_sha(current) != str(job.get("jd_sha") or ""):
                problems.append({"code": "jd_changed", "job_id": jid,
                                 "message": f"岗位 JD 已经更新过：{jid}"})
    return problems


def _validate(ws, work: dict[str, Any], payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """完整 schema 校验；返回 `(errors, cleaned)`。

    先验顶层类型再处理内容：`results` 传字符串/字典不会被当成"没有条目"跳过。
    """
    stage = str(work.get("stage") or "")
    schema = _item_schema(stage)
    errors: list[dict[str, Any]] = []
    for key in schema["required"]:
        if key not in payload:
            errors.append({"code": "missing_field", "field": key, "message": f"缺 {key}"})

    list_key = schema.get("list")
    if list_key is None:
        # 单文档阶段（简历优化）：不加"每岗一条"的循环约束
        cleaned = {k: payload.get(k) for k in schema["item"] if k in payload}
        errors.extend(_check_fields(stage, payload, schema["item"], ""))
        return errors, cleaned

    items = payload.get(list_key)
    if not isinstance(items, list):
        errors.append({"code": "invalid_type", "field": list_key,
                       "message": f"{list_key} 必须是数组，收到 {_type_name(items)}"})
        return errors, {}
    if not items:
        errors.append({"code": "empty_results", "field": list_key,
                       "message": f"{list_key} 是空数组，至少要有 {len(work.get('jobs') or [])} 条"})
    if not schema.get("per_job"):
        return errors, {list_key: items}

    allowed = {str(item["job_id"]) for item in work.get("jobs") or []}
    facts = _fact_ids(ws, work)
    one_per_job = bool(schema.get("one_per_job", True))
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"code": "invalid_item", "field": f"{list_key}[{idx}]",
                           "message": f"应为对象，收到 {_type_name(item)}"})
            continue
        jid = item.get("job_id")
        if not isinstance(jid, str) or not jid.strip():
            errors.append({"code": "invalid_type", "field": f"{list_key}[{idx}].job_id",
                           "message": f"job_id 必须是非空字符串，收到 {_type_name(jid)}"})
        else:
            if jid not in allowed:
                errors.append({"code": "unknown_job", "field": f"{list_key}[{idx}].job_id",
                               "message": f"不在本次工作项里：{jid}"})
            if one_per_job and jid in seen:
                errors.append({"code": "duplicate_job", "field": f"{list_key}[{idx}].job_id",
                               "message": f"同一个岗位给了两条结果：{jid}"})
            seen.add(jid)
        errors.extend(_check_fields(stage, item, schema["item"], f"{list_key}[{idx}]"))
        score = item.get("score")
        if isinstance(score, int) and not isinstance(score, bool) and not 0 <= score <= 100:
            errors.append({"code": "score_out_of_range", "field": f"{list_key}[{idx}].score",
                           "message": f"分数 {score} 超出 0–100"})
        if "facts" in schema["item"] and isinstance(item.get("facts"), list):
            for fid in item["facts"]:
                if not isinstance(fid, str) or fid not in facts:
                    errors.append({"code": "unknown_fact", "field": f"{list_key}[{idx}].facts",
                                   "message": f"引用了不存在的事实：{fid}"})

    missing = allowed - seen
    if one_per_job and missing:
        errors.append({"code": "missing_jobs", "field": list_key,
                       "message": f"缺 {len(missing)} 个岗位的结果",
                       "detail": sorted(missing)[:20]})
    return errors, {list_key: items}


def _check_fields(stage: str, item: dict[str, Any], spec: dict[str, str],
                  prefix: str) -> list[dict[str, Any]]:
    """逐字段查类型/长度；`prefix` 为空表示单文档阶段（字段在顶层）。"""
    errors: list[dict[str, Any]] = []
    for key, expect in spec.items():
        name = f"{prefix}.{key}" if prefix else key
        if key not in item:
            errors.append({"code": "missing_field", "field": name, "message": f"缺 {key}"})
            continue
        value = item[key]
        want = _TYPES.get(expect)
        ok = isinstance(value, want) if want else True
        if want is int and isinstance(value, bool):
            ok = False  # bool 是 int 的子类，别把 True 当分数
        if not ok:
            errors.append({"code": "invalid_type", "field": name,
                           "message": f"应为 {expect}，收到 {_type_name(value)}"})
            continue
        if (stage, key) in _LIMITS and isinstance(value, str):
            low, high = _LIMITS[(stage, key)]
            if len(value) < low or len(value) > high:
                errors.append({"code": "invalid_length", "field": name,
                               "message": f"长度应在 {low}–{high} 之间，现在 {len(value)}"})
    extra = set(item) - set(spec)
    if extra and not prefix:
        errors.append({"code": "unknown_field", "field": ",".join(sorted(extra)),
                       "message": f"单文档阶段只接受 {sorted(spec)}"})
    return errors


def _text_sha(text: Any) -> str:
    """JD 文本指纹：与 `deliver_round_v2.jd_sha` 同口径（正文 sha256 前 16 位）。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _hash(payload: dict[str, Any]) -> str:
    """输入指纹：不含 id/时间/状态，只算真正影响结果的字段。"""
    body = {k: payload[k] for k in ("stage", "snapshot", "strategy", "jobs", "schema") if k in payload}
    return digest(body)


def _fact_ids(ws, work: dict[str, Any]) -> set[str]:
    """本次工作项允许的事实编号（来自快照里的候选人）。"""
    candidate = None
    plan_id = work.get("snapshot", {}).get("plan")
    if plan_id:
        try:
            plan = ws.get("plan", plan_id)
            candidate = ws.get("candidate", plan.get("candidate_id"))
        except NotFound:
            candidate = ws.active_candidate()
    else:
        candidate = ws.active_candidate()
    return {str(f.get("id")) for f in (candidate or {}).get("facts") or []}


def list_works(ws, limit: int = 50) -> list[dict[str, Any]]:
    """最近的工作项摘要（不含完整结果）。"""
    folder: Path = ws.tasks_dir()
    if not folder.is_dir():
        return []
    rows = []
    for path in folder.glob("work_*.json"):
        try:
            data = read_json(path)
        except ValueError:
            continue
        rows.append({"work_id": data.get("work_id"), "stage": data.get("stage"),
                     "status": data.get("status"), "created_at": data.get("created_at"),
                     "jobs": len(data.get("jobs") or []),
                     "execution": data.get("execution"),
                     "input_hash": data.get("input_hash")})
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:limit]
