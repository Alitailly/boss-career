"""Queued plan-dependent tasks must not silently adopt a different plan."""

import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from fastapi.testclient import TestClient

import boss_db
from boss_core import snapshot
from boss_core.workspace import Workspace
from boss_web.app import create_app
from boss_web.services import single_scoring, workflow_context
from boss_web.services.resume_store import save_resume
from boss_web.services.workflow_tasks import TaskStore, build_command


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

    def test_cancel_panel_task_does_not_cancel_an_unrelated_delivery(self):
        store = TaskStore(self.ws.root)
        with patch('boss_login.account.pin_payload', return_value={}):
            job = store.add('send', {})
        store.claim()
        other = SimpleNamespace(task_id='dv_other', status='running')
        with (TestClient(create_app()) as client,
              patch('boss_web.services.deliver_task.deliver_tasks.current', return_value=other),
              patch('boss_web.services.deliver_task.deliver_tasks.cancel') as cancel):
            response = client.post(f"/api/workflow/control/tasks/{job['id']}/cancel")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'stopping')
            cancel.assert_not_called()
        self.assertTrue((self.ws.root / 'runs' / 'panel-jobs' / f"{job['id']}.stop").is_file())

    def test_workflow_waiter_cancels_only_its_own_delivery_and_waits_for_results(self):
        with patch.object(sys, 'path', [str(WORKFLOW), *sys.path]):
            from workflow import deliver_round_v2 as worker

        stop = self.root / 'send.stop'
        stop.touch()
        running = {'task_id': 'dv_owned', 'status': 'running'}
        cancelled = {**running, 'status': 'cancelled', 'items': []}
        with (patch.object(worker, 'STOP_FILE', stop),
              patch.object(worker.time, 'sleep'),
              patch.object(worker, 'api', side_effect=[running, {}, running, cancelled]) as api):
            self.assertEqual(worker.wait_task('dv_owned'), cancelled)
            self.assertEqual(api.call_args_list, [
                call('GET', '/api/deliver/status', timeout=20),
                call('POST', '/api/deliver/dv_owned/cancel', timeout=5),
                call('GET', '/api/deliver/status', timeout=20),
                call('GET', '/api/deliver/status', timeout=20),
            ])

        with (patch.object(worker, 'STOP_FILE', stop),
              patch.object(worker, 'api', return_value={**running, 'task_id': 'dv_other'}) as api):
            self.assertEqual(worker.wait_task('dv_owned')['status'], 'changed')
            api.assert_called_once_with('GET', '/api/deliver/status', timeout=20)

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

    def test_matching_endpoint_rechecks_fingerprint_even_with_cached_plan(self):
        queued = workflow_context.snapshot(refresh=True)
        self.activate_plan('new query')
        with TestClient(create_app()) as client:
            response = client.post('/api/match/analyze', json={
                'resume_id': 'unused', 'snapshot_fingerprint': queued['fingerprint'],
            })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['code'], 'stale_input')

    def test_writer_rejects_a_plan_changed_after_worker_start(self):
        queued = snapshot.build(self.ws)
        self.activate_plan('new query')
        script = WORKFLOW / 'greeting' / 'writer' / 'bridge.py'
        proc = subprocess.run(
            [sys.executable, str(script), '--request', str(self.ws.root / 'greeting' / 'unused.json'),
             '--expected-snapshot', queued['fingerprint']],
            cwd=WORKFLOW, capture_output=True, encoding='utf-8', timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)['code'], 'stale_input', proc.stderr)

    def test_scoring_context_uses_only_its_bound_snapshot(self):
        resume = save_resume('Fictional resume')
        self.candidate = self.ws.save_candidate({
            **self.candidate,
            'resume': {'resume_id': resume.resume_id},
            'scoring_material': {
                'text': 'Bound scoring material', 'confirmed': True,
                'resume_id': resume.resume_id, 'source_sha': single_scoring.source_sha(resume),
                'facts_sha': single_scoring.facts_sha(self.candidate),
            },
        })
        strategy = self.ws.save_strategy({'stage': 'matching', 'content': {
            'criteria': 'Use the original scoring rules', 'evaluation': {'batch': 2},
        }})
        plan = self.ws.save_plan({'candidate_id': self.candidate['id'],
                                  'strategies': {'matching': strategy['id']},
                                  'runtime': {'min_score': 73}})
        self.ws.activate_plan(plan['id'])
        bound = snapshot.build(self.ws)
        self.activate_plan('new plan')
        with patch.object(workflow_context, 'snapshot', side_effect=AssertionError('reloaded active plan')):
            context = single_scoring.context(resume, snap=bound)
        self.assertEqual(context['snapshot']['fingerprint'], bound['fingerprint'])
        self.assertEqual(context['material']['text'], 'Bound scoring material')
        self.assertEqual(context['standard']['text'], 'Use the original scoring rules')
        self.assertEqual(context['threshold'], 73)
        self.assertEqual(context['limits']['stage1_batch'], 2)


if __name__ == '__main__':
    unittest.main()
