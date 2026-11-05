"""逐条写稿输入输出的只读回溯；历史缺项不借用当前配置冒充。"""
from __future__ import annotations
import json
import hashlib
from pathlib import Path
from typing import Any


def read(root: Path, job_id: str, *, analysis_id: str = '', text_sha: str = '') -> dict[str, Any]:
    from . import workflow_context as wctx
    from .resume_store import load_analysis
    from boss_jobs.store import JobStore
    latest, trace_event = {}, {}
    try:
        with (root/'greeting/queue.jsonl').open(encoding='utf-8-sig') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or str(event.get('job_id') or '') != job_id:
                    continue
                if isinstance(event.get('text'), str) and event['text'].strip() and event.get('event') in {
                    'validated','revalidated','rejected','needs_review','sent','manual_approved'}:
                    if text_sha and hashlib.sha256(event['text'].encode('utf-8')).hexdigest()[:16] != text_sha:
                        continue
                    if event.get('text') != latest.get('text'):
                        trace_event = {}
                        latest = {}
                    latest = {**latest, **event}
                    if event.get('writer_input_trace'):
                        trace_event = event
    except FileNotFoundError:
        pass
    trace, trace_note = {}, '本条没有保存可验证的写稿输入，不能用当前规则或当前简历代替。'
    relative = trace_event.get('writer_input_trace')
    if isinstance(relative, str) and relative:
        target = (root/'greeting'/relative).resolve()
        allowed = (root/'greeting/writer/runs').resolve()
        if target.is_relative_to(allowed) and target.is_file():
            try:
                trace = json.loads(target.read_text(encoding='utf-8'))
                actual_sha = hashlib.sha256(json.dumps(trace,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
                if trace_event.get('writer_input_sha') != actual_sha:
                    raise ValueError('输入留存指纹不符')
                if not isinstance(trace, dict):
                    raise ValueError('写稿输入文件格式错误')
                trace_note = '本条写稿时留存的实际输入；不随当前规则变化。'
            except (OSError, ValueError):
                trace = {}
                trace_note = '本条写稿输入文件读取失败，暂不能确认实际输入。'
    request = trace.get('request') if isinstance(trace.get('request'), dict) else {}
    jobs = request.get('jobs') if isinstance(request.get('jobs'), list) else []
    job_input = next((j for j in jobs if isinstance(j, dict) and str(j.get('job_id')) == job_id), None)
    messages = trace.get('messages') if isinstance(trace.get('messages'), list) else []
    recorded = bool(job_input and messages and isinstance(messages[0], dict) and isinstance(messages[0].get('content'), str))
    facts = [f for f in request.get('facts', []) if isinstance(f, dict)] if isinstance(request.get('facts'), list) else []
    used = trace_event.get('used_fact_ids') or []
    actual_analysis = latest.get('analysis_snapshot') or {}
    saved_analysis, scored_resume = {}, ''
    analysis_note = '写稿时保存的评分快照。' if actual_analysis else '未找到关联评分记录。'
    aid = str(latest.get('analysis_id') or analysis_id or '')
    if aid:
        try:
            data = load_analysis(aid)
            scored_resume = data.get('resume_title') or data.get('resume_id') or ''
            saved_analysis = next((m for m in data.get('matches', []) if str(m.get('encrypt_job_id')) == job_id), {})
            analysis_note = '写稿时保存的评分快照。' if actual_analysis else '当前关联评分记录；可能已重新评分，不能视为当时实际写稿输入。'
        except FileNotFoundError:
            analysis_note = '关联分析已不存在；仍展示台账中的留存快照。'
    with JobStore() as store:
        job = store.get_job(job_id)
    rules = wctx.greeting_rules() or {}
    return {'ok': True, 'job_id': job_id,
        'job': {'title': getattr(job,'job_name','') or latest.get('title'),
                'company': getattr(job,'brand_name','') or latest.get('company'),
                'jd': getattr(job,'job_desc',''), 'jd_source': '本地当前岗位全文，可能与写稿时不同'},
        'output': {'text': latest.get('text') or '', 'at': latest.get('at'), 'event': latest.get('event'),
                   'used_fact_ids': used},
        'inputs': {'recorded': recorded, 'note': trace_note, 'facts': facts if recorded else [],
            'used_facts': [f for f in facts if f.get('id') in used] if recorded else [],
            'job': job_input if recorded else {},
            'rules': (trace.get('messages') or [{}])[0].get('content','') if recorded else ''},
        'analysis': actual_analysis or saved_analysis, 'analysis_note': analysis_note,
        'scored_resume': scored_resume, 'analysis_id': aid,
        'current_rules': rules.get('text') or '',
        'current_note': '当前写作规则用于后续生成，不代表本条成稿当时采用。'}
