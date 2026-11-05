"""Keep send restrictions and optional verification restrictions in separate scopes."""
import json
import os
from pathlib import Path
import threading
import time

_LOCK = threading.Lock()


def _path():
    root = os.environ.get('BOSS_WORKFLOW_DIR')
    if not root:
        from ..runtime import app_data_dir
        root = app_data_dir()
    return Path(root) / 'greeting' / 'delivery-control.json'


def state():
    path = _path()
    if not path.exists():
        return {'paused': False}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('paused'), bool):
            raise ValueError('invalid control')
        return data
    except (OSError, ValueError):
        return {'paused': True, 'reason': '发送暂停记录无法读取，请先检查，未继续发送'}


def set_paused(paused, reason='', *, scope='send'):
    if scope not in {'send', 'verification'}:
        raise ValueError('未知暂停范围')
    with _LOCK:
        data = state()
        if scope == 'verification':
            data.update(verification_paused=bool(paused), verification_reason=str(reason))
        else:
            data.update(paused=bool(paused), reason=str(reason))
        data['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
    return data


def ensure_available():
    from ..errors import ConflictError
    control = state()
    if control.get('paused'):
        raise ConflictError('发送已暂停：' + str(control.get('reason') or '请先处理平台提示，再明确恢复'))
