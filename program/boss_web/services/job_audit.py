"""Explain collected facts and their age locally; never refresh the platform."""
from dataclasses import asdict
import hashlib
import json

from boss_core.conditions import judge
from .job_verification import saved


def audit(store, job, snapshot):
    try:
        raw = json.loads(job.raw_json or '{}')
    except (ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    timing = store.job_timing(job.encrypt_job_id)
    verification = saved(job.encrypt_job_id)
    facts = {key: getattr(job, key) for key in
             ('job_name', 'brand_name', 'salary_desc', 'job_type', 'city_name')}
    return {
        'job_id': job.encrypt_job_id,
        'first_seen_at': timing.get('first_seen_at') or None,
        'current': facts,
        'listing': {
            'seen_at': timing.get('fetched_at') or None,
            'job_id': raw.get('encryptJobId') or None,
            'job_name': raw.get('jobName') or None,
            'salary_desc': raw.get('salaryDesc') or None,
        },
        'jd': {
            'saved_at': job.detail_fetched_at or None,
            'first_saved_at': timing.get('jd_first_at') or None,
            'sha': hashlib.sha256(job.job_desc.strip().encode()).hexdigest()[:16] if job.job_desc else None,
        },
        'last_verification': {
            'checked_at': verification.get('checked_at'),
            'salary_desc': (verification.get('facts') or {}).get('salary_desc'),
            'state': verification.get('state'),
        },
        'information_conflict': bool((verification.get('conflict') or {}).get('active')),
        'conflict_reason': (verification.get('conflict') or {}).get('reason') or '',
        'current_conditions': judge(asdict(job), snapshot) if snapshot else None,
        'scope_note': '仅检查本地存档和当前硬条件，未访问BOSS。列表和全文分别保存，时间可能不同；旧记录缺失的时间与发送时资料不能补造。',
    }
