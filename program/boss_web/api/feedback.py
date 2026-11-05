"""反馈：回读已打招呼会话里的对方消息（只读）。

BOSS 没有“回复率”接口，只能按会话回读：`encryptBossId` → 数字 uid →
`fetch_chat_history(uid)`。这里只做一次有限范围内的读取，不改任何投递状态。

返回约定（调用方必须按此处理，不能把失败当成“未回复”）：

- `ok=true`：读到会话，`messages` 为原始消息的精简投影；
- `ok=false`：`error` 说明原因（没建会话、接口失败、无登录态等），
  调用方应记“反馈未知”，而不是“HR 未回复”。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..errors import UpstreamError, ValidationWebError

router = APIRouter()

MAX_JOBS = 20
MAX_MESSAGES = 200


class ChatBody(BaseModel):
    """一次回读请求。

    Attributes:
        encrypt_job_ids: 要回读的职位 id，最多 20 个。
        limit: 每个会话最多返回多少条消息。
    """

    encrypt_job_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=MAX_MESSAGES)
    debug: bool = False
    sender_account_id: str = ''
    application_ids: dict[str, str] = Field(default_factory=dict)


def _as_int(value: Any) -> int:
    """把可能是字符串/浮点的时间或 id 转成整数，失败回 0。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _pick_message(raw: dict[str, Any]) -> dict[str, Any]:
    """从原始消息里挑出可稳定识别的字段。

    站点返回结构没有公开文档，字段名按常见形态兜底；识别不到的字段留空，
    由调用方判断“分类不确定”。

    Args:
        raw: 原始消息字典。

    Returns:
        精简后的消息字典。
    """
    sender = raw.get("from") if isinstance(raw.get("from"), dict) else {}
    target = raw.get("to") if isinstance(raw.get("to"), dict) else {}
    text = raw.get("text")
    if text is None:
        text = raw.get("content")
    if isinstance(text, dict):
        text = text.get("text") or text.get("content")
    return {
        "mid": raw.get("mid") or raw.get("messageId") or raw.get("id") or "",
        "from_uid": _as_int(sender.get("uid") if sender else raw.get("fromUid") or raw.get("uid")),
        "to_uid": _as_int(target.get("uid") if target else raw.get("toUid")),
        "type": raw.get("type") or raw.get("msgType") or "",
        "time": _as_int(raw.get("time") or raw.get("createTime") or raw.get("timestamp")),
        "text": str(text or "").strip()[:500],
        "raw_keys": ",".join(sorted(str(k) for k in raw.keys()))[:200],
    }


@router.post("/chat")
def chat(body: ChatBody) -> dict[str, Any]:
    from ..services.feedback_reader import reading
    from boss_login.account import AccountChangedError
    from ..errors import ConflictError
    try:
        with reading():
            return _chat(body)
    except AccountChangedError as exc:
        raise ConflictError(str(exc)) from exc


def _chat(body: ChatBody) -> dict[str, Any]:
    """按职位回读会话；普通单岗失败保留未知，平台限制会中止整体回读。

    Args:
        body: 职位列表与每会话消息上限。

    Returns:
        `{"ok": bool, "my_uid": int, "items": [...]}`；整体不可用时抛
        `UpstreamError` 或 `ValidationWebError`。
    """
    from boss_jobs import create_client
    from boss_jobs.errors import JobError
    from boss_jobs.store import JobStore
    from ..services.troubleshoot import humanize
    from ..services.feedback_reader import request

    ids = [str(i).strip() for i in body.encrypt_job_ids if str(i).strip()]
    if not ids:
        raise ValidationWebError("没有要回读的岗位")
    if len(ids) > MAX_JOBS:
        raise ValidationWebError(f"一次最多回读 {MAX_JOBS} 个岗位")

    from boss_login.account import binding, AccountChangedError
    from ..errors import ConflictError
    try:
        identity = binding()
    except AccountChangedError as exc:
        raise ConflictError(str(exc)) from exc
    if body.sender_account_id and body.sender_account_id != identity.account_id:
        raise ConflictError('当前登录账号与原发送账号不一致，本次未访问平台')

    from boss_core.feedback import load_events, sent_jobs
    from .workflow import workflow_dir
    folder = workflow_dir()
    sent = sent_jobs(load_events(folder / 'greeting/queue.jsonl')) if folder else {}
    selected = {}
    for jid in ids:
        choices = [row for row in sent.values() if row['job_id'] == jid and
                   row.get('sender_account_id') == identity.account_id and
                   (not body.application_ids.get(jid) or row.get('application_id') == body.application_ids[jid])]
        if len(choices) != 1:
            raise ConflictError('未找到当前账号唯一对应的发送记录；历史归属待确认，不回读其他账号会话')
        selected[jid] = choices[0]

    try:
        with JobStore() as store:
            jobs = {jid: store.get_job(jid) for jid in ids}
            # A recruiter conversation may cover multiple applications. Preserve
            # the messages, but do not claim which JD received the reply.
            peer_counts = {}
            for info in sent.values():
                if info.get('sender_account_id') != identity.account_id:
                    continue
                sent_id = info['job_id']
                candidate = jobs.get(sent_id) or store.get_job(sent_id)
                peer = str((info.get('job_snapshot') or {}).get('encrypt_boss_id') or getattr(candidate, 'encrypt_boss_id', '') or '')
                if peer:
                    peer_counts[peer] = peer_counts.get(peer, 0) + 1
    except Exception as exc:  # 数据库读失败也是环境问题
        raise UpstreamError(f"读职位库失败：{humanize(exc)}") from exc

    try:
        client = create_client(auto_stoken=False)
        client_binding = getattr(client, 'account_binding', identity)
        if client_binding != identity:
            raise AccountChangedError('回读客户端账号与发送记录不一致')
        with identity.guard():
            pass
        me = request(client.fetch_me)
        if str(me.user_id) != identity.account_id:
            raise AccountChangedError('平台返回的账号与发送账号不一致，本次不保存回复')
    except JobError as exc:
        raise UpstreamError(humanize(exc)) from exc

    items: list[dict[str, Any]] = []
    for jid in ids:
        job = jobs.get(jid)
        rec: dict[str, Any] = {"encrypt_job_id": jid, "ok": False,
                               'sender_account_id': identity.account_id,
                               'application_id': selected[jid].get('application_id') or ''}
        if job is None:
            rec["error"] = "job_not_found"
            items.append(rec)
            continue
        encrypt_boss_id = str((selected[jid].get('job_snapshot') or {}).get('encrypt_boss_id') or getattr(job, "encrypt_boss_id", "") or "")
        if not encrypt_boss_id:
            rec["error"] = "no_encrypt_boss_id"
            items.append(rec)
            continue
        rec["company"] = str(getattr(job, "brand_name", "") or "")
        rec["title"] = str(getattr(job, "job_name", "") or "")
        rec["encrypt_boss_id"] = encrypt_boss_id
        rec['association_ambiguous'] = peer_counts.get(encrypt_boss_id, 0) > 1
        try:
            with identity.guard():
                pass
            boss = request(client.fetch_boss_data, encrypt_boss_id)
        except JobError as exc:
            # 最常见原因：只投递过但没建起会话；仍按“未知”返回
            rec["error"] = "boss_data_failed"
            rec["message"] = humanize(exc)[:200]
            items.append(rec)
            continue
        rec["boss_uid"] = int(boss.uid)
        rec["boss_name"] = str(boss.name or "")
        try:
            with identity.guard():
                pass
            raw_messages = request(client.fetch_chat_history, boss.uid)
        except JobError as exc:
            rec["error"] = "history_failed"
            rec["message"] = humanize(exc)[:200]
            items.append(rec)
            continue
        picked = [_pick_message(m) for m in raw_messages if isinstance(m, dict)]
        picked.sort(key=lambda m: m["time"])
        if body.debug:
            # 只回键名与条数，用于确认站点返回结构；不带会话内容
            try:
                raw = request(client._get_json_with_stoken_retry,
                    client.endpoints["chat_history"], params={"bossId": boss.uid},
                    action="回读聊天记录(探针)")
                zp = raw.get("zpData")
            except JobError as exc:
                rec["debug"] = {"error": humanize(exc)[:120]}
            else:
                rec["debug"] = {
                    "zpData_type": type(zp).__name__,
                    "zpData_keys": sorted(zp.keys())[:20] if isinstance(zp, dict) else [],
                    "code": raw.get("code"),
                    "message": str(raw.get("message") or "")[:80],
                }
        rec["ok"] = True
        rec["message_count"] = len(picked)
        rec["messages"] = picked[-body.limit:]
        items.append(rec)

    with identity.guard():
        pass
    return {"ok": True, "my_uid": int(me.user_id), 'sender_account_id': identity.account_id, "items": items}
