#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HR 反馈采集：回读已发送会话，只写 `feedback.jsonl`，不改发送状态。

设计约束（审查报告 R3/R4/R5）：

- 反馈与投递**分开存**：本脚本永不往 `greeting/queue.jsonl` 写事件，
  因此 sent/unknown 不会被 reply 覆盖；
- 读不到就是「未知」：接口失败、没建会话、字段识别不了，都不能算「HR 未回复」；
- 每条都带观察窗口与覆盖率；当日回复率只是快照，不做“今天回复少就改策略”；
- 同一会话同一批消息按 `job_id|first_reply_at|count` 去重，重复跑不会双算。

用法：
    python feedback_sync.py                # 增量同步，最多 20 个会话
    python feedback_sync.py --dry-run      # 只打印要同步什么，不发请求
    python feedback_sync.py --max-jobs 5 --report-only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

from workflow_env import api_base, workspace_root, ensure_core_path

ensure_core_path()
from boss_core import feedback as feedback_stats

BASE = api_base()
HERE = workspace_root()
QUEUE = HERE / "greeting" / "queue.jsonl"
FEEDBACK = HERE / "greeting" / "feedback.jsonl"
STATE = HERE / "feedback" / "last-sync.json"
REPORTS = HERE / "feedback"
RUNS = HERE / "runs"

# 观察窗口：发送后多久算“该有回音了”。是可配置示例，不是已验证的最优值。
WINDOWS_HOURS = (24, 72, 168)
# 同一条消息的稳定程度有限，先去重再谈比例
MIN_AGE_HOURS = 1.0
RESYNC_HOURS = 6.0

CATEGORIES = (
    ("ask_resume", ("简历", "附件", "作品集")),
    ("interview", ("面试", "约面", "面谈", "视频面", "线上面")),
    ("chat", ("电话", "微信", "聊聊", "沟通", "方便")),
    ("reject", ("不合适", "不匹配", "已招满", "暂不需要", "谢谢关注")),
)


def now_dt() -> dt.datetime:
    return dt.datetime.now()


def parse_time(value: str) -> dt.datetime | None:
    """时间字段可能是 ISO 串，也可能是 epoch 秒（浮点）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromtimestamp(float(text))
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except (TypeError, ValueError):
            continue
    return None


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # 反馈文件坏了不影响发送；下面按“未知”处理
        if isinstance(rec, dict):
            out.append(rec)
    return out


def append_feedback(**rec) -> None:
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": now_dt().strftime("%Y-%m-%d %H:%M:%S"), **rec}
    with FEEDBACK.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def sent_jobs() -> dict[str, dict]:
    """从投递台账里取「已发送」的岗位，带发送时间与正文哈希。"""
    return feedback_stats.sent_jobs(load_events(QUEUE))


def prior_feedback() -> tuple[dict[str, dict], set[str]]:
    """已有反馈：按 job 折叠，外加去重键集合。"""
    return feedback_stats.fold_feedback(load_events(FEEDBACK))


def current_account() -> str:
    response = requests.get(f'{BASE}/api/auth/account', timeout=20)
    response.raise_for_status()
    return str(response.json().get('sender_account_id') or '')


def api_chat(ids: list[str], *, account_id='', application_ids=None) -> dict:
    resp = requests.post(f"{BASE}/api/feedback/chat",
                         json={"encrypt_job_ids": ids, "limit": 50,
                               'sender_account_id': account_id, 'application_ids': application_ids or {}}, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"反馈接口 {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def classify(messages: list[dict], my_uid: int) -> dict:
    """挑出对方发来的消息，给一个带不确定性的分类。"""
    hr = [m for m in messages if m.get("from_uid") and int(m["from_uid"]) != int(my_uid)]
    if not hr:
        return {"replied": False, "count": 0, "first_reply_at": "", "category": "",
                "evidence": ""}
    hr.sort(key=lambda m: m.get("time") or 0)
    first = hr[0]
    text = " ".join(str(m.get("text") or "") for m in hr)[:200]
    category = "other"
    for name, words in CATEGORIES:
        if any(w in text for w in words):
            category = name
            break
    return {
        "replied": True,
        "count": len(hr),
        "first_reply_at": dr_from_ms(first.get("time")) or "",
        "category": category,
        "evidence": text[:60],
        "messages": [{k: message.get(k) for k in ('mid', 'from_uid', 'to_uid', 'time', 'type', 'text')} for message in hr],
    }


def dr_from_ms(value) -> str:
    stamp = feedback_stats.parse_time(value)
    return stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp else ""


def judge(item: dict, my_uid: int, sent_at: str = "") -> tuple[str, dict]:
    """D1：把一条会话判成 reply / none / unknown，绝不把「读不到」当成「没回复」。

    - 读取失败 → unknown
    - 读到了消息、里面有 HR 的 → reply
    - 读到了消息、全是自己发的 → none（附实际 message_count）
    - 空响应（一条消息都没有）→ unknown：通道没给资料，不能当成 HR 未回复
    """
    if not item.get("ok"):
        return "unknown", {"reason": item.get("error") or "读取失败",
                          "message": str(item.get("message") or "")[:120]}
    if not my_uid:
        return "unknown", {"reason": "无法确认当前用户身份"}
    messages = item.get("messages") or []
    sent_time = feedback_stats.parse_time(sent_at)
    if sent_time:
        messages = [m for m in messages if (stamp := feedback_stats.parse_time(m.get('time'))) and stamp >= sent_time]
    if any(not str(m.get('from_uid') or '').isdigit() or int(m.get('from_uid') or 0) <= 0 for m in messages):
        return "unknown", {"reason": "消息发送者无法识别", "message_count": len(messages)}
    info = classify(messages, my_uid)
    count = len(messages)
    if item.get('association_ambiguous') and info['replied']:
        return 'unknown', {'reason': '同一招聘者对应多个已投岗位，回复归属待确认',
                           'messages': info.get('messages') or [], 'message_count': count}
    if info["replied"]:
        return "reply", {**info, "message_count": count}
    if int(item.get('message_count') or 0) > len(item.get('messages') or []):
        return 'unknown', {'reason': '消息有截断，尚不能确认没有回复', 'message_count': count}
    if count > 0:
        return "none", {"count": 0, "message_count": count}
    return "unknown", {"reason": "空响应：没读到任何消息，通道未验证", "message_count": 0}


def sync(max_jobs: int, dry_run: bool) -> dict:
    sent = sent_jobs()
    prior, keys = prior_feedback()
    now = now_dt()
    from boss_login.account import assert_task_environment
    assert_task_environment()
    account_id = current_account()
    if not account_id:
        return {'checked': 0, 'replied': 0, 'none': 0, 'unknown': 0,
                'endpoint_verified': False, 'errors': [], 'remaining': 0,
                'stop_reason': '无法确认当前账号，本次未回读平台'}
    todo: list[str] = []
    for jid, info in sent.items():
        if str(info.get('sender_account_id') or '') != account_id:
            continue
        ts = parse_time(info["at_sent"])
        if not ts:
            continue
        age_h = (now - ts).total_seconds() / 3600
        if age_h < MIN_AGE_HOURS:
            continue
        last = parse_time(prior.get(jid, {}).get("checked_at") or "")
        if last and (now - last).total_seconds() / 3600 < RESYNC_HOURS:
            continue
        todo.append(jid)
    # Never-read conversations first, then the least recently checked. Sorting
    # only by send date repeatedly selects the same old 20 and starves the rest.
    todo.sort(key=lambda j: (str(prior.get(j, {}).get('checked_at') or ''), str(sent[j]['at_sent'])))
    remaining = max(0, len(todo) - max_jobs)
    todo = todo[:max_jobs]

    result = {"checked": 0, "replied": 0, "none": 0, "unknown": 0,
              "endpoint_verified": False, "errors": [], "remaining": remaining, "stop_reason": "",
              'sender_account_id': account_id,
              'other_account_sent': sum(bool(info.get('sender_account_id')) and str(info['sender_account_id']) != account_id for info in sent.values()),
              'unassigned_sent': sum(not info.get('sender_account_id') for info in sent.values())}
    if dry_run:
        print(f"[dry-run] 待同步 {len(todo)} 个会话：{', '.join(todo[:5])}")
        return result
    if not todo:
        return result

    # One conversation per request: the API paces real upstream calls. This also
    # bounds an in-flight read and stops before the next chat on a channel error.
    for start in range(0, len(todo)):
        chunk = todo[start:start + 1]
        job_ids = [sent[key]['job_id'] for key in chunk]
        applications = {sent[key]['job_id']: sent[key].get('application_id') or '' for key in chunk}
        def save(kind, key, **fields):
            append_feedback(kind=kind, job_id=sent[key]['job_id'],
                            application_id=sent[key].get('application_id') or '',
                            sender_account_id=account_id, reader_account_id=account_id, **fields)
        try:
            payload = api_chat(job_ids, account_id=account_id, application_ids=applications)
            if str(payload.get('my_uid') or '') != account_id:
                raise RuntimeError('返回账号与原发送账号不一致，未采用本次会话内容')
        except Exception as exc:
            for jid in chunk:
                save('unknown', jid, reason=f"接口失败：{str(exc)[:120]}",
                                checked_at=now.strftime("%Y-%m-%d %H:%M:%S"))
                result["unknown"] += 1
                result["errors"].append(str(exc)[:120])
            result['stop_reason'] = '反馈读取失败，已停止本次回读；请先检查登录或平台限制'
            result['remaining'] += len(todo) - start - len(chunk)
            break
        my_uid = int(payload.get("my_uid") or 0)
        chunk_verified = False
        by_id = {str(item.get('encrypt_job_id') or ''): item for item in payload.get('items') or []}
        for jid in chunk:
            item = by_id.get(sent[jid]['job_id']) or {'ok': False, 'error': '响应缺少该会话'}
            if item.get('sender_account_id') != account_id or item.get('application_id', '') != applications[sent[jid]['job_id']]:
                item = {'ok': False, 'error': '响应投递归属与本次请求不一致，未采用消息'}
            checked = now.strftime("%Y-%m-%d %H:%M:%S")
            kind, detail = judge(item, my_uid, sent[jid]['at_sent'])
            if kind == "unknown":
                save('unknown', jid, checked_at=checked,
                                **{k: v for k, v in detail.items() if k != "message_count"},
                                message_count=int(detail.get("message_count") or 0))
                result["unknown"] += 1
                continue
            result["checked"] += 1
            chunk_verified = True
            result['endpoint_verified'] = True
            if kind == "reply":
                key = f"{jid}|{detail['first_reply_at']}|{detail['count']}"
                if key in keys:
                    # Even unchanged history was checked now. Without this
                    # event the old timestamp causes repeated reads next run.
                    known_messages = prior.get(jid, {}).get('messages') or []
                    save('checked', jid, checked_at=checked,
                                    messages=[message for message in detail.get('messages') or [] if message not in known_messages])
                    continue
                keys.add(key)
                save('reply', jid, dedupe_key=key,
                                replied=True,
                                first_reply_at=detail["first_reply_at"], count=detail["count"],
                                category=detail["category"], evidence=detail["evidence"],
                                messages=detail.get('messages') or [],
                                message_count=int(detail.get("message_count") or 0),
                                checked_at=checked)
                result["replied"] += 1
            else:
                # D1：只有「会话可读且确实读到了消息、但没有 HR 消息」才记无回复
                save('none', jid, count=0,
                                message_count=int(detail.get("message_count") or 0),
                                checked_at=checked)
                result["none"] += 1
                result["endpoint_verified"] = True

        if not chunk_verified:
            result['stop_reason'] = '本批未读到可验证的消息，已暂停回读；空响应不等于未回复'
            result['remaining'] += len(todo) - start - len(chunk)
            break

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"at": now.strftime("%Y-%m-%d %H:%M:%S"), **result},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def cohort_report() -> dict:
    """按消息年龄窗口统计；只报计数与覆盖率，不下结论。"""
    sent = sent_jobs()
    per, _ = prior_feedback()
    return feedback_stats.report(sent, per, now_dt(), WINDOWS_HOURS)


def write_report(sync_result: dict, report: dict) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"日报-{dt.date.today().isoformat()}.md"
    snapshot = REPORTS / f"日报-{dt.date.today().isoformat()}.json"
    snapshot.write_text(json.dumps({**report, 'sync': sync_result}, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = [
        f"# 反馈日报 {dt.date.today().isoformat()}",
        "",
        f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 本次同步",
        "",
        "仅生成本地快照，本次未回读平台。" if sync_result.get('report_only') else '本次已按可执行范围尝试回读平台。',
        "",
        f"- 读取会话：{sync_result['checked']}；确认回复：{sync_result['replied']}；"
        f"读到但没有对方消息：{sync_result['none']}；未知：{sync_result['unknown']}",
        f"- 反馈通道是否返回过消息内容：{'是' if sync_result['endpoint_verified'] else '否'}"
        "（「否」时不能把 0 条当成 HR 未回复）",
        "",
        "## 按消息年龄的回复情况（快照，不是结论）",
        "",
        "| 已发送至少 | 发送记录 | 有回复 | 已读未回复 | 未知 | 读取覆盖率 | 回复率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["cohorts"]:
        rate = f"{row['reply_rate']:.1%}" if row['reply_rate'] is not None else '待完整读取'
        coverage = f"{row['coverage']:.1%}" if row['coverage'] is not None else '—'
        lines.append(f"| {row['window_hours']} 小时 | {row['sent']} | {row['replied']} | {row['none']} | "
                     f"{row['unknown']} | {coverage} | {rate} |")
    lines += [
        "",
        f"累计已发送：{report['sent_total']}；有反馈记录的投递：{report['feedback_events']}。",
        "",
        "口径说明：",
        "",
        "- 当日回复数只是快照，刚发出去和等了几天的不混在一起算；",
        "- 「反馈未知」= 接口失败/没建会话/字段识别不了，不等于「HR 未回复」；",
        "- 索要简历、约面、愿意聊、拒绝分别记，不把索要简历当成面试；",
        "- 样本小，不据此改开头、改长度或改求职方向。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-jobs", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    try:
        result = {"checked": 0, "replied": 0, "none": 0, "unknown": 0,
                  "endpoint_verified": False, "errors": []} if args.report_only \
            else sync(args.max_jobs, args.dry_run)
    except RuntimeError as exc:
        print(f"[7] {exc}")
        return 7
    report = cohort_report()
    result['report_only'] = args.report_only
    if not args.dry_run:
        path = write_report(result, report)
        print(f"日报：{path.name}")
    print(json.dumps({**result, "cohorts": report["cohorts"]}, ensure_ascii=False))
    return 7 if result.get('stop_reason') else 0


if __name__ == "__main__":
    sys.exit(main())
