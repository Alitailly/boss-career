"""Switching workspaces must preserve the old library and isolate new personal data."""

import gc
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import boss_db
from boss_core.workspace import Workspace
from boss_jobs.models import Job, PageResult
from boss_jobs.store import JobStore
from boss_web.app import create_app
from boss_web.services import personal_data, workspace_setup
from boss_web.services.interview_generation_task import save_questions
from boss_web.services.interview_generator import InterviewQuestion
from boss_web.services.resume_store import create_revision, save_analysis, save_resume


class WorkspaceStorageTests(unittest.TestCase):
    def test_switch_on_restart_preserves_and_separates_personal_records(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(gc.collect)
        self.addCleanup(boss_db.close_all)
        root = Path(folder.name).resolve()
        original = Workspace.create(root / 'first', name='First')
        other = root / 'second'
        installation = root / 'boss.db'
        job_id = 'fixture-job'

        def seed_personal(label):
            resume = save_resume(f'Sample resume for {label}', filename=f'{label}.md')
            analysis_id = save_analysis({'resume_id': resume.resume_id, 'matches': [
                {'encrypt_job_id': job_id, 'match_score': 80}]})
            create_revision(revision_id=label, resume_id=resume.resume_id, resume_title=label,
                            target_kind='jobs', target_json={}, crawl_task_id='',
                            job_count=1, original_md=resume.raw)
            save_questions(analysis_id=analysis_id, encrypt_job_id=job_id, questions=[
                InterviewQuestion(f'Question for {label}', 'general', 'basic', False)])
            return resume.resume_id, analysis_id

        def collection(client, path):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['items']

        def check_records(client, label, ids):
            resume_id, analysis_id = ids
            self.assertEqual([r['resume_id'] for r in collection(client, '/api/resume/list')], [resume_id])
            self.assertEqual(collection(client, '/api/match/index')[job_id]['analysis_id'], analysis_id)
            self.assertEqual([r['revision_id'] for r in collection(client, '/api/resume/revisions')], [label])
            records = collection(client, '/api/interview/records?title=Shared')
            self.assertEqual([(r['analysis_id'], r['job_name']) for r in records], [(analysis_id, 'Shared job')])

        env = {k: v for k, v in os.environ.items() if not k.startswith('BOSS_')}
        env.update(BOSS_APP_DATA=str(root), BOSS_DB=str(installation), BOSS_PANEL_RUNNER='0')
        with patch.dict(os.environ, env, clear=True):
            job = Job(encrypt_job_id=job_id, job_name='Shared job', brand_name='Example',
                      location='', salary_desc='', job_experience='', job_degree='',
                      brand_industry='', brand_scale_name='')
            with JobStore() as jobs:
                jobs.save_page(PageResult(page=1, jobs=(job,), raw_count=1))
            legacy = seed_personal('legacy')
            os.environ['BOSS_WORKFLOW_DIR'] = str(original.root)
            with TestClient(create_app()) as client:
                staged = client.post('/api/system/workspace', json={'path': str(other), 'confirmed': True})
                self.assertEqual(staged.status_code, 200, staged.text)
                self.assertTrue(staged.json()['restart_required'])
                self.assertEqual(staged.json()['dir'], str(original.root))
                self.assertEqual(staged.json()['next_dir'], str(other))
                check_records(client, 'legacy', legacy)

            del os.environ['BOSS_WORKFLOW_DIR']
            workspace_setup.apply_saved()
            self.assertEqual(personal_data.database_path(), other / 'personal.db')
            with TestClient(create_app()) as client:
                for path in ('/api/resume/list', '/api/resume/revisions', '/api/interview/records'):
                    self.assertEqual(collection(client, path), [])
                self.assertEqual(collection(client, '/api/match/index'), {})
                second = seed_personal('second')
                check_records(client, 'second', second)
                response = client.post('/api/system/workspace', json={'path': str(original.root), 'confirmed': True})
                self.assertTrue(response.json()['restart_required'])

            del os.environ['BOSS_WORKFLOW_DIR']
            workspace_setup.apply_saved()
            self.assertEqual(personal_data.database_path(), installation)
            with TestClient(create_app()) as client:
                check_records(client, 'legacy', legacy)
