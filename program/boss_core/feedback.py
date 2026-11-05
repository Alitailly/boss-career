"""Reply snapshots from separate send/feedback ledgers; no platform access."""
from __future__ import annotations

import datetime as dt
import json
import hashlib
from pathlib import Path


def parse_time(value) -> dt.datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        number = float(text)
        return dt.datetime.fromtimestamp(number / 1000 if number > 1e11 else number)
    except (ValueError, OSError, OverflowError):
        pass
    try:
        stamp = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
        return stamp.astimezone().replace(tzinfo=None) if stamp.tzinfo else stamp
    except ValueError:
        return None


def load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def application_key(event):
    if event.get('application_id'):
        return str(event['application_id'])
    jid, account = str(event.get('job_id') or ''), str(event.get('sender_account_id') or '')
    return 'account-' + hashlib.sha256((account + '|' + jid).encode()).hexdigest()[:24] if account else jid


def sent_jobs(events: list[dict]) -> dict[str, dict]:
    out, pending = {}, {}
    for event in events:
        jid = str(event.get('job_id') or '')
        if event.get('event') == 'sending' and jid:
            pending[application_key(event)] = event
        if event.get('event') != 'sent' or not jid:
            continue
        intent = pending.get(application_key(event), {})
        # A caller must not borrow another account's most recent intent.
        if (event.get('application_id') and event.get('application_id') != intent.get('application_id')) or (
                event.get('sender_account_id') and event.get('sender_account_id') != intent.get('sender_account_id')):
            intent = {}
        event = {**intent, **event}
        key = application_key(event)
        stamp = str(event.get('at_sent') or event.get('at') or '')
        # Reconciliation can append another sent event; preserve original cohort.
        old = parse_time(out.get(key, {}).get('at_sent'))
        current = parse_time(stamp)
        if key not in out or current and (old is None or current < old):
            out[key] = {**event, 'job_id': jid, 'at_sent': stamp,
                        'sender_account_id': str(event.get('sender_account_id') or ''),
                        'application_id': str(event.get('application_id') or '')}
    return out


def fold_feedback(events: list[dict]) -> tuple[dict[str, dict], set[str]]:
    per, keys = {}, set()
    for event in events:
        if not event.get('job_id'):
            continue
        if event.get('reader_account_id') and str(event['reader_account_id']) != str(event.get('sender_account_id') or ''):
            continue
        jid = application_key(event)
        if event.get('dedupe_key'):
            keys.add(str(event['dedupe_key']))
        previous = per.get(jid, {})
        replied = event.get('kind') == 'reply' or event.get('replied') is True
        # A later failed read cannot erase an already observed reply.
        if previous.get('replied') and not replied:
            per[jid] = {**previous, 'last_check_kind': event.get('kind'),
                        'checked_at': event.get('checked_at') or event.get('at') or previous.get('checked_at')}
        else:
            per[jid] = {**event, 'replied': replied,
                        'checked_at': event.get('checked_at') or event.get('at') or ''}
        merged = {}
        for message in [*(previous.get('messages') or []), *(event.get('messages') or [])]:
            if not isinstance(message, dict):
                continue
            key = str(message.get('mid') or '') or hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            merged[key] = message
        if merged:
            per[jid]['messages'] = list(merged.values())
        first = [str(value) for value in (previous.get('first_reply_at'), event.get('first_reply_at')) if value]
        if first:
            per[jid]['first_reply_at'] = min(first)
    return per, keys


def _counts(ids, per, now, window_hours=0):
    replied = none = unknown = fresh = 0
    for jid in ids:
        fb = per.get(jid, {})
        if fb.get('replied'):
            replied += 1
        elif fb.get('kind') == 'none':
            none += 1
        else:
            unknown += 1
        checked = parse_time(fb.get('checked_at'))
        if checked and (now - checked).total_seconds() <= 24 * 3600:
            fresh += 1
    sent = len(ids)
    known = replied + none
    return {'sent': sent, 'replied': replied, 'none': none, 'unknown': unknown,
            'known': known, 'coverage': round(known / sent, 4) if sent else None,
            'fresh_checked': fresh,
            # Only a fully observed denominator has an exact reply rate.
            'reply_rate': round(replied / sent, 4) if sent and not unknown else None,
            'reply_rate_lower_bound': round(replied / sent, 4) if sent else None,
            'known_reply_rate': round(replied / known, 4) if known else None}


def report(sent, per, now=None, windows=(24, 72, 168)) -> dict:
    now = now or dt.datetime.now()
    per = {key: fb for key, fb in per.items() if key in sent and
           str(fb.get('sender_account_id') or '') == str(sent[key].get('sender_account_id') or '') and
           (not fb.get('reader_account_id') or str(fb['reader_account_id']) == str(sent[key].get('sender_account_id') or ''))}
    cohorts = []
    for hours in windows:
        ids = [jid for jid, event in sent.items()
               if (stamp := parse_time(event.get('at_sent'))) and (now - stamp).total_seconds() >= hours * 3600]
        cohorts.append({'window_hours': hours, **_counts(ids, per, now)})
    dates = sorted({stamp.date().isoformat() for event in sent.values()
                    if (stamp := parse_time(event.get('at_sent')))}, reverse=True)
    daily = []
    for date in dates[:14]:
        ids = [jid for jid, event in sent.items()
               if (stamp := parse_time(event.get('at_sent'))) and stamp.date().isoformat() == date]
        daily.append({'sent_date': date, **_counts(ids, per, now)})
    reasons = {}
    for jid in sent:
        event = per.get(jid, {})
        if not event.get('replied') and event.get('kind') == 'unknown':
            reason = str(event.get('reason') or '读取失败')[:200]
            reasons[reason] = reasons.get(reason, 0) + 1
    accounts = sorted({str(event.get('sender_account_id') or '') for event in sent.values()})
    by_account = [{'sender_account_id': account, 'label': account or '历史账号待确认',
                   **_counts([key for key, event in sent.items() if str(event.get('sender_account_id') or '') == account], per, now)}
                  for account in accounts]
    return {'as_of': now.strftime('%Y-%m-%d %H:%M:%S'), 'cohorts': cohorts, 'daily': daily,
            'by_account': by_account, 'unassigned_sent': sum(not event.get('sender_account_id') for event in sent.values()),
            'unique_jobs': len({event.get('job_id') or key for key, event in sent.items()}),
            'sent_total': len(sent), 'feedback_events': len(set(sent) & set(per)),
            'overall': _counts(list(sent), per, now),
            'last_checked_at': max((str(per[jid].get('checked_at') or '') for jid in sent if jid in per), default=''),
            'unknown_reasons': [{'reason': key, 'count': value} for key, value in sorted(reasons.items(), key=lambda x: -x[1])],
            'source': 'greeting/queue.jsonl + greeting/feedback.jsonl',
            'scope': '按投递记录统计，区分发送账号；历史账号待确认的单列。回复按招聘者会话观察，非平台全量回复率'}


def read_report(root: Path) -> dict:
    root = Path(root)
    sent = sent_jobs(load_events(root / 'greeting/queue.jsonl'))
    per, _ = fold_feedback(load_events(root / 'greeting/feedback.jsonl'))
    result = report(sent, per)
    path = root / 'feedback/last-sync.json'
    try:
        last = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        last = {}
    result['last_sync'] = {k: last.get(k) for k in ('at', 'checked', 'replied', 'none', 'unknown', 'endpoint_verified', 'stop_reason', 'remaining')} if isinstance(last, dict) else {}
    return result
