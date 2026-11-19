"""One append lock for the workflow and panel send audit."""
from contextlib import suppress
import datetime as dt
import json
import os
from pathlib import Path
import tempfile


def append(path, event, job_id='', **fields):
    from boss_jobs.request_control import RequestControl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {'at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'event': event, 'job_id': job_id, **fields}
    with RequestControl(path.parent / 'ledger-control').locked():
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def append_greeting_batch(path, work_id, rows, *, allow_new):
    """用同一把台账锁追加完整批次；同一个工作项只追加一次。"""
    from boss_jobs.request_control import RequestControl

    work_key = work_id.encode('ascii')
    with RequestControl(path.parent / 'ledger-control').locked():
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            content = b''
        for line in content.splitlines():
            if b'"external_work_id"' in line and work_key in line:
                record = json.loads(line)
                if isinstance(record, dict) and record.get('external_work_id') == work_id:
                    return True  # 整批通过原子替换写进台账，看到一条就已全部写入。
        if not allow_new:
            return False

        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                if content and not content.endswith(b'\n'):
                    stream.write(b'\n')
                for row in rows:
                    line = json.dumps({**row, 'external_work_id': work_id}, ensure_ascii=False) + '\n'
                    stream.write(line.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            with suppress(OSError):
                Path(temp).unlink(missing_ok=True)
        return True


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
