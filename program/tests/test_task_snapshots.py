"""Queued plan-dependent tasks must not silently adopt a different plan."""

import gc
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import boss_db
from boss_core import snapshot
from boss_core.workspace import Workspace
from boss_web.app import create_app
from boss_web.services.workflow_tasks import build_command


WORKFLOW = Path(__file__).resolve().parents[1] / 'workflow'


class TaskSnapshotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        env = {key: value for key, value in os.environ.items() if not key.startswith('BOSS_')}
        env.update(BOSS_APP_DATA=str(self.root / 'data'), BOSS_DB=str(self.root / 'boss.db'),
                   BOSS_WORKFLOW_DIR=str(self.root / 'workspace'), BOSS_WORKFLOW_CODE=str(WORKFLOW),
                   BOSS_PANEL_RUNNER='0', BOSS_API_BASE='http://127.0.0.1:0')
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(gc.collect)
        self.addCleanup(boss_db.close_all)
        self.ws = Workspace.create(self.root / 'workspace', name='Test workspace')
        self.candidate = self.ws.save_candidate({
            'label': 'Fictional candidate',
            'facts': [{'id': 'F1', 'text': 'Works on local test fixtures'}],
        })
        self.plan = self.activate_plan('old query')

    def activate_plan(self, keyword):
        plan = self.ws.save_plan({
            'label': keyword, 'candidate_id': self.candidate['id'],
            'search': {'keywords': [keyword]},
        })
        self.ws.activate_plan(plan['id'])
        return plan

    def test_queue_entry_points_capture_a_snapshot_and_keep_it_on_retry(self):
        snap = snapshot.build(self.ws)
        headers = {'Idempotency-Key': 'panel-greeting'}
        body = {'action': 'prepare', 'confirmed': True}
        with TestClient(create_app()) as client:
            ai = client.post('/api/ai/tasks', json={'stage': 'collect', 'scope': 'all'})
            panel = client.post('/api/workflow/control/tasks', json=body, headers=headers)
            self.assertEqual(ai.status_code, 200, ai.text)
            self.assertEqual(panel.status_code, 200, panel.text)
            ai_task = client.get('/api/ai/tasks/' + ai.json()['task']['id']).json()['task']
            panel_task = client.get('/api/workflow/control/tasks/' + panel.json()['id']).json()
            for task in (ai_task, panel_task):
                self.assertEqual(task['payload']['plan_id'], self.plan['id'])
                self.assertEqual(task['payload']['snapshot_fingerprint'], snap['fingerprint'])

            self.activate_plan('new query')
            retry = client.post('/api/workflow/control/tasks', json=body, headers=headers)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertEqual(retry.json()['id'], panel.json()['id'])
            self.assertEqual(retry.json()['payload']['snapshot_fingerprint'], snap['fingerprint'])

    def test_worker_commands_reject_stale_snapshot_before_contacting_the_panel(self):
        old = snapshot.build(self.ws)
        self.activate_plan('new query')
        for action in ('collect', 'match', 'prepare'):
            with self.subTest(action=action):
                payload = {'profile_version': self.candidate['id'], 'limit': 1,
                           'plan_id': self.plan['id'], 'snapshot_fingerprint': old['fingerprint']}
                cmd = build_command(WORKFLOW, self.ws.root, {'id': action, 'action': action, 'payload': payload})
                proc = subprocess.run(cmd, cwd=WORKFLOW,
                                      env={**os.environ, 'BOSS_PLATFORM_OFFLINE': '1', 'PYTHONUTF8': '1'},
                                      capture_output=True, encoding='utf-8', timeout=10)
                self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
                self.assertIn('任务创建后已变化', proc.stdout)


if __name__ == '__main__':
    unittest.main()
