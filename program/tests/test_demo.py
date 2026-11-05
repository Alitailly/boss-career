"""Offline experience must stay isolated and never dispatch platform operations."""
import os
import gc
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'demo').resolve()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        import boss_db
        def close_databases():
            boss_db.close_all()
            gc.collect()  # sqlite connection context managers commit; GC closes the handles.
        self.addCleanup(close_databases)

    def configure(self):
        from boss_web.demo import configure
        configure(self.root)

    def test_cli_accepts_demo(self):
        from boss_web.cli import build_parser
        args = build_parser().parse_args(['--mode', 'demo', '--demo-dir', str(self.root)])
        self.assertEqual(args.mode, 'demo')

    def test_isolates_inherited_real_paths_and_loads_labelled_jobs(self):
        import boss_db
        from boss_jobs.store import JobStore
        from boss_core.workspace import Workspace
        os.environ.update(BOSS_DB='real.db', BOSS_WORKFLOW_DIR='real-workspace',
                          BOSS_APP_DATA='real-data', BOSS_WORKFLOW_CODE='real-code')
        self.configure()
        self.assertEqual(boss_db.resolve_db_path(), self.root / 'boss.db')
        ws = Workspace.open(self.root / 'workspace')
        self.assertIsNone(ws.active_candidate())
        with JobStore() as store:
            jobs = store.list_jobs(limit=20)
        self.assertGreaterEqual(len(jobs), 5)
        self.assertTrue(all(j.encrypt_job_id.startswith('demo-') for j in jobs))
        self.assertTrue(all('示例' in j.brand_name and j.job_desc for j in jobs))
        self.assertTrue(all(not j.security_id and not j.lid for j in jobs))

    def test_refuses_existing_non_demo_directory(self):
        from boss_web.demo import configure
        self.root.mkdir()
        (self.root / 'boss.db').write_bytes(b'preserve-me')
        with self.assertRaises(ValueError):
            configure(self.root)
        self.assertEqual((self.root / 'boss.db').read_bytes(), b'preserve-me')

    def test_restart_preserves_existing_jd_and_workspace(self):
        from boss_jobs.store import JobStore
        from boss_core.workspace import Workspace
        self.configure()
        identity = Workspace.open(self.root / 'workspace').meta()['id']
        with JobStore() as store:
            job = store.list_jobs(limit=1)[0]
            store.update_job_desc(job.encrypt_job_id, '用户修改过的示例正文')
        self.configure()
        self.assertEqual(Workspace.open(self.root / 'workspace').meta()['id'], identity)
        with JobStore() as store:
            self.assertEqual(store.get_job(job.encrypt_job_id).job_desc, '用户修改过的示例正文')

    def test_gateway_stays_blocked_after_resume(self):
        from boss_jobs.request_control import get_control
        from boss_jobs.errors import JobApiError
        self.configure()
        control = get_control()
        control.resume('all')
        reached_transport = False
        with self.assertRaises(JobApiError):
            with control.operation('/wapi/zpgeek/search/joblist.json'):
                reached_transport = True
        self.assertFalse(reached_transport)
        self.assertEqual(control.status()['requests_last_ten_minutes'], 0)

    def test_normal_gateway_still_allows_an_operation(self):
        from boss_jobs.request_control import RequestControl
        control = RequestControl(self.root / 'control')
        reached_transport = False
        with control.operation('/wapi/zpgeek/search/joblist.json'):
            reached_transport = True  # No real network: test the gateway context only.
        self.assertTrue(reached_transport)
        self.assertEqual(control.status()['requests_last_ten_minutes'], 1)

    def test_http_blocks_platform_actions_before_task_creation(self):
        self.configure()
        os.environ['BOSS_PANEL_RUNNER'] = '0'
        from boss_web.app import create_app
        with TestClient(create_app()) as client:
            for path, body in [
                ('/api/auth/qr/start', {}),
                ('/api/crawl/start', {}),
                ('/api/system/chrome/open', {}),
                ('/api/platform/resume', {'confirmed': True, 'scope': 'all'}),
                ('/api/ai/tasks', {'stage': 'collect', 'scope': 'all'}),
                ('/api/workflow/control/tasks', {'action': 'send', 'confirmed': True}),
                ('/api/feedback/sync', {}),
            ]:
                response = client.post(path, json=body)
                self.assertEqual(response.status_code, 409, path)
                self.assertEqual(response.json()['code'], 'offline_demo', path)
            self.assertEqual(client.get('/api/system/health').json()['mode'], 'demo')
            self.assertTrue(client.get('/api/ai/context').json()['demo']['enabled'])
            self.assertEqual(client.get('/api/ai/tasks').json()['items'], [])

    def test_local_pipeline_saves_scores_drafts_and_interview_without_network(self):
        self.configure()
        os.environ['BOSS_PANEL_RUNNER'] = '0'
        from boss_web.app import create_app
        with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network')):
            with TestClient(create_app()) as client:
                def call(method, path, **kwargs):
                    response = client.request(method, path, **kwargs)
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()

                raw = '【虚构QA资料】有Python和RAG项目经验。'
                resume = call('POST', '/api/resume/upload', files={
                    'file': ('qa.txt', raw.encode('utf-8'), 'text/plain')})
                candidate = call('POST', '/api/ai/candidates', json={
                    'label': '虚构QA候选人', 'resume_id': resume['resume_id'],
                    'source_sha': hashlib.sha256(raw.encode()).hexdigest(),
                    'facts': [{'id': 'F1', 'text': '有Python和RAG项目经验。'}]})['candidate']
                strategies = {}
                for stage in ('matching', 'greeting', 'interview'):
                    strategy = call('POST', '/api/ai/strategies', json={
                        'stage': stage, 'name': 'QA宿主AI方法', 'source': 'external_skill',
                        'content': {'instructions': '仅供回归测试的虚构输入输出。'}})['strategy']
                    strategies[stage] = strategy['id']
                plan = call('POST', '/api/ai/plans', json={
                    'label': 'QA离线方案', 'candidate_id': candidate['id'],
                    'target': {'directions': {'mode': 'list', 'values': ['AI应用']}}})['plan']
                plan = call('PATCH', f"/api/ai/plans/{plan['id']}", json={
                    'patch': {'search': {'keywords': ['AI']}, 'strategies': strategies}})['plan']
                call('POST', f"/api/ai/plans/{plan['id']}/activate", json={})
                call('PUT', '/api/ai/scoring-material', json={
                    'text': raw, 'confirmed': True, 'resume_id': resume['resume_id'],
                    'source_sha': hashlib.sha256(raw.encode()).hexdigest(),
                    'expected_candidate': candidate['id']})
                job_id = 'demo-manufacturing-intern'
                work = call('POST', '/api/ai/work', json={
                    'stage': 'matching', 'job_ids': [job_id]})['work']
                result = call('POST', f"/api/ai/work/{work['work_id']}/submit", json={
                    'results': [{'job_id': job_id, 'score': 80,
                                 'verdict': 'QA断言：RAG经验对应知识库职责，实习安排待核实。'}]})
                self.assertEqual(result['status'], 'applied')
                self.assertTrue(result['applied']['analysis_id'])
                work = call('POST', '/api/ai/work', json={
                    'stage': 'greeting', 'job_ids': [job_id]})['work']
                draft = '您好，我有Python和RAG项目经验，关注设备知识库建设，想了解该岗位的具体开发职责。'
                result = call('POST', f"/api/ai/work/{work['work_id']}/submit", json={
                    'results': [{'job_id': job_id, 'text': draft, 'facts': ['F1']}]})
                self.assertEqual(result['status'], 'applied')
                self.assertTrue(result['applied']['drafts'][0]['review_pending'])
                response = client.get(f'/api/ai/greeting-inputs/{job_id}')
                self.assertIn(draft, response.text)
                session = call('POST', '/api/ai/interviews', json={'job_id': job_id})['session']
                self.assertIn(job_id, str(session))
                self.assertEqual(client.get('/api/platform/control').json()['requests_last_ten_minutes'], 0)


if __name__ == '__main__':
    unittest.main()
