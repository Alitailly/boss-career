"""Join existing local send, JD and reply records for later review. No BOSS I/O."""
from boss_core.feedback import fold_feedback, load_events, sent_jobs, application_key
from .workflow_history import delivery_history


def records(root):
    queue = root / 'greeting/queue.jsonl'
    sent = sent_jobs(load_events(queue))
    feedback, _ = fold_feedback(load_events(root / 'greeting/feedback.jsonl'))
    history = delivery_history(queue, limit=None)
    confirmed = {}
    for row in reversed(history['items']):
        if row.get('status') == 'sent':
            confirmed.setdefault(application_key(row), row)
    from boss_jobs.store import JobStore
    out = []
    with JobStore() as store:
        for key, info in sent.items():
            jid = info['job_id']
            event = confirmed.get(key, {})
            snapshot = event.get('job_snapshot') or {}
            job = store.get_job(jid)
            fb = feedback.get(key, {})
            account = info.get('sender_account_id') or ''
            if str(fb.get('sender_account_id') or '') != account or (fb.get('reader_account_id') and str(fb['reader_account_id']) != account):
                fb = {}
            jd = str(snapshot.get('jd_text') or '')
            local_jd = str(getattr(job, 'job_desc', '') or '')
            from .job_verification import saved
            verification = saved(jid)
            out.append({
                'record_id': key, 'application_id': info.get('application_id') or '',
                'sender_account_id': account, 'account_label': account or '历史账号待确认',
                'job_id': jid, 'company': event.get('company') or snapshot.get('company') or getattr(job, 'brand_name', '') or '公司未记录',
                'title': event.get('title') or snapshot.get('title') or getattr(job, 'job_name', '') or '岗位未记录',
                'at_sent': info['at_sent'], 'greeting': event.get('text') or '',
                'greeting_source': '实际发送记录' if event.get('text') else '历史记录未保存实际正文，不能用新草稿替代',
                'jd_text': jd or local_jd,
                'jd_source': '发送时保存的岗位全文' if jd else '当前本地岗位全文，发送当时的版本未记录' if local_jd else '没有保存岗位全文',
                'profile_version': event.get('profile_version') or '',
                'status': 'reply' if fb.get('replied') else 'none' if fb.get('kind') == 'none' else 'unknown',
                'checked_at': fb.get('checked_at') or '', 'first_reply_at': fb.get('first_reply_at') or '',
                'category': fb.get('category') or '', 'messages': fb.get('messages') or [],
                'legacy_excerpt': fb.get('evidence') or '', 'reason': fb.get('reason') or ('' if account else '历史发送账号待确认，不自动回读或重发'),
                'association_note': '回复通过该岗位的招聘者会话关联；同一招聘者涉及多个岗位时，需要结合消息确认归属。',
                'information_conflict': bool((verification.get('conflict') or {}).get('active')),
                'verification': verification,
            })
    return sorted(out, key=lambda row: (row['status'] == 'reply', row['first_reply_at'] or row['at_sent']), reverse=True)


def list_records(root, *, offset=0, limit=20, status='all'):
    rows = [row for row in records(root) if status == 'all' or row['status'] == status]
    fields = ('record_id', 'application_id', 'sender_account_id', 'account_label', 'job_id', 'company', 'title', 'at_sent', 'status', 'checked_at', 'first_reply_at', 'category', 'reason', 'information_conflict')
    return {'ok': True, 'total': len(rows), 'offset': offset, 'limit': limit,
            'items': [{key: row[key] for key in fields} for row in rows[offset:offset + limit]]}


def detail(root, jid):
    rows = records(root)
    exact = next((row for row in rows if row['record_id'] == jid), None)
    if exact:
        return exact
    matches = [row for row in rows if row['job_id'] == jid]
    if len(matches) > 1:
        from ..errors import ConflictError
        raise ConflictError('该岗位有多条账号投递记录，请使用 record_id 查看指定记录')
    return matches[0] if matches else None
