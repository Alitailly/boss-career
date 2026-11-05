"""Cross-process pacing and persistent restrictions for this installation's account.

Only local JSON and a lock file; no cookies, query parameters or message bodies.
"""
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import threading
import time
import uuid
from collections import deque

from .errors import JobApiError

_locks = {}
_index_lock = threading.Lock()


def scope_for(path):
    if 'detail' in path: return 'details'
    if 'joblist' in path or 'job/list' in path: return 'search'
    if 'friend/add' in path: return 'send'
    if 'chat' in path.lower() or 'BossData' in path: return 'chat'
    if '/browser/' in path: return 'token'
    return 'other'


def restriction(exc):
    if not isinstance(exc, JobApiError): return None
    if exc.code in (401, 403, 429) or exc.is_risk_control or exc.is_session_expired or exc.is_chat_rate_limited:
        return 'account'
    if any(word in exc.message for word in ('账户存在异常', '账号存在异常', '异常行为', '请求频繁', '操作频繁')):
        return 'account'
    if exc.is_browser_check: return 'scope'
    return None


class RequestControl:
    gap = 8.0
    limits = ((60., 6), (600., 40))

    def __init__(self, root, *, clock=time.time, sleeper=time.sleep):
        self.root = Path(root)
        self.clock, self.sleep = clock, sleeper
        self.path = self.root / 'state.json'

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with _index_lock:
            lock = _locks.setdefault(str(self.root.resolve()), threading.RLock())
        with lock, (self.root / 'request.lock').open('a+b') as file:
            file.seek(0, 2)
            if file.tell() == 0: file.write(b'0'); file.flush()
            file.seek(0)
            if os.name == 'nt':
                import msvcrt
                while True:
                    try: msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1); break
                    except OSError: time.sleep(.05)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX)
            try: yield
            finally:
                file.seek(0)
                if os.name == 'nt': msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(file, fcntl.LOCK_UN)

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, dict): raise ValueError()
            if not isinstance(data.get('attempts', []), list): raise ValueError()
            return data
        except FileNotFoundError:
            return {'account_paused': False, 'paused_scopes': {}, 'attempts': []}
        except (ValueError, OSError):
            raise JobApiError(36, '平台请求控制记录异常，已停止请求，请检查本地记录') from None

    def save(self, data):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, self.path)

    def check(self, data, scope):
        if data.get('account_paused'):
            raise JobApiError(36, '账号平台操作已暂停，请先处理平台提示并明确恢复；不会自动重试')
        if scope in data.get('paused_scopes', {}):
            raise JobApiError(37, f'{scope} 请求链已暂停，请先处理平台提示并明确恢复')

    def assert_allowed(self, scope):
        with self.locked(): self.check_recorded(self.read(), scope)

    def check_recorded(self, data, scope):
        try:
            self.check(data, scope)
        except JobApiError as exc:
            self.event({'event': 'blocked', 'scope': scope, 'code': exc.code,
                        'outcome': 'blocked', 'blocked_reason':
                        'account_paused' if data.get('account_paused') else 'scope_paused'})
            raise

    def event(self, fields):
        """Only callers' fixed categories and timings; never platform payloads."""
        now = self.clock()
        stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone()
        entry = {'schema': 2, 'at': stamp.isoformat(), 'timestamp': now,
                 'pid': os.getpid(), **fields}
        try:
            with (self.root / ('requests-' + stamp.date().isoformat() + '.jsonl')).open(
                    'a', encoding='utf-8') as log:
                log.write(json.dumps(entry) + '\n')
        except OSError:
            # An audit failure must never turn a sent message into a retry.
            import logging
            logging.getLogger(__name__).error('请求事件写入失败，请检查数据目录')

    def diagnostics(self, *, hours=24, limit=100):
        """Read bounded local log tails; this method never contacts the platform."""
        now = self.clock()
        cutoff = now - hours * 3600
        first = dt.datetime.fromtimestamp(max(0., cutoff), dt.timezone.utc).astimezone().date()
        last = dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone().date()
        events, by_scope = deque(maxlen=limit), {}
        truncated = False
        skipped = 0
        day = first
        while day <= last:
            path = self.root / ('requests-' + day.isoformat() + '.jsonl')
            day += dt.timedelta(days=1)
            try:
                with path.open('rb') as log:
                    size = log.seek(0, 2)
                    offset = max(0, size - 4 * 1024 * 1024)
                    log.seek(offset)
                    if offset:
                        log.readline()
                        truncated = True
                    for line in log:
                        try:
                            raw = json.loads(line)
                            ts = raw.get('timestamp')
                            if ts is None: ts = dt.datetime.fromisoformat(raw['at']).timestamp()
                            if not cutoff <= ts <= now: continue
                            # Old events are supported, but only allowlisted fields are returned.
                            entry = {k: raw[k] for k in (
                                'schema', 'at', 'timestamp', 'pid', 'operation_id', 'scope',
                                'event', 'code', 'outcome', 'blocked_reason', 'started_at',
                                'duration_seconds', 'queue_seconds', 'lock_wait_seconds',
                                'wait_reasons', 'wait_reason', 'expected_wait_seconds') if k in raw}
                            scope = entry.get('scope', 'other')
                            if scope not in ('search', 'details', 'send', 'chat', 'token', 'other'):
                                scope = 'other'
                            row = by_scope.setdefault(scope, {
                                'scope': scope, 'attempts': 0, 'errors': 0, 'blocked': 0,
                                'waiting_events': 0, 'timed_attempts': 0,
                                'duration_seconds': 0., 'queue_seconds': 0.,
                                'lock_wait_seconds': 0., 'wait_reasons': {}})
                            kind = entry.get('event', 'request')
                            if kind == 'blocked': row['blocked'] += 1
                            elif kind == 'waiting': row['waiting_events'] += 1
                            else:
                                row['attempts'] += 1
                                row['errors'] += entry.get('outcome') == 'error'
                                row['timed_attempts'] += 'duration_seconds' in entry
                                for key in ('duration_seconds', 'queue_seconds', 'lock_wait_seconds'):
                                    row[key] += entry.get(key, 0.)
                                for key, seconds in entry.get('wait_reasons', {}).items():
                                    row['wait_reasons'][key] = row['wait_reasons'].get(key, 0.) + seconds
                            events.append(entry)
                        except (ValueError, KeyError, TypeError, AttributeError):
                            skipped += 1
            except FileNotFoundError:
                continue
        for row in by_scope.values():
            for key in ('duration_seconds', 'queue_seconds', 'lock_wait_seconds'):
                row[key] = round(row[key], 3)
            row['wait_reasons'] = {k: round(v, 3) for k, v in row['wait_reasons'].items()}
        return {'ok': True, 'hours': hours, 'control': self.status(),
                'coverage': 'recorded_platform_operations_only',
                'truncated': truncated, 'skipped_lines': skipped,
                'by_scope': list(by_scope.values()), 'events': list(reversed(events)),
                'event_limit': limit}

    def status(self):
        data = self.read()
        return {**{k:v for k,v in data.items() if k != 'attempts'},
                'min_gap_seconds': self.gap, 'max_per_minute': 6, 'max_per_ten_minutes': 40,
                'requests_last_ten_minutes': sum(self.clock()-x < 600 for x in data.get('attempts', []))}

    def pause(self, scope='account'):
        with self.locked():
            data = self.read()
            if scope == 'account': data['account_paused'] = True
            else: data.setdefault('paused_scopes', {})[scope] = True
            data['paused_at'] = dt.datetime.now().astimezone().isoformat()
            self.save(data)

    def resume(self, scope):
        with self.locked():
            data = self.read()
            if scope in ('all', 'account'): data['account_paused'] = False
            if scope == 'all': data['paused_scopes'] = {}
            else: data.setdefault('paused_scopes', {}).pop(scope, None)
            # Retain request history: resuming must not reset the budget.
            self.save(data)

    @contextmanager
    def operation(self, path):
        scope = scope_for(path)
        operation_id = uuid.uuid4().hex
        queued_at = self.clock()
        lock_wait = 0.
        reasons = {}
        announced = set()
        while True:
            before_lock = self.clock()
            with self.locked():
                lock_wait += max(0., self.clock() - before_lock)
                data = self.read(); self.check_recorded(data, scope)
                now = self.clock()
                history = [x for x in data.get('attempts', []) if now-x < 600]
                wait = max(0., self.gap-(now-history[-1])) if history else 0.
                reason = 'min_gap'
                for window, cap in self.limits:
                    recent = [x for x in history if now-x < window]
                    if len(recent) >= cap and recent[-cap] + window - now > wait:
                        wait = recent[-cap] + window - now
                        reason = 'minute_budget' if window == 60 else 'ten_minute_budget'
                if wait <= 0:
                    data['attempts'] = history + [now]; self.save(data)
                    code = 0
                    outcome = 'ok'
                    try:
                        yield
                    except Exception as exc:
                        code = getattr(exc, 'code', None)
                        outcome = 'error'
                        level = restriction(exc)
                        if level == 'account': data['account_paused'] = True
                        elif level: data.setdefault('paused_scopes', {})[scope] = True
                        if level:
                            data['paused_at'] = dt.datetime.now().astimezone().isoformat()
                            self.save(data)
                        raise
                    finally:
                        self.event({'event': 'request', 'operation_id': operation_id,
                                    'scope': scope, 'code': code, 'outcome': outcome,
                                    'started_at': dt.datetime.fromtimestamp(now, dt.timezone.utc).astimezone().isoformat(),
                                    'duration_seconds': round(max(0., self.clock() - now), 3),
                                    'queue_seconds': round(max(0., now - queued_at), 3),
                                    'lock_wait_seconds': round(lock_wait, 3),
                                    'wait_reasons': {k: round(v, 3) for k, v in reasons.items()}})
                    return
                if reason not in announced:
                    self.event({'event': 'waiting', 'operation_id': operation_id,
                                'scope': scope, 'wait_reason': reason,
                                'expected_wait_seconds': round(wait, 3)})
                    announced.add(reason)
            # Release locks during waiting so pause/status actions remain responsive.
            before_sleep = self.clock()
            self.sleep(min(wait, 1.))
            reasons[reason] = reasons.get(reason, 0.) + max(0., self.clock() - before_sleep)


def get_control():
    import boss_db
    return RequestControl(boss_db.resolve_db_path().parent / 'platform-control')
