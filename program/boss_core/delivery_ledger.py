"""One append lock for the workflow and panel send audit."""
import datetime as dt
import json
from pathlib import Path


def append(path, event, job_id='', **fields):
    from boss_jobs.request_control import RequestControl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {'at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'event': event, 'job_id': job_id, **fields}
    with RequestControl(path.parent / 'ledger-control').locked():
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def unresolved_jobs(events, own_applications=()):
    from .feedback import application_key
    states = {}
    for row in events:
        key = application_key(row)
        kind = row.get('event')
        if not row.get('job_id') or kind not in {'reserved', 'sending', 'sent', 'failed', 'unknown', 'skipped'}:
            continue
        if states.get(key, {}).get('event') != 'sent':
            states[key] = row
    return {row['job_id'] for key, row in states.items()
            if key not in own_applications and row.get('event') in {'reserved', 'sending', 'unknown'}}
