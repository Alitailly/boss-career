"""Bounded conversation reads: one caller, paced requests, persistent pause."""
import datetime as dt
import json
import re
import threading
import time
from contextlib import contextmanager

from ..errors import ConflictError, UpstreamError

REQUEST_GAP_SECONDS = 5.0  # Conservative reference value, not a safety guarantee.
_lock = threading.Lock()
optional_read_lock = _lock
_last_request_at = 0.0
_restriction = re.compile(r'异常|频繁|验证码|验证|登录|限流|风控|\b(?:401|403|429)\b|too many|captcha|risk|unauthorized', re.I)


def root():
    from ..api.workflow import workflow_dir
    return workflow_dir()


def read_control():
    folder = root()
    if folder is None:
        return {}
    path = folder / 'feedback/read-control.json'
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict):
            raise ValueError('invalid control state')
        return result
    except (OSError, ValueError) as exc:
        raise ConflictError('回复回读的暂停记录无法读取，请先检查本地记录') from exc


def set_pause(paused, reason=''):
    folder = root()
    if folder is None:
        raise ConflictError('未配置工作区，无法保存回复回读状态')
    path = folder / 'feedback/read-control.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {'paused': paused, 'reason': reason, 'at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
    return state


def ensure_idle():
    from ..api.crawl import crawl_tasks
    from ..api.jobs import desc_tasks
    from ..api.deliver import deliver_tasks
    for manager in (crawl_tasks, desc_tasks, deliver_tasks):
        if manager.snapshot().get('status') in {'running', 'queued', 'starting'}:
            raise ConflictError('采集、补全文或发送正在运行，本次回复回读延后，避免叠加平台请求')


@contextmanager
def reading():
    if not _lock.acquire(blocking=False):
        raise ConflictError('已有回复回读或岗位核验在运行，请等待该任务结束')
    try:
        state = read_control()
        if state.get('paused'):
            raise ConflictError('回复回读已暂停，请先处理平台异常，再明确恢复：' + str(state.get('reason') or ''))
        ensure_idle()
        yield
    finally:
        _lock.release()


def request(function, *args, **kwargs):
    """Delay before each actual platform request, rather than after processing."""
    from boss_jobs.errors import JobError
    global _last_request_at
    ensure_idle()
    gap = REQUEST_GAP_SECONDS - (time.monotonic() - _last_request_at)
    if gap > 0:
        time.sleep(gap)
    ensure_idle()
    _last_request_at = time.monotonic()
    try:
        return function(*args, **kwargs)
    except JobError as exc:
        if getattr(exc, 'is_browser_check', False) or _restriction.search(str(exc)):
            reason = str(exc)[:180]
            set_pause(True, reason)
            raise UpstreamError('平台提示异常，已停止并暂停后续回复回读：' + reason) from exc
        raise
