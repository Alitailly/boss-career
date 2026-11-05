"""面试会话：题目、回答、追问、反馈按回合落盘，换进程/新会话都能接着聊。

旧实现只有一张"按岗位生成的题目"表，没有会话、没有回答、没有回合；面板刷新就丢。
这里把会话做成工作区里的文件：

    interviews/<session_id>/session.json   会话本身（状态、题目、游标、快照）
    interviews/<session_id>/turns.jsonl    逐轮追加（谁在第几轮说了什么）

两条硬规则：

* **一次只问一题**：上一题没有用户回答时，`next_question` 返回同一题，不当作新题；
* **提交带幂等键**：同一个 `key` 重复提交不会产生第二条回合、也不会覆盖已答内容。
"""

from __future__ import annotations

from typing import Any

from .errors import Conflict, InvalidInput, NotFound
from .jsonio import append_jsonl, digest, new_id, now_iso, read_json, read_jsonl, write_json

SCHEMA = "interview_session/v1"
ROLES = ("interviewer", "user", "feedback", "system")


def _dir(ws, sid: str):
    return ws.interview_dir(sid)


def _load(ws, sid: str) -> dict[str, Any]:
    """读会话；不存在抛 `NotFound`。"""
    path = _dir(ws, sid) / "session.json"
    if not path.is_file():
        raise NotFound(f"找不到面试会话：{sid}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise NotFound(f"面试会话文件损坏：{sid}")
    return data


def _save(ws, session: dict[str, Any]) -> None:
    write_json(_dir(ws, session["id"]) / "session.json", session)


def create(ws, *, job_id: str, snapshot: dict[str, Any], strategy_id: str | None,
           actor: str = "ai") -> dict[str, Any]:
    """开一个新会话；同一岗位可以有多轮练习，旧的不会被覆盖。"""
    if not str(job_id or "").strip():
        raise InvalidInput("job_id 不能为空", field="job_id")
    sid = new_id("itr")
    session = {
        "schema": SCHEMA,
        "id": sid,
        "job_id": str(job_id),
        "created_at": now_iso(),
        "created_by": actor,
        "status": "active",
        "strategy_id": strategy_id,
        "snapshot": {"fingerprint": snapshot.get("fingerprint"),
                     "versions": snapshot.get("versions")},
        "questions": [],
        "cursor": 0,
        "turn_count": 0,
        "last_at": now_iso(),
    }
    _save(ws, session)
    ws.audit(actor, "interview.create", sid, {"job_id": job_id})
    return session


def set_questions(ws, sid: str, questions: list[dict[str, Any]], *, actor: str = "ai") -> dict[str, Any]:
    """写入题库；已问过的题保留顺序，新题追加在后面。"""
    session = _load(ws, sid)
    if session.get("status") != "active":
        raise Conflict("会话已结束，不能再改题目", field="status")
    clean = []
    for idx, item in enumerate(questions):
        text = str(item.get("question") or item.get("text") or "").strip()
        if not text:
            raise InvalidInput(f"第 {idx + 1} 题没有内容", field=f"questions[{idx}]")
        clean.append({"index": len(session["questions"]) + idx, "text": text,
                      "type": str(item.get("type") or "general"),
                      "difficulty": str(item.get("difficulty") or "medium")})
    session["questions"].extend(clean)
    session["last_at"] = now_iso()
    _save(ws, session)
    ws.audit(actor, "interview.questions", sid, {"added": len(clean)})
    return session


def turns(ws, sid: str) -> list[dict[str, Any]]:
    """按顺序返回回合。"""
    return read_jsonl(_dir(ws, sid) / "turns.jsonl")


def state(ws, sid: str) -> dict[str, Any]:
    """会话 + 回合 + 下一步该做什么（前端/外部 AI 都用这一份）。"""
    session = _load(ws, sid)
    rows = turns(ws, sid)
    return {**session, "turns": rows, "turn_count": len(rows),
            "pending": _pending(session, rows)}


def _pending(session: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """下一步：等用户回答、该问下一题、还是要反馈。"""
    if session.get("status") != "active":
        return {"action": "finished"}
    last = rows[-1] if rows else None
    asked = [r for r in rows if r.get("role") == "interviewer"]
    answered = [r for r in rows if r.get("role") == "user"]
    if last and last.get("role") == "interviewer" and len(asked) > len(answered):
        return {"action": "awaiting_answer", "question": last.get("text"),
                "question_index": last.get("question_index")}
    if len(asked) < len(session.get("questions") or []):
        nxt = session["questions"][len(asked)]
        return {"action": "ask_next", "question": nxt["text"], "question_index": nxt["index"]}
    return {"action": "wrap_up"}


def add_turn(ws, sid: str, *, role: str, text: str, key: str | None = None,
             actor: str = "user", question_index: int | None = None) -> dict[str, Any]:
    """追加一轮；`key` 相同的重复提交直接返回上一轮（幂等）。"""
    if role not in ROLES:
        raise InvalidInput(f"不支持的回合类型：{role}", field="role")
    body = str(text or "").strip()
    if not body:
        raise InvalidInput("回合内容不能为空", field="text")
    session = _load(ws, sid)
    rows = turns(ws, sid)
    if key:
        for row in rows:
            if row.get("key") == key:
                return {"ok": True, "duplicate": True, "turn": row}
    if session.get("status") != "active":
        raise Conflict("会话已结束，不能再追加回合", field="status")

    if role == "interviewer" and question_index is None:
        asked = [r for r in rows if r.get("role") == "interviewer"]
        if len(asked) >= len(session.get("questions") or []):
            raise Conflict("题库已经问完，先补题或结束会话", field="questions")
        question_index = session["questions"][len(asked)]["index"]

    turn = {
        "turn": len(rows) + 1,
        "role": role,
        "text": body,
        "at": now_iso(),
        "by": actor,
        "key": key or "",
        "question_index": question_index,
        "sha": digest(body)[:16],
    }
    append_jsonl(_dir(ws, sid) / "turns.jsonl", turn)
    session["turn_count"] = len(rows) + 1
    session["cursor"] = sum(1 for r in rows if r.get("role") == "interviewer") + (
        1 if role == "interviewer" else 0)
    session["last_at"] = now_iso()
    _save(ws, session)
    return {"ok": True, "duplicate": False, "turn": turn, "session": session}


def finish(ws, sid: str, *, actor: str = "user", reason: str = "") -> dict[str, Any]:
    """结束会话；已答内容保留，不再接收回合。"""
    session = _load(ws, sid)
    if session.get("status") == "finished":
        return session
    session.update({"status": "finished", "finished_at": now_iso(), "reason": reason})
    _save(ws, session)
    ws.audit(actor, "interview.finish", sid, {"reason": reason})
    return session


def list_sessions(ws, *, job_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """最近的会话摘要；给了 `job_id` 就只列这个岗位。"""
    base = ws.interview_dir()
    if not base.is_dir():
        return []
    rows = []
    for folder in base.iterdir():
        path = folder / "session.json"
        if not path.is_file():
            continue
        try:
            data = read_json(path)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if job_id and data.get("job_id") != job_id:
            continue
        rows.append({"id": data.get("id"), "job_id": data.get("job_id"),
                     "status": data.get("status"), "created_at": data.get("created_at"),
                     "last_at": data.get("last_at"), "turns": data.get("turn_count"),
                     "questions": len(data.get("questions") or [])})
    rows.sort(key=lambda r: str(r.get("last_at") or ""), reverse=True)
    return rows[:limit]


def resume(ws, *, job_id: str | None = None) -> dict[str, Any] | None:
    """接着上次没结束的会话；没有就返回 None（由调用方决定开新会话）。"""
    for row in list_sessions(ws, job_id=job_id):
        if row.get("status") == "active":
            return state(ws, str(row["id"]))
    return None
