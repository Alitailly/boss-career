"""Local identity snapshots; never log credentials or probe the platform."""
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass

import boss_db
from boss_jobs.errors import JobError


class AccountChangedError(JobError):
    pass


def session_lock(path=None):
    from boss_jobs.request_control import RequestControl
    return RequestControl(boss_db.resolve_db_path(path).parent / 'session-control').locked()


@dataclass(frozen=True)
class AccountBinding:
    account_id: str
    revision: str
    path: object

    @contextmanager
    def guard(self):
        # Serialize credential replacement with an actual in-flight request.
        with session_lock(self.path):
            if binding(self.path) != self:
                raise AccountChangedError('登录账号或会话已更换，旧任务已停止；请确认新账号后重新启动任务')
            yield


def binding(path=None, *, required=True):
    from .session import load_session
    resolved = boss_db.resolve_db_path(path)
    stored = load_session(resolved)
    return from_session(stored, resolved, required=required)


def from_session(stored, resolved, *, required=True):
    account_id = str(stored.user.get('user_id') or '').strip()
    if stored.is_empty or not account_id:
        if required:
            raise AccountChangedError('无法确认当前发送账号，请先通过正常登录确认身份')
        return None
    # Browser token refresh preserves saved_at and does not change account identity.
    material = [account_id, stored.token, stored.saved_at,
                {k: v for k, v in stored.cookies.items() if k != '__zp_stoken__'}]
    revision = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return AccountBinding(account_id, revision, resolved)


def current_account():
    current = binding(required=False)
    return {'ok': True, 'sender_account_id': current.account_id if current else '',
            'session_version': current.revision if current else '',
            'identity_known': current is not None}


def pin_payload(action, payload):
    if action not in {'collect', 'send', 'auto_send', 'feedback'} or '_account_binding' in payload:
        return dict(payload)
    return {**payload, '_account_binding': current_account()}


def assert_task_environment():
    import os
    expected = os.environ.get('BOSS_TASK_ACCOUNT_ID')
    revision = os.environ.get('BOSS_TASK_SESSION_VERSION')
    current = binding()
    if expected and current.account_id != expected or revision and current.revision != revision:
        raise AccountChangedError('后台任务绑定的账号或会话已变化，请重新确认后启动新任务')
    return current
