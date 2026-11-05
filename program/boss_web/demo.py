"""An isolated workspace for trying local stages without a platform account.

These are fictional JDs, not replayed platform responses. No scores, drafts or
delivery outcomes are seeded: those must be produced by the user's workflow.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

OFFLINE_ENV = 'BOSS_PLATFORM_OFFLINE'
MARKER = '.boss-career-demo.json'
SCHEMA = 'boss-career-demo/v1'
MESSAGE = '离线体验模式已禁用平台操作。请使用本地示例岗位继续评分、写稿或面试准备。'


def enabled() -> bool:
    return os.environ.get(OFFLINE_ENV) == '1'


def status() -> dict:
    return {'enabled': enabled(), 'source': 'fictional_demo' if enabled() else None,
            'platform_access': 'disabled' if enabled() else 'normal',
            'notice': MESSAGE if enabled() else ''}


def configure(folder: Path | str | None = None) -> dict:
    """Select a dedicated data directory before any runtime state is restored.

    Override inherited real-install paths; refuse nonempty, unmarked folders.
    Restarts preserve the user's candidate, plan, scores and edited sample JDs.
    """
    from .runtime import executable_dir, resource_root

    root = Path(folder).expanduser().resolve() if folder else executable_dir() / 'data-demo'
    marker = root / MARKER
    if root.exists() and any(root.iterdir()):
        try:
            valid = json.loads(marker.read_text(encoding='utf-8')).get('schema') == SCHEMA
        except (OSError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError(f'离线体验需要空目录或已有示例目录，已保留原目录：{root}')
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'schema': SCHEMA, 'source': 'fictional_demo'}), encoding='utf-8')
    os.environ.update({
        OFFLINE_ENV: '1', 'BOSS_SERVE_MODE': 'demo', 'BOSS_PANEL_RUNNER': '1',
        'BOSS_APP_DATA': str(root), 'BOSS_DB': str(root / 'boss.db'),
        'BOSS_WORKFLOW_DIR': str(root / 'workspace'),
        'BOSS_WORKFLOW_CODE': str(resource_root() / 'workflow'),
        'BOSS_CHROME_PROFILE': str(root / 'chrome_profile'),
    })
    from boss_core.workspace import Workspace
    from boss_jobs.models import Job, PageResult
    from boss_jobs.store import JobStore

    workspace = root / 'workspace'
    if not (workspace / 'workspace.json').exists():
        Workspace.create(workspace, name='离线体验（虚构岗位）', actor='demo')
    else:
        Workspace.open(workspace)
    with JobStore() as store:
        missing = []
        for item in SAMPLES:
            if store.get_job(item['encrypt_job_id']) is None:
                missing.append(Job(**item, job_degree='本科', jd_status='completed',
                                   raw_json=json.dumps({'source': 'fictional_demo', 'synthetic': True}),
                                   auto_delivery_blocked=True))
        if missing:
            store.save_page(PageResult(page=1, jobs=tuple(missing), raw_count=len(missing)))
    return {'root': str(root), 'sample_count': len(SAMPLES), **status()}


def blocked_request(path: str, method: str, query, body: dict) -> bool:
    """Reject online actions early; RequestControl is the transport-level guard."""
    if path == '/api/filters/locations' and str(query.get('refresh', '')).lower() in ('true', '1'):
        return True
    if method in ('GET', 'HEAD', 'OPTIONS'):
        return False
    if path in ('/api/system/chrome/open', '/api/system/workspace', '/api/platform/resume'):
        return True
    if path.startswith(('/api/auth/', '/api/crawl/', '/api/deliver/', '/api/feedback/')):
        return True
    if path.startswith(('/api/workflow/control/authorization', '/api/workflow/control/schedule')):
        return True
    if path.startswith('/api/jobs/') and (path.endswith('/verify') or '/fetch-descriptions' in path):
        return True
    if path == '/api/ai/tasks':
        return body.get('stage') in ('collect', 'send', 'feedback')
    if path == '/api/workflow/control/tasks':
        return body.get('action') in ('collect', 'send', 'auto_send', 'feedback')
    return False


SAMPLES = [
    dict(encrypt_job_id='demo-manufacturing-intern', job_name='AI应用开发实习生',
         brand_name='【示例】甲制造智能', city_name='苏州', location='苏州',
         salary_desc='180–220元/天', job_experience='在校生',
         brand_industry='工业软件', brand_scale_name='20-99人',
         skills=('Python', 'RAG', 'Agent'),
         job_desc='【离线虚构样本，不代表真实在招岗位或市场薪资】\n'
                  '为制造企业构建设备维护知识库及工单助手。使用Python实现RAG、引用溯源、'
                  '权限隔离与评测，参与客户现场需求确认。面向2027届，日常实习，每周至少4天、'
                  '持续4个月。不提供住宿，餐补情况待确认。'),
    dict(encrypt_job_id='demo-fde-campus', job_name='FDE／AI交付工程师（校招）',
         brand_name='【示例】乙工业智能体', city_name='上海', location='上海',
         salary_desc='12-18K', job_experience='应届生',
         brand_industry='人工智能', brand_scale_name='100-499人',
         skills=('Python', 'FastAPI', 'Agent', 'SQL'),
         job_desc='【离线虚构样本，不代表真实在招岗位或市场薪资】\n'
                  '面向2027届本科毕业生。与工厂业务人员梳理流程，开发Agent及数据接口，'
                  '完成部署、质量评测和持续迭代。要求Python及接口开发能力，理解RAG。'
                  '约30%的时间出差，现场交付与研发的具体比例需进一步确认。'),
    dict(encrypt_job_id='demo-generic-backend', job_name='Python后端实习生',
         brand_name='【示例】丙网络服务', city_name='成都', location='成都',
         salary_desc='150元/天', job_experience='在校生',
         brand_industry='互联网', brand_scale_name='20-99人', skills=('Python', 'Redis'),
         job_desc='【离线虚构样本，不代表真实在招岗位或市场薪资】\n'
                  '开发通用营销系统的API、缓存与报表。每周4天，实习3个月。'
                  '岗位职责未涉及模型、Agent或RAG，未明确服务的垂直行业；不包吃住。'),
    dict(encrypt_job_id='demo-senior-vision', job_name='工业视觉算法负责人',
         brand_name='【示例】丁视觉科技', city_name='深圳', location='深圳',
         salary_desc='30-45K', job_experience='5-10年',
         brand_industry='智能制造', brand_scale_name='100-499人', skills=('C++', '计算机视觉'),
         job_desc='【离线虚构样本，不代表真实在招岗位或市场薪资】\n'
                  '负责产线缺陷检测算法与团队管理。要求5年以上工业视觉研发经验、'
                  '相机选型及产线调试经验，熟悉C++部署。岗位为社招，不接受实习或应届生。'),
    dict(encrypt_job_id='demo-incomplete-offer', job_name='工业知识库实习生',
         brand_name='【示例】戊设备服务', city_name='武汉', location='武汉',
         salary_desc='面议', job_experience='在校生',
         brand_industry='机械设备', brand_scale_name='20人以下', skills=('RAG', 'Python'),
         job_desc='【离线虚构样本，不代表真实在招岗位或市场薪资】\n'
                  '参与设备说明书整理及售后知识库建设。JD同时提到数据标注和AI开发，'
                  '但两者比例、实习周期、薪酬和食宿均未写明，需向招聘方核实。'),
]
