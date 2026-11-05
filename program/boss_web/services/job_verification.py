"""Local send checks and an explicit, paced detail refresh with its own pause scope."""
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import math
import time

import boss_db
from boss_core.conditions import judge
from boss_jobs.detail_facts import extract
from boss_jobs.errors import JobApiError

_last_request = {}
_clock = time.monotonic


def local_reason(job, analysed):
    """Validate collected/scored facts locally; sending adds no detail request."""
    sha = (analysed or {}).get('source_jd_sha')
    if sha and hashlib.sha256(str(job.job_desc or '').strip().encode()).hexdigest()[:16] != sha:
        return '本地岗位全文与评分依据不一致，请重新评分；该岗位未发送'
    labels = {'job_name': '岗位名称', 'salary_desc': '薪资', 'brand_name': '公司'}
    for field, label in labels.items():
        previous = (analysed or {}).get(field)
        current = getattr(job, field, '')
        if previous and current and str(previous).strip() != str(current).strip():
            return f'本地{label}与审核资料不同，请重新评分/审核；该岗位未发送'
    return ''


@contextmanager
def _manual_request(snapshot):
    from . import delivery_control, feedback_reader
    from ..errors import ConflictError
    # Share only the optional-reader mutex; each reader keeps its own pause state.
    if not feedback_reader.optional_read_lock.acquire(blocking=False):
        raise ConflictError('回复回读或岗位核验正在进行，本次核验延后')
    try:
        control = delivery_control.state()
        if control.get('verification_paused') or control.get('paused'):
            raise ConflictError('岗位核验已暂停：' + str(control.get('verification_reason') or control.get('reason') or '请先处理平台提示'))
        feedback_reader.ensure_idle()
        from ..api.workflow import workflow_dir, _collector
        root = workflow_dir()
        if root is not None and _collector(root).get('running'):
            raise ConflictError('常驻采集器正在运行，可选岗位核验延后；优先沿用采集链更新资料')
        gap = max(5., float((snapshot.get('search') or {}).get('jd_interval') or 5))
        key = str(delivery_control._path())
        now = _clock()
        remaining = gap - (now - _last_request.get(key, -float('inf')))
        if remaining > 0:
            raise ConflictError(f'岗位核验间隔不足，请至少 {math.ceil(remaining)} 秒后再试',
                                detail={'retry_after_seconds': math.ceil(remaining)})
        _last_request[key] = now
        try:
            yield
        except JobApiError as exc:
            if exc.is_browser_check or exc.is_risk_control or exc.is_session_expired or exc.is_chat_rate_limited:
                delivery_control.set_paused(True, str(exc), scope='verification')
            # Real account/login/rate restrictions are distinct from an optional detail failure.
            if exc.is_risk_control or exc.is_session_expired or exc.is_chat_rate_limited:
                delivery_control.set_paused(True, str(exc), scope='send')
            raise
    finally:
        feedback_reader.optional_read_lock.release()


def verify(client, store, job, snapshot, *, analysed=None) -> tuple[str, dict]:
    """Explicit, rate-limited detail refresh. Never called from the send loop."""
    with _manual_request(snapshot):
        return _verify(client, store, job, snapshot, analysed=analysed)


def _verify(client, store, job, snapshot, *, analysed=None):
    jid = job.encrypt_job_id
    prior = saved(jid)
    detail = client.fetch_job_detail(security_id=job.security_id, lid=job.lid)
    facts = extract(detail.raw, jid, require_identity=True)
    if not facts.get('salary_desc') or not facts.get('job_name') or not facts.get('job_desc'):
        return '最新岗位的名称、薪资或全文不完整，留待核实；未发送', {}
    current = {**asdict(job), **facts}
    data = detail.raw.get('zpData') or detail.raw.get('data') or {}
    latest_boss = str((data.get('bossInfo') or {}).get('encryptBossId') or '')
    recipient_changed = bool(latest_boss and job.encrypt_boss_id and latest_boss != job.encrypt_boss_id)
    verdict = judge(current, snapshot)
    store.update_detail_facts(jid, detail.raw)
    store.update_job_desc(jid, facts['job_desc'],
                          fetched_at=time.strftime('%Y-%m-%d %H:%M:%S'))
    baseline = {**asdict(job), **{k: v for k, v in (analysed or {}).items()
                                if k in ('job_name', 'salary_desc', 'brand_name') and v}}
    changed = [key for key in ('job_name', 'salary_desc', 'brand_name', 'job_desc')
               if key in facts and str(facts[key]).strip() != str(baseline.get(key) or '').strip()]
    scored_sha = (analysed or {}).get('source_jd_sha')
    if scored_sha and hashlib.sha256(facts['job_desc'].strip().encode('utf-8')).hexdigest()[:16] != scored_sha and 'job_desc' not in changed:
        changed.append('job_desc')
    reason = ''
    if (prior.get('conflict') or {}).get('active'):
        reason = '用户已报告平台与本地岗位信息不一致，待核实处理；未发送'
    elif recipient_changed:
        reason = '最新详情中的招聘者与本地发送对象不一致，需重新采集并审核；未发送'
    elif verdict['blocking']:
        failures = [item['rule'] for item in verdict['checks'] if item['state'] == 'fail']
        reason = '最新岗位不符合当前硬条件：' + '；'.join(failures)
    elif changed:
        labels = {'job_name': '岗位名称', 'salary_desc': '薪资', 'brand_name': '公司', 'job_desc': '岗位全文'}
        reason = '岗位信息与审核时不同（' + '、'.join(labels[k] for k in changed) + '），请刷新并重新评分/审核；未发送'
    elif any(item['id'] == 'employment' and item['state'] == 'unknown' for item in verdict['checks']):
        reason = '最新岗位的实习/全职性质不明确，留待核实；未发送'
    record = {'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'job_id': jid,
              'source': 'platform_job_detail', 'facts': facts, 'changed': changed,
              'recipient_changed': recipient_changed,
              'state': 'blocked' if reason else 'verified', 'reason': reason}
    if prior.get('conflict'):
        record['conflict'] = prior['conflict']
    boss_db.doc_set('job_verification:' + jid, record)
    return reason, current


def saved(job_id):
    return boss_db.doc_get('job_verification:' + job_id) or {}


def flag_conflict(job_id, reason, *, active=True):
    record = saved(job_id)
    record['conflict'] = {'active': active, 'reason': reason,
                          'at': time.strftime('%Y-%m-%d %H:%M:%S')}
    boss_db.doc_set('job_verification:' + job_id, record)
    return record
