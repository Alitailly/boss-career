"""Reconstruct actual send attempts from the workflow ledger, including older runs."""
import json
from collections import deque
from boss_core.feedback import application_key


def delivery_history(path, limit=100, analysis_loader=None):
    drafts, pending, attempts = {}, {}, deque(maxlen=limit)
    generated = deque(maxlen=limit)
    settled = {}  # job_id -> 那次尝试的行，用来把「事后对账」折回同一条，而不是多出一条
    if not path.exists():
        return {'items': [], 'drafts': [], 'errors': 0}
    errors = 0
    with path.open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('not object')
            except ValueError:
                errors += 1
                continue
            jid, kind = event.get('job_id'), event.get('event')
            key = application_key(event)
            if not jid:
                continue
            if kind in {'validated', 'revalidated'} and event.get('text'):
                drafts[jid] = event
                generated.append(dict(job_id=jid, text=event['text'], at=event.get('at'),
                    analysis_id=event.get('analysis_id') or '', company=event.get('company') or '',
                    title=event.get('title') or '', model=event.get('model') or '',
                    analysis=event.get('analysis_snapshot'), analysis_source='写稿时快照' if event.get('analysis_snapshot') else '未记录'))
            if kind == 'sending':
                if event.get('application_id') and (key in pending or key in settled):
                    continue  # Same application intent may be acknowledged by the panel.
                draft = drafts.get(jid, {})
                row = {k: event.get(k) or draft.get(k) or '' for k in
                       ('text', 'company', 'title', 'analysis_id', 'writer', 'model', 'profile_version')}
                row.update(job_id=jid, status='sending', at=event.get('at'), batch=event.get('batch'),
                           application_id=event.get('application_id') or '',
                           sender_account_id=event.get('sender_account_id') or '',
                           task_id='', reason='', channel='工作流统一发送', attempt_id=number)
                row['analysis_id'] = row['analysis_id'] or event.get('batch') or ''
                row['generated_text'] = draft.get('text') or ''
                row['job_snapshot'] = event.get('job_snapshot') or None
                row['analysis'] = event.get('analysis_snapshot') or draft.get('analysis_snapshot') or None
                row['analysis_source'] = '发送时快照' if event.get('analysis_snapshot') else '写稿时快照' if row['analysis'] else '未记录'
                attempts.append(row)
                pending[key] = row
                settled.pop(key, None)
            elif kind in {'sent', 'failed', 'unknown', 'skipped'}:
                row = pending.pop(key, None)
                if row is None:
                    prior = settled.get(key)
                    if event.get('application_id') and prior is not None and prior.get('status') == 'sent':
                        continue  # A lost outer response must not erase a confirmed send.
                    if event.get('application_id') and prior is not None and prior.get('status') == kind:
                        continue
                    if prior is not None and prior.get('status') == 'unknown' and event.get('reconciled'):
                        # 事后对账是「同一次尝试的结论」，不是新的一次发送；
                        # 时间保留当时的观察时间，依据写进说明。
                        note = str(event.get('reason') or '')
                        evidence = str(event.get('evidence') or '')
                        prior.update(status=kind,
                                     reason=note + (f'（依据：{evidence}）' if evidence else ''))
                        continue
                    # Historical records without an intent cannot prove the exact body.
                    row = dict(job_id=jid, text='', company='', title='', writer='', model='',
                               application_id=event.get('application_id') or '',
                               sender_account_id=event.get('sender_account_id') or '',
                               channel='工作流历史记录', attempt_id=number)
                    attempts.append(row)
                row.update(status=kind, at=event.get('at'), task_id=event.get('task_id') or '',
                           reason=str(event.get('reason') or event.get('error') or ''))
                settled[key] = row
    cache = {}
    items = list(reversed(attempts))
    draft_items = list(reversed(generated))
    for row in items + draft_items:
        aid = row.get('analysis_id')
        if row.get('analysis') or not aid or analysis_loader is None:
            continue
        try:
            if aid not in cache:
                cache[aid] = analysis_loader(aid)
            item = next((m for m in cache[aid].get('matches', []) if m.get('encrypt_job_id') == row['job_id']), None)
            if item:
                row['analysis'] = {k: item.get(k) for k in ('match_score', 'verdict', 'matched_skills', 'missing_skills', 'pros', 'cons', 'hard_constraints')}
                row['analysis_source'] = '关联分析当前存档（不保证与发送当时相同）'
                row['company'] = row.get('company') or item.get('brand_name') or ''
                row['title'] = row.get('title') or item.get('job_name') or ''
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            row['analysis_source'] = '关联分析不可用'
    return {'items': items, 'drafts': draft_items, 'errors': errors}
