"""外部 AI 结果 → 正式业务存储的适配器。

`boss_core.external.submit` 只做校验；结果写进哪张表、进哪条审核链路由这里决定。
目的只有一个：让"工作项提交成功"等于"真的进了可查询、可审核的业务对象"，
而不是只落一个工作项 JSON 就报告 200。

各阶段落点：

* ``matching`` → `analysis` 表里一条逐岗结果（`stage=full` / `status=succeeded`，
  带来源快照与 JD 指纹）。下游准入、写稿、发送都从这份分析读，不另开一条路。
* ``greeting`` → 写回该分析里那条 `greeting`，再按**原有台账格式**追加一条
  `validated` 事件到 `greeting/queue.jsonl`。发送仍要人工审核 + 发送前复核，
  这里只把稿子送进待审队列。
* ``interview`` → 写进已有面试会话的题库。
* ``resume_revision`` → 建一条简历优化草稿（`status=draft`），不覆盖已保存简历。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from boss_core import compat, greeting as greeting_mod, interview as interview_mod
from boss_core.errors import InvalidInput, MissingConfiguration
from boss_core.jsonio import digest


def apply_result(ws, work: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """按阶段写进正式存储；返回可查询引用（`analysis_id` / `draft_id` / …）。"""
    stage = str(work.get("stage") or "")
    if stage == "matching":
        return _apply_matching(ws, work, payload)
    if stage == "greeting":
        return _apply_greeting(ws, work, payload)
    if stage == "interview":
        return _apply_interview(ws, work, payload)
    if stage == "resume_revision":
        return _apply_resume_revision(ws, work, payload)
    raise InvalidInput(f"这个阶段没有业务适配器：{stage}", field="stage")


# ------------------------------------------------------------------ 匹配结果


def _apply_matching(ws, work: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """外部评分 → 一条正式分析（逐岗结果、来源快照、JD 指纹齐全）。"""
    from .match_task import _blacklist_snapshot, _load_job
    from .resume_store import save_analysis, update_analysis_payload

    snap = _snapshot(ws, work)
    candidate = snap.get("candidate") or {}
    resume_id = str((candidate.get("resume") or {}).get("resume_id") or "")
    analysis_id = "an_" + digest(str(work["work_id"]))[:12]
    by_id = {str(j["job_id"]): j for j in work.get("jobs") or []}
    versions = (work.get("snapshot") or {}).get("versions") or {}
    items = []
    for result in payload.get("results") or []:
        jid = str(result.get("job_id"))
        job = _load_job(jid)
        if job is None:
            raise MissingConfiguration(f"岗位已不在库里：{jid}", field="job_ids")
        brief = by_id.get(jid) or {}
        jd_sha = _jd_sha(job.job_desc or "")
        items.append({
            "encrypt_job_id": jid,
            "job_name": job.job_name,
            "brand_name": job.brand_name,
            "location": job.location,
            "salary_desc": job.salary_desc,
            "job_experience": job.job_experience,
            "job_degree": job.job_degree,
            "brand_industry": job.brand_industry,
            "brand_scale_name": job.brand_scale_name,
            "security_id": job.security_id,
            "lid": job.lid,
            "last_greeted_at": job.last_greeted_at or "",
            "source_jd_sha": jd_sha,
            "analyzed_at": time.time(),
            **_blacklist_snapshot(job),
            "match_score": int(result.get("score") or 0),
            "matched_skills": [],
            "missing_skills": [],
            "verdict": str(result.get("verdict") or "")[:80],
            "pros": [],
            "cons": [],
            "advice": "",
            "jd_brief": "",
            "hard_constraints": [],
            "greeting": "",
            "stage": "full",
            "status": "succeeded",
            "attempt": 1,
            "source": {
                "kind": "external_ai",
                "work_id": work["work_id"],
                "input_hash": work.get("input_hash"),
                "strategy": (work.get("strategy") or {}).get("id"),
                "versions": versions,
            },
            "jd_sha_at_prepare": brief.get("jd_sha"),
        })
    body = {
        "analysis_id": analysis_id,
        "resume_id": resume_id,
        "resume_title": str(candidate.get("label") or ""),
        "created_at": time.time(),
        "status": "done",
        "llm": {"model": "external-skill", "base_url": "external"},
        "matches": items,
        "source": {"kind": "external_ai", "work_id": work["work_id"],
                   "input_hash": work.get("input_hash"), "versions": versions},
    }
    try:
        save_analysis(body)
    except sqlite3.IntegrityError:
        # 重试同一工作项：analysis_id 是确定性的，覆盖写而不是再插一条
        update_analysis_payload(body)
    return {"analysis_id": analysis_id, "stage": "matching", "jobs": len(items)}


# ------------------------------------------------------------------ 招呼语


def _apply_greeting(ws, work: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """外部招呼语 → 分析正文 + 台账 `validated` 事件（进原有待审队列）。"""
    from .resume_store import load_analysis, update_greeting

    analysis_id = str(work.get("analysis_id") or "")
    if not analysis_id:
        raise InvalidInput("这个招呼语工作项没记录分析来源，无法写进待审队列",
                           field="analysis_id",
                           next_steps=["prepare 时由服务端带上 analysis_id"])
    analysis = load_analysis(analysis_id)
    index = {str(it.get("encrypt_job_id")): it for it in analysis.get("matches") or []
             if isinstance(it, dict)}
    snap = _snapshot(ws, work)
    rules = compat.delivery_policy(snap)
    profile = compat.delivery_profile(snap)
    prefs_version = compat.preferences_version(snap)
    max_chars = int(rules.get("max_len") or 200)
    fact_ids = [fid for fid, _ in compat.candidate_facts(snap)]

    drafts = []
    for result in payload.get("results") or []:
        jid = str(result.get("job_id"))
        text = str(result.get("text") or "").strip()
        item = index.get(jid)
        if item is None:
            raise MissingConfiguration(f"分析 {analysis_id} 里没有这个岗位：{jid}",
                                       field="analysis_id")
        check = greeting_mod.validate_text(text, max_chars=max_chars, fact_ids=fact_ids)
        issues = list(check["issues"])
        issues += _grounding_issues(text, result.get("facts") or [], snap)
        if issues:
            raise InvalidInput("招呼语没有通过原有正文校验：" +
                               "；".join(str(i.get("message") or i) for i in issues[:3]),
                               field="text", detail={"issues": issues})
        update_greeting(analysis_id, jid, text)
        meta = _greeting_meta(work, item, analysis, text, rules, profile, prefs_version)
        meta["writer"] = "external_ai"
        meta["model"] = "external-skill"
        meta["checks"] = {"format": "ok", "facts": "ok:external_review",
                          "business": "external_ai"}
        _append_event(ws, "validated", jid, text=text, **meta)
        drafts.append({"job_id": jid, "draft_id": f"{analysis_id}:{jid}",
                       "sha": meta["sha"], "chars": len(text),
                       "review_pending": True})
    return {"analysis_id": analysis_id, "stage": "greeting", "drafts": drafts,
            "queue": str(_queue_path(ws))}


def _grounding_issues(text: str, used_facts: list[Any], snap: dict[str, Any]) -> list[dict[str, Any]]:
    """事实层确定性检查：正文里出现的数字必须能在事实/可用安排里找到出处。"""
    import re

    facts = {fid: txt for fid, txt in compat.candidate_facts(snap)}
    cited = [str(x) for x in used_facts]
    unknown = [x for x in cited if x not in facts]
    if unknown:
        return [{"code": "unknown_fact", "message": f"引用了不存在的事实：{'、'.join(unknown)}"}]
    # 事实引用标记（「事实: F1,F2」）里的数字是编号，不是正文声称的数字
    body = re.sub(r"事实[:：][^。！？\n]*", " ", text)
    haystack = " ".join(facts.get(x, "") for x in cited) if cited else " ".join(facts.values())
    avail = (snap.get("candidate") or {}).get("availability") or {}
    haystack += " " + " ".join(str(v) for v in avail.values() if v is not None)
    issues = []
    for number in sorted(set(re.findall(r"\d+(?:\.\d+)?", body))):
        if number not in haystack:
            issues.append({"code": "ungrounded_number",
                           "message": f"正文里的「{number}」在资料里找不到依据"})
    return issues


def _greeting_meta(work: dict[str, Any], item: dict[str, Any], analysis: dict[str, Any],
                   text: str, rules: dict[str, Any], profile: dict[str, Any],
                   prefs_version: str) -> dict[str, Any]:
    """台账元数据：字段与 `deliver_round_v2._greeting_meta` 一致，发送前复核才认。"""
    analysis_id = str(work.get("analysis_id") or "")
    return {
        "analysis_id": analysis_id,
        "company": str(item.get("brand_name") or ""),
        "title": str(item.get("job_name") or ""),
        "analysis_snapshot": {
            "analysis_id": analysis_id,
            "analysis_at": analysis.get("created_at"),
            "score": int(item.get("match_score") or 0),
            "verdict": str(item.get("verdict") or ""),
            "matched_skills": item.get("matched_skills") or [],
            "missing_skills": item.get("missing_skills") or [],
            "pros": item.get("pros") or [],
            "cons": item.get("cons") or [],
            "hard_constraints": item.get("hard_constraints") or [],
            "jd_sha": str(item.get("source_jd_sha") or ""),
        },
        "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "jd_sha": str(item.get("source_jd_sha") or ""),
        "score": int(item.get("match_score") or 0),
        "policy_version": str(rules.get("version") or ""),
        "profile_version": str(profile.get("version") or ""),
        "selection_version": str(rules.get("selection_version") or "selection-legacy"),
        "prefs_version": prefs_version,
    }


def _queue_path(ws) -> Path:
    return Path(ws.root) / "greeting" / "queue.jsonl"


def _append_event(ws, event: str, job_id: str, **data: Any) -> None:
    """按 `deliver_round_v2.append_event` 的格式追加台账事件。"""
    path = _queue_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": dt.datetime.now().isoformat(timespec="seconds"),
              "event": event, "job_id": job_id, **data}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 面试


def _apply_interview(ws, work: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """外部出题 → 写进已有面试会话题库。"""
    session_id = str(work.get("session_id") or "")
    if not session_id:
        raise InvalidInput("这个出题工作项没记录面试会话，无法写入",
                           field="session_id", next_steps=["prepare 时带上 session_id"])
    questions = [{"question": str(q.get("question") or ""),
                  "type": str(q.get("type") or "general")}
                 for q in payload.get("questions") or []]
    session = interview_mod.set_questions(ws, session_id, questions, actor="external_ai")
    return {"session_id": session["id"], "stage": "interview", "questions": len(questions)}


# ------------------------------------------------------------------ 简历优化


def _apply_resume_revision(ws, work: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """外部改稿 → 一条简历优化草稿（不覆盖已保存简历）。"""
    from .resume_store import (
        REVISION_DRAFT,
        create_revision,
        load_resume,
        update_revision_result,
    )

    snap = _snapshot(ws, work)
    candidate = snap.get("candidate") or {}
    resume_id = str((candidate.get("resume") or {}).get("resume_id") or "")
    if not resume_id:
        raise MissingConfiguration("候选人没有关联简历，无法保存改稿", field="resume_id")
    try:
        original = load_resume(resume_id).raw
    except FileNotFoundError as exc:
        raise MissingConfiguration(f"找不到简历：{resume_id}") from exc
    revision_id = "rev_" + digest(str(work["work_id"]))[:12]
    markdown = str(payload.get("markdown") or "").strip()
    try:
        create_revision(
            revision_id=revision_id, resume_id=resume_id,
            resume_title=str(candidate.get("label") or ""),
            target_kind="jobs", target_json={"job_ids": [j["job_id"] for j in work.get("jobs") or []]},
            crawl_task_id="", job_count=len(work.get("jobs") or []),
            original_md=original, mode="targeted", selected_sections=[],
            optimization_prompt="外部 AI 改稿",
        )
    except sqlite3.IntegrityError:
        pass  # 重试同一工作项：沿用已有草稿行
    update_revision_result(revision_id, revised_md=markdown, changes=[],
                           model="external-skill", status=REVISION_DRAFT)
    return {"revision_id": revision_id, "stage": "resume_revision",
            "status": REVISION_DRAFT, "resume_id": resume_id}


# ------------------------------------------------------------------ 公共


def _snapshot(ws, work: dict[str, Any]) -> dict[str, Any]:
    """按工作项记录的那一版方案重建快照（不读"当前活动方案"以外的猜测）。"""
    from boss_core import snapshot as snapshot_mod

    plan_id = (work.get("snapshot") or {}).get("plan")
    if not plan_id:
        raise MissingConfiguration("工作项没记录方案版本，无法定位业务落点", field="plan_id")
    return snapshot_mod.build(ws, plan_id=str(plan_id))


def _jd_sha(text: str) -> str:
    """与 `match_task._jd_sha` 同口径的 JD 指纹。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
