"""HTTP 路由层。"""

from __future__ import annotations

from fastapi import APIRouter

from . import ai, auth, collection, crawl, deliver, feedback, filters, interview, jobs, llm, match, resume, system, workflow, workflow_control, workspace

api_router = APIRouter()
from . import platform
api_router.include_router(platform.router, prefix='/platform', tags=['platform'])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(filters.router, prefix="/filters", tags=["filters"])
api_router.include_router(collection.router, prefix="", tags=["collection"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["jobs"])
api_router.include_router(crawl.router, prefix="/crawl", tags=["crawl"])
api_router.include_router(llm.router, prefix="/llm", tags=["llm"])
api_router.include_router(resume.router, prefix="/resume", tags=["resume"])
api_router.include_router(match.router, prefix="/match", tags=["match"])
api_router.include_router(interview.router, prefix="/interview", tags=["interview"])
api_router.include_router(deliver.router, prefix="/deliver", tags=["deliver"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
# 工作区配置必须能"没配置时也打开"，所以和 system 同前缀、但自己一组路由。
api_router.include_router(workspace.router, prefix="/system", tags=["system"])
api_router.include_router(feedback.router, prefix="/feedback", tags=["feedback"])
api_router.include_router(workflow.router, prefix="/workflow", tags=["workflow"])
api_router.include_router(workflow_control.router, prefix="/workflow/control", tags=["workflow-control"])


@api_router.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """健康检查探针。"""
    return {"ok": "true", "service": "boss_web"}
