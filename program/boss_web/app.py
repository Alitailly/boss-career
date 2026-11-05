"""FastAPI 应用工厂：挂路由、静态资源、异常壳。"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from boss_core.errors import CoreError
from boss_jobs.cdp_stoken import StokenError
from boss_jobs.errors import JobError

from . import config as C
from .api import api_router
from .errors import WebError
from boss_filter.validation import FilterValidationError
from .services.troubleshoot import humanize

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """组装 FastAPI 应用：挂 `/api` 路由、静态资源与异常壳。"""
    @asynccontextmanager
    async def lifespan(app):
        from .api.workflow import workflow_dir, workflow_code_dir
        from .services.workflow_tasks import Runner
        def resolve_paths():
            root = workflow_dir()
            return root, workflow_code_dir(root)
        runner = None
        # serve / setup 模式只起服务：不认领面板任务，避免"看看设置"就跑采集或发送。
        if os.environ.get("BOSS_PANEL_RUNNER", "1") != "0":
            runner = Runner(resolve_paths)
            runner.start()
        try:
            yield
        finally:
            if runner is not None:
                runner.stop()

    app = FastAPI(
        title="BOSS 控制台",
        version=__import__("boss_web", fromlist=["__version__"]).__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # `/api/system/workspace` 也挂在这条 router 里：它必须在工作区**还没配置**时
    # 就能响应（GET 报现状 + 下一步，POST 建/绑工作区），所以不要给 /api 套
    # 「工作区必须就绪」的依赖或中间件；就绪校验留给真正读台账的
    # `workflow_control.paths()`。
    app.include_router(api_router, prefix="/api")

    @app.middleware('http')
    async def _offline_demo(request: Request, call_next):
        from . import demo
        if demo.enabled():
            body = {}
            if request.url.path in ('/api/ai/tasks', '/api/workflow/control/tasks'):
                try:
                    body = await request.json()
                except ValueError:
                    pass  # Leave malformed JSON to the normal request validation.
                if not isinstance(body, dict):
                    body = {}
            if demo.blocked_request(request.url.path, request.method, request.query_params, body):
                return JSONResponse(status_code=409, content={
                    'ok': False, 'code': 'offline_demo', 'message': demo.MESSAGE})
        return await call_next(request)

    @app.exception_handler(WebError)
    async def _web_error(request: Request, exc: WebError) -> JSONResponse:  # noqa: ANN202
        """业务错误回 JSON，并按状态码落一行日志。"""
        level = logger.warning if exc.status_code >= 500 else logger.info
        level(
            "业务错误：%s %s -> %s %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(FilterValidationError)
    async def _filter_error(_: Request, exc: FilterValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"ok": False, "code": "invalid_input",
                            "field": exc.field, "message": str(exc),
                            "next": ["GET /api/filters/locations?city=实际城市代码，选择对应城市目录中的选项"
                                     if exc.field in ('multiBusinessDistrict','multiSubway','city_filters') else
                                     "GET /api/filters/conditions，选择返回的代码或精确名称"]})

    @app.exception_handler(CoreError)
    async def _core_error(request: Request, exc: CoreError) -> JSONResponse:  # noqa: ANN202
        """核心库的稳定错误类别：带 code/field/next，外部 AI 不用猜字符串。"""
        logger.info("核心错误：%s %s -> %s %s", request.method, request.url.path,
                    exc.status_code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(StokenError)
    async def _stoken_error(_: Request, exc: StokenError) -> JSONResponse:  # noqa: ANN202
        """取令牌失败翻成环境问题人话，回 503。"""
        logger.warning("取安全令牌失败：%s", exc)
        return JSONResponse(
            status_code=503,
            content={"ok": False, "code": "environment", "message": humanize(exc)},
        )

    @app.exception_handler(JobError)
    async def _job_error(_: Request, exc: JobError) -> JSONResponse:  # noqa: ANN202
        """`boss_jobs` 的异常翻成人话，回 502，不掉进兜底 500。"""
        logger.warning("职位/采集调用失败：%s", exc)
        return JSONResponse(
            status_code=502,
            content={"ok": False, "code": "upstream", "message": humanize(exc)},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        """兜底：未预料异常回 500 人话，不甩 traceback。"""
        logger.exception("未处理的异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "code": "internal",
                "message": "程序内部出了点问题，已经记进日志了。"
                "关掉重开一次通常就好；还是不行的话，把「数据目录」里 logs\\boss.log 发给开发者。",
            },
        )

    #: 路径模板 → 支持的方法。只有 `/api/*` 没命中具体路由时才会建，建一次就缓存。
    #: 走 OpenAPI 而不遍历 `app.routes`：新版 FastAPI 的 include_router 是惰性的
    #: （`_IncludedRouter`），路由不在 `app.router.routes` 里。
    _api_methods: dict[str, set[str]] = {}

    def _api_method_index() -> dict[str, set[str]]:
        if not _api_methods:
            for template, item in (app.openapi().get("paths") or {}).items():
                methods = {name.upper() for name in item
                           if name.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")}
                if methods:
                    _api_methods[template] = methods
        return _api_methods

    static_dir = C.STATIC_DIR
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.middleware("http")
        async def _no_cache_static(request: Request, call_next):  # noqa: ANN202
            """静态与接口响应加禁缓存头，避免跨文件版本错配与旧登录态。"""
            response = await call_next(request)
            if request.url.path.endswith(".js"):
                response.headers["Content-Type"] = "application/javascript; charset=utf-8"
            if request.url.path.startswith("/static/") or request.url.path == "/":
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            elif request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:  # noqa: ANN202
            """回 SPA 首页壳。"""
            return FileResponse(static_dir / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):  # noqa: ANN202
            """未命中路径回 SPA 壳；真静态文件直接回文件。

            例外是 `/api/*`：没命中就回 JSON 404 / 405。以前这类路径会拿到
            200 的首页 HTML，调用方（尤其接手的 AI）会把它当成"接口成功但返回
            怪东西"，或者把只支持 PUT 的路径当成 GET 也可用。
            """
            if full_path == "api" or full_path.startswith("api/"):
                target = "/" + full_path
                allow: set[str] = set()
                for template, methods in _api_method_index().items():
                    pattern = re.sub(r"\{[^}]+\}", r"[^/]+", template)
                    if re.fullmatch(pattern, target):
                        allow |= methods
                if allow:
                    ordered = ", ".join(sorted(allow))
                    return JSONResponse(
                        status_code=405,
                        headers={"Allow": ordered},
                        content={"ok": False, "code": "method_not_allowed",
                                 "message": f"/{full_path} 存在，但不接受 GET；只支持 {ordered}。",
                                 "allow": sorted(allow)},
                    )
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "code": "not_found",
                             "message": f"没有这个接口：/{full_path}",
                             "hint": "接口清单见 GET /api/ai/manifest，"
                                     "完整 OpenAPI 见 GET /api/openapi.json"},
                )
            candidate = static_dir / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


def app_factory() -> FastAPI:
    """uvicorn `factory=True` 入口。"""
    return create_app()
