"""给外部 AI 用的接口：读上下文、改方案、登记策略、准备/接收结果、管面试。

这一层只做三件事：把 HTTP 参数翻译成 `boss_core` 调用、把业务错误翻成稳定类别、
记录谁做了什么。业务判断全在 `boss_core` 里，面板和 CLI 走同一套。

自报的 `X-Actor` 只用于审计回溯，**不是认证**；本机单用户场景不假装它有权限语义。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from boss_core import external, greeting, interview, snapshot
from boss_core.errors import Conflict, CoreError, InvalidInput, MissingConfiguration, NotFound
from boss_core.jsonio import digest
from boss_core.models import STAGES, default_plan
from boss_core import strategies as strategies_mod

from . import workflow as view
from ..errors import ValidationWebError
from ..services.workflow_tasks import ACTIONS, TaskStore

router = APIRouter()

CONTRACT_VERSION = "ai-contract/v1"

#: 意图清单：外部 AI 先读这个，再决定调哪个 operationId。不复制请求 schema，只引用。
MANIFEST: list[dict[str, Any]] = [
    {"id": "read_context", "operationId": "ai_context", "method": "GET", "path": "/api/ai/context",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "读取服务身份、活动方案/资料、有效配置、缺失项、任务摘要"},
    {"id": "save_scoring_material", "operationId": "ai_save_scoring_material", "method": "PUT",
     "path": "/api/ai/scoring-material", "prerequisites": ["candidate", "resume"],
     "side_effects": ["保存候选人新版本并改绑活动方案"], "cost": "none", "requires_human": True,
     "summary": "用户核对后保存评分用材料；原文指纹和候选人版本校验，保留其他个人配置"},
    {'id': 'read_city_filter_options', 'operationId': 'filter_locations', 'method': 'GET',
     'path': '/api/filters/locations', 'prerequisites': ['city'], 'side_effects': [],
     'cost': 'none', 'requires_human': False,
     'summary': '读取指定城市本地区/商圈、地铁目录；普通读取不联网。refresh=true 是单独的在线维护，有请求成本和本地写入'},
    {"id": "read_stage_inputs", "operationId": "ai_stage_inputs", "method": "GET",
     "path": "/api/ai/stage-inputs/{stage}", "prerequisites": [], "side_effects": [],
     "cost": "none", "requires_human": False, "summary": "读取当前实际模板及规则；不调用模型、不执行任务"},
    {"id": "save_candidate", "operationId": "ai_save_candidate", "method": "POST",
     "path": "/api/ai/candidates", "prerequisites": [], "side_effects": ["新增候选人版本并设为活动"],
     "cost": "none", "requires_human": False, "summary": "保存简历事实与结构化资料"},
    {"id": "read_manifest", "operationId": "ai_manifest", "method": "GET", "path": "/api/ai/manifest",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "读取本清单（意图 → 操作 → 副作用）"},
    {"id": "read_capabilities", "operationId": "ai_capabilities", "method": "GET",
     "path": "/api/ai/capabilities", "prerequisites": [], "side_effects": [], "cost": "none",
     "requires_human": False,
     "summary": "定制能力清单：每个阶段能改什么、默认值、示例、校验、生效位置、未实现限制"},
    {"id": "list_candidates", "operationId": "ai_list_candidates", "method": "GET",
     "path": "/api/ai/candidates", "prerequisites": [], "side_effects": [], "cost": "none",
     "requires_human": False, "summary": "列出候选人资料版本"},
    {"id": "read_candidate", "operationId": "ai_read_candidate", "method": "GET",
     "path": "/api/ai/candidates/{candidate_id}", "prerequisites": [], "side_effects": [],
     "cost": "none", "requires_human": False, "summary": "读一份候选人资料版本"},
    {"id": "read_plan", "operationId": "ai_read_plan", "method": "GET", "path": "/api/ai/plans/{plan_id}",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "读一份方案版本"},
    {"id": "list_tasks", "operationId": "ai_list_tasks", "method": "GET", "path": "/api/ai/tasks",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "最近任务列表"},
    {"id": "list_work", "operationId": "ai_list_work", "method": "GET", "path": "/api/ai/work",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "最近外部工作项"},
    {"id": "list_interviews", "operationId": "ai_list_interviews", "method": "GET",
     "path": "/api/ai/interviews", "prerequisites": [], "side_effects": [], "cost": "none",
     "requires_human": False, "summary": "最近面试会话"},
    {"id": "interview_questions", "operationId": "ai_interview_questions", "method": "POST",
     "path": "/api/ai/interviews/{session_id}/questions", "prerequisites": ["interview"],
     "side_effects": ["追加题目"], "cost": "none", "requires_human": False,
     "summary": "给会话写入题库"},
    {"id": "finish_interview", "operationId": "ai_finish_interview", "method": "POST",
     "path": "/api/ai/interviews/{session_id}/finish", "prerequisites": ["interview"],
     "side_effects": ["结束会话"], "cost": "none", "requires_human": True, "summary": "结束面试会话"},
    {"id": "setup", "operationId": "ai_setup", "method": "POST", "path": "/api/ai/setup",
     "prerequisites": [], "side_effects": ["建/迁移工作区"], "cost": "none",
     "requires_human": False, "summary": "新用户初始化工作区（可选迁移旧配置）"},
    {"id": "list_plans", "operationId": "ai_list_plans", "method": "GET", "path": "/api/ai/plans",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False, "summary": "列出方案版本"},
    {"id": "create_plan", "operationId": "ai_create_plan", "method": "POST", "path": "/api/ai/plans",
     "prerequisites": ["candidate"], "side_effects": ["新建方案草稿"], "cost": "none",
     "requires_human": False, "summary": "从中性默认或当前方案创建草稿"},
    {"id": "update_plan_draft", "operationId": "ai_update_plan", "method": "PATCH",
     "path": "/api/ai/plans/{plan_id}", "prerequisites": ["plan"], "side_effects": ["新建方案版本"],
     "cost": "none", "requires_human": False, "summary": "局部更新草稿；未提供字段保持不变"},
    {"id": "validate_plan", "operationId": "ai_validate_plan", "method": "POST",
     "path": "/api/ai/plans/{plan_id}/validate", "prerequisites": ["plan"], "side_effects": [],
     "cost": "none", "requires_human": False, "summary": "纯校验：冲突、缺项、可执行阶段"},
    {"id": "preview_plan_change", "operationId": "ai_preview_plan", "method": "POST",
     "path": "/api/ai/plans/{plan_id}/preview", "prerequisites": ["plan"], "side_effects": [],
     "cost": "none", "requires_human": False, "summary": "与当前活动方案的差异与影响面"},
    {"id": "activate_plan", "operationId": "ai_activate_plan", "method": "POST",
     "path": "/api/ai/plans/{plan_id}/activate", "prerequisites": ["plan", "validate"],
     "side_effects": ["切换活动方案；下一个任务生效"], "cost": "none", "requires_human": True,
     "summary": "激活通过校验的方案版本"},
    {"id": "list_strategies", "operationId": "ai_list_strategies", "method": "GET",
     "path": "/api/ai/strategies", "prerequisites": [], "side_effects": [], "cost": "none",
     "requires_human": False, "summary": "列出已登记策略"},
    {"id": "register_strategy", "operationId": "ai_register_strategy", "method": "POST",
     "path": "/api/ai/strategies", "prerequisites": [], "side_effects": ["登记一个策略版本"],
     "cost": "none", "requires_human": False, "summary": "登记策略内容与能力需求；不等于激活"},
    {"id": "bind_strategy", "operationId": "ai_bind_strategy", "method": "POST",
     "path": "/api/ai/strategies/{strategy_id}/bind", "prerequisites": ["plan", "strategy"],
     "side_effects": ["新建方案版本"], "cost": "none", "requires_human": False,
     "summary": "把策略绑到方案的某个阶段"},
    {"id": "judge_jobs", "operationId": "ai_judge", "method": "POST", "path": "/api/ai/judge",
     "prerequisites": ["plan"], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "按当前快照批量判断岗位条件，返回三态与依据"},
    {"id": "preview_stage", "operationId": "ai_preview_stage", "method": "POST",
     "path": "/api/ai/preview-stage", "prerequisites": ["plan", "llm"], "side_effects": ["调用模型"],
     "cost": "model", "requires_human": False,
     "summary": "对 1–3 个已存岗位生成样例评分（不写库、不发送）"},
    {"id":"greeting_inputs","operationId":"ai_greeting_inputs","method":"GET","path":"/api/ai/greeting-inputs/{job_id}",
     "prerequisites":[],"side_effects":[],"cost":"none","requires_human":False,
     "summary":"按岗位与正文指纹只读实际写稿输入输出：事实、岗位摘录、评分匹配点、规则、成稿"},
    {"id": "greeting_rules", "operationId": "ai_greeting_rules", "method": "GET",
     "path": "/api/ai/greeting-rules", "prerequisites": ["plan"], "side_effects": [], "cost": "none",
     "requires_human": False, "summary": "读取合成后的有效写作规则、指纹与冲突"},
    {"id": "create_task", "operationId": "ai_create_task", "method": "POST", "path": "/api/ai/tasks",
     "prerequisites": ["plan"], "side_effects": ["入队一个执行任务"], "cost": "model",
     "requires_human": False, "summary": "显式 scope/阶段/上限/幂等键地创建任务"},
    {"id": "read_task", "operationId": "ai_read_task", "method": "GET", "path": "/api/ai/tasks/{task_id}",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False, "summary": "查询任务状态"},
    {"id": "cancel_task", "operationId": "ai_cancel_task", "method": "POST",
     "path": "/api/ai/tasks/{task_id}/cancel", "prerequisites": [], "side_effects": ["请求停止后续操作"],
     "cost": "none", "requires_human": False, "summary": "取消任务；已发出请求等待结果"},
    {"id": "prepare_external_work", "operationId": "ai_prepare_work", "method": "POST",
     "path": "/api/ai/work", "prerequisites": ["plan"], "side_effects": ["生成工作项输入"],
     "cost": "none", "requires_human": False, "summary": "给外部 AI 输入快照、策略、schema、input_hash"},
    {"id": "submit_external_result", "operationId": "ai_submit_work", "method": "POST",
     "path": "/api/ai/work/{work_id}/submit", "prerequisites": ["work"], "side_effects": ["保存外部结果"],
     "cost": "none", "requires_human": False, "summary": "校验并保存外部结果；过期/缺岗/越界一律拒绝"},
    {"id": "start_interview", "operationId": "ai_start_interview", "method": "POST",
     "path": "/api/ai/interviews", "prerequisites": ["plan"], "side_effects": ["新建面试会话"],
     "cost": "none", "requires_human": False, "summary": "为某个岗位开一轮模拟面试"},
    {"id": "read_interview", "operationId": "ai_read_interview", "method": "GET",
     "path": "/api/ai/interviews/{session_id}", "prerequisites": [], "side_effects": [], "cost": "none",
     "requires_human": False, "summary": "读取会话、回合与下一步"},
    {"id": "answer_interview", "operationId": "ai_answer_interview", "method": "POST",
     "path": "/api/ai/interviews/{session_id}/turns", "prerequisites": ["interview"], "side_effects": ["追加回合"],
     "cost": "none", "requires_human": True, "summary": "记录一次提问/回答/反馈；带幂等键"},
    {"id": "resume_interview", "operationId": "ai_resume_interview", "method": "GET",
     "path": "/api/ai/interviews/resume", "prerequisites": [], "side_effects": [], "cost": "none",
     "requires_human": False, "summary": "接着上次没结束的会话"},
    {"id": "read_audit", "operationId": "ai_audit", "method": "GET", "path": "/api/ai/audit",
     "prerequisites": [], "side_effects": [], "cost": "none", "requires_human": False,
     "summary": "最近的配置/任务/提交审计"},
]

#: 明确"还没实现"的能力。清单里列出来，避免外部 AI 以为能调。
UNIMPLEMENTED = [
    {"id": "send_and_approve", "status": "existing_endpoint",
     "reason": "发送审核沿用原有哈希与台账", "next": ["GET /api/workflow/sendable", "POST /api/workflow/control/tasks"]},
]


def workspace():
    """取工作区；没配置时抛业务错误（由 app 统一翻成 JSON）。"""
    root = view.workflow_dir()
    if root is None:
        raise MissingConfiguration("没有配置工作区目录 BOSS_WORKFLOW_DIR")
    from boss_core.workspace import Workspace

    ws = Workspace(root)
    if not ws.exists():
        raise MissingConfiguration("工作区还没初始化（缺 workspace.json）",
                                   next_steps=["运行迁移工具，或调用 /api/ai/setup"])
    return ws


def _actor(x_actor: str | None) -> str:
    """自报操作者标签：只写审计，不作为认证依据。"""
    return (x_actor or "ai").strip()[:80] or "ai"


def _fail(exc: CoreError):
    """把核心错误翻成 HTTP（供 app 的异常处理器调用）。"""
    raise exc


# ------------------------------------------------------------------ 读上下文


@router.get("/context", tags=["ai"], operation_id="ai_context")
def ai_context(x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """服务身份 + 活动方案/资料 + 有效配置 + 缺失项 + 任务摘要。"""
    ws = workspace()
    meta = ws.meta()
    plan = ws.active_plan()
    saved_candidate = ws.active_candidate()
    candidate = saved_candidate
    snap: dict[str, Any] | None = None
    if plan is not None:
        try:
            snap = snapshot.build(ws)
            candidate = snap.get("candidate")
        except CoreError:
            snap = None
    store = TaskStore(ws.root)
    from .. import demo
    return {
        "ok": True,
        "contract": CONTRACT_VERSION,
        "mode": __import__("os").environ.get("BOSS_SERVE_MODE", "run"),
        'demo': demo.status(),
        "workspace": {"id": meta.get("id"), "name": meta.get("name"), "root": str(ws.root)},
        "active": {
            "candidate": _brief(candidate),
            "plan": _brief(plan),
            "effective_fingerprint": (snap or {}).get("fingerprint"),
        },
        "effective": _effective(snap),
        "saved_candidate": _brief(saved_candidate),
        "missing": (snap or {}).get("missing") or ([] if snap else ["active_plan"]),
        "stages": (snap or {}).get("stages") or {},
        "migration": meta.get("migration") or {},
        "tasks": {"total": len(store.list()), "latest": store.list()[:3]},
        "strategies": {
            "builtin": sorted(strategies_mod.BUILTIN),
            "registered": len(ws.list("strategy")),
        },
        "executors": {"llm:chat": True, "external_skill": True},
        "credentials": {"platform_login": 'not required: offline demo' if demo.enabled() else "check /api/auth/status",
                        "llm_key": "check /api/llm/config"},
        "actor": _actor(x_actor),
    }


def _brief(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {"id": version.get("id"), "revision": version.get("revision"),
            "label": version.get("label") or version.get("name"),
            "created_at": version.get("created_at")}


def _effective(snap: dict[str, Any] | None) -> dict[str, Any]:
    if not snap:
        return {}
    plan = snap.get("plan") or {}
    search = snap.get("search") or {}
    return {
        "candidate": {"id": (snap.get("candidate") or {}).get("id"),
                      "resume_id": ((snap.get("candidate") or {}).get("resume") or {}).get("resume_id")},
        "target": plan.get("target"),
        "salary": plan.get("salary"),
        "disclosure": plan.get("disclosure"),
        "runtime": {k: v for k, v in (plan.get("runtime") or {}).items() if k != "pricing"},
        "thresholds": snap.get("thresholds"),
        "search": {"keywords": search.get("keywords"), "base": search.get("base"),
                   "bases": search.get("bases"), "audit": search.get("audit"),
                   "filters_hash": search.get("filters_hash")},
        "versions": snap.get("versions"),
    }


@router.get("/manifest", tags=["ai"], operation_id="ai_manifest")
def ai_manifest() -> dict[str, Any]:
    """意图清单：外部 AI 先读它，再按 operationId 调具体接口。"""
    return {"ok": True, "contract": CONTRACT_VERSION, "operations": MANIFEST,
            "unimplemented": UNIMPLEMENTED,
            "errors": ["revision_conflict", "missing_configuration", "unsupported_strategy",
                       "stale_input", "needs_login", "external_result_invalid", "invalid_input",
                       "not_found", "environment"]}


@router.get("/capabilities", tags=["ai"], operation_id="ai_capabilities")
def ai_capabilities() -> dict[str, Any]:
    """定制能力清单：每个阶段能改什么、默认值、接口与示例、校验、生效位置、限制。

    这是与 `ai_manifest` 互补的另一半：manifest 说"能调哪些接口"，
    这里说"每个可定制项到底是什么、改完在哪生效、哪些做不到"。
    """
    from ..services.capabilities import catalog

    return catalog()


# ------------------------------------------------------------------ 候选人


class CandidateBody(BaseModel):
    """保存一份候选人资料版本（事实 + 结构化字段）。"""

    label: str = ""
    facts: list[dict[str, Any]] = Field(min_length=1, max_length=60)
    education: list[dict[str, Any]] = Field(default_factory=list)
    experiences: list[dict[str, Any]] = Field(default_factory=list)
    availability: dict[str, Any] = Field(default_factory=dict)
    scoring_material: dict[str, Any] | None = None
    resume_id: str | None = None
    source_sha: str | None = None
    expected_revision: str | None = None


@router.get("/candidates", tags=["ai"], operation_id="ai_list_candidates")
def ai_list_candidates() -> dict[str, Any]:
    """列出候选人资料版本。"""
    ws = workspace()
    active = (ws.active_candidate() or {}).get("id")
    return {"ok": True, "active": active,
            "items": [{**_brief(row), "facts": len(row.get("facts") or []),
                       "active": row.get("id") == active}
                      for row in ws.list("candidate")[::-1]]}


@router.post("/candidates", tags=["ai"], operation_id="ai_save_candidate")
def ai_save_candidate(body: CandidateBody,
                      x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """保存候选人资料并设为活动资料；事实编号必须唯一。"""
    ws = workspace()
    payload = body.model_dump()
    if payload.get("scoring_material") is None:
        payload.pop("scoring_material", None)
    expected = payload.pop("expected_revision", None)
    if body.resume_id:
        payload["resume"] = {"resume_id": body.resume_id, "source_sha": body.source_sha,
                             "raw_path": None, "title": None, "format": None}
    payload.pop("resume_id", None)
    payload.pop("source_sha", None)
    saved = ws.save_candidate(payload, actor=_actor(x_actor), base=expected)
    rebound = _rebind_active_plan(ws, saved["id"], _actor(x_actor))
    _invalidate_caches()
    return {"ok": True, "candidate": saved, "plan": rebound,
            "next": ["POST /api/ai/plans 或 PATCH 现有方案改条件"]}


class ScoringMaterialBody(BaseModel):
    """用户 AI 整理材料，用户核对后确认；不在面板自动摘要或推定事实。"""
    text: str = Field(min_length=1, max_length=12000)
    confirmed: bool
    resume_id: str
    source_sha: str
    expected_candidate: str


@router.put("/scoring-material", tags=["ai"], operation_id="ai_save_scoring_material")
def ai_save_scoring_material(body: ScoringMaterialBody,
                            x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    import copy
    from ..services import single_scoring
    from ..services.resume_store import load_resume
    ws = workspace()
    snap = snapshot.build(ws)
    candidate = snap['candidate']
    if candidate['id'] != body.expected_candidate:
        raise Conflict("候选人版本已变化，请重新读取材料", field="expected_candidate")
    if str((candidate.get('resume') or {}).get('resume_id') or '') != body.resume_id:
        raise InvalidInput("材料必须关联当前方案使用的简历", field="resume_id")
    try:
        resume = load_resume(body.resume_id)
    except FileNotFoundError as exc:
        raise NotFound("找不到关联简历", field="resume_id") from exc
    if single_scoring.source_sha(resume) != body.source_sha:
        raise Conflict("简历正文已变化，请重新核对材料", field="source_sha")
    if not body.text.strip():
        raise InvalidInput("评分用材料不能为空", field="text")
    payload = copy.deepcopy(candidate)
    payload['scoring_material'] = {'text': body.text.strip(), 'confirmed': body.confirmed,
                                  'resume_id': body.resume_id, 'source_sha': body.source_sha,
                                  'facts_sha': single_scoring.facts_sha(candidate)}
    saved = ws.save_candidate(payload, actor=_actor(x_actor), base=body.expected_candidate)
    rebound = _rebind_active_plan(ws, saved['id'], _actor(x_actor))
    _invalidate_caches()
    return {'ok': True, 'candidate_id': saved['id'], 'plan': rebound,
            'material': single_scoring.candidate_material(resume)}


def _rebind_active_plan(ws, candidate_id: str, actor: str) -> dict[str, Any] | None:
    """资料换了版本：把活动方案改绑到新版本并激活，旧方案版本原样保留。

    方案钉的是**具体某一版资料**。新资料存进来后如果方案还指着旧版本，用户会看到
    "我明明改了资料，打分却还是按旧的"；自动生成一版新方案（父版本不变、内容只改
    `candidate_id`）并激活，旧版本仍在，旧快照和旧任务照旧能用旧资料重放。
    """
    import copy

    plan = ws.active_plan()
    if plan is None or str(plan.get("candidate_id") or "") == candidate_id:
        return None
    body = copy.deepcopy(plan)
    body["candidate_id"] = candidate_id
    saved = ws.save_plan(body, actor=actor, parent=str(plan["id"]))
    ws.activate_plan(saved["id"], actor=actor, reason="资料新版本自动改绑")
    return {"id": saved["id"], "revision": saved.get("revision"),
            "parent": plan["id"], "candidate_id": candidate_id}


@router.get("/candidates/{candidate_id}", tags=["ai"], operation_id="ai_read_candidate")
def ai_read_candidate(candidate_id: str) -> dict[str, Any]:
    """读一份候选人资料版本。"""
    return {"ok": True, "candidate": workspace().get("candidate", candidate_id)}


# ------------------------------------------------------------------ 方案


class PlanCreate(BaseModel):
    """创建方案：从中性默认或复制当前方案。"""

    label: str = Field(default="", max_length=120)
    from_plan_id: str | None = None
    candidate_id: str | None = None
    target: dict[str, Any] | None = None


class PlanPatch(BaseModel):
    """局部更新；`expected_revision` 是乐观锁（当前活动方案 id）。"""

    expected_revision: str | None = None
    patch: dict[str, Any] = Field(default_factory=dict)


def _apply_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """深合并：只覆盖给出的键；显式 null 表示"清空为未知"。"""
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out[key] = None
        elif isinstance(value, dict) and isinstance(out.get(key), dict) and "mode" not in value:
            out[key] = _apply_patch(out[key], value)
        else:
            out[key] = value
    return out


@router.get("/plans", tags=["ai"], operation_id="ai_list_plans")
def ai_list_plans() -> dict[str, Any]:
    """列出全部方案版本（新在前）。"""
    ws = workspace()
    rows = ws.list("plan")[::-1]
    active = (ws.active_plan() or {}).get("id")
    return {"ok": True, "active": active,
            "items": [{**_brief(row), "status": row.get("status"),
                       "active": row.get("id") == active,
                       "candidate_id": row.get("candidate_id")} for row in rows]}


@router.post("/plans", tags=["ai"], operation_id="ai_create_plan")
def ai_create_plan(body: PlanCreate, x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """创建方案草稿：`from_plan_id` 给了就复制那一版，否则用中性模板。"""
    ws = workspace()
    candidate = ws.get("candidate", body.candidate_id) if body.candidate_id else ws.active_candidate()
    if candidate is None:
        raise MissingConfiguration("还没有候选人资料", field="candidate_id",
                                   next_steps=["先 POST /api/ai/candidates"])
    if body.from_plan_id:
        raw = dict(ws.get("plan", body.from_plan_id))
        raw.pop("id", None)
        raw.pop("revision", None)
        raw.pop("created_at", None)
        raw["candidate_id"] = candidate["id"]
        raw["status"] = "draft"
        parent = body.from_plan_id
    else:
        raw = default_plan()
        raw["candidate_id"] = candidate["id"]
        parent = None
    raw["label"] = body.label or raw.get("label") or "新方案"
    if body.target:
        raw["target"] = {**(raw.get("target") or {}), **body.target}
    plan = ws.save_plan(raw, actor=_actor(x_actor), parent=parent)
    return {"ok": True, "plan": plan, "next": ["PATCH /api/ai/plans/{id}", "POST .../validate"]}


@router.get("/plans/{plan_id}", tags=["ai"], operation_id="ai_read_plan")
def ai_read_plan(plan_id: str) -> dict[str, Any]:
    """读一份方案版本。"""
    ws = workspace()
    return {"ok": True, "plan": ws.get("plan", plan_id)}


@router.patch("/plans/{plan_id}", tags=["ai"], operation_id="ai_update_plan")
def ai_update_plan(plan_id: str, body: PlanPatch,
                   x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """局部更新草稿并生成新版本；`expected_revision` 不符就报冲突。"""
    ws = workspace()
    current = ws.get("plan", plan_id)
    updated = _apply_patch(current, body.patch)
    updated.pop("id", None)
    updated.pop("revision", None)
    updated.pop("created_at", None)
    saved = ws.save_plan(updated, actor=_actor(x_actor), base=body.expected_revision,
                         parent=plan_id)
    return {"ok": True, "plan": saved, "changed_fields": sorted(body.patch)}


@router.post("/plans/{plan_id}/validate", tags=["ai"], operation_id="ai_validate_plan")
def ai_validate_plan(plan_id: str) -> dict[str, Any]:
    """纯校验：不调模型、不发送。"""
    ws = workspace()
    plan = ws.get("plan", plan_id)
    snap = snapshot.build(ws, plan_id=plan_id)
    return {"ok": True, "valid": not snap["missing"], "missing": snap["missing"],
            "stages": snap["stages"], "fingerprint": snap["fingerprint"],
            "thresholds": snap["thresholds"], "search": {
                "keywords": snap["search"]["keywords"], "base": snap["search"]["base"],
                "bases": snap["search"]["bases"], "audit": snap["search"]["audit"]}}


@router.post("/plans/{plan_id}/preview", tags=["ai"], operation_id="ai_preview_plan")
def ai_preview_plan(plan_id: str) -> dict[str, Any]:
    """与当前活动方案的差异：字段级。"""
    ws = workspace()
    active = ws.active_plan()
    after = snapshot.build(ws, plan_id=plan_id)
    if active is None:
        return {"ok": True, "diff": [], "note": "当前没有活动方案，全部字段都算新增"}
    before = snapshot.build(ws, plan_id=active["id"])
    return {"ok": True, "diff": snapshot.effective_diff(before, after),
            "before": before["versions"], "after": after["versions"],
            "search_changed": (before["search"]["filters_hash"] != after["search"]["filters_hash"])}


@router.post("/plans/{plan_id}/activate", tags=["ai"], operation_id="ai_activate_plan")
def ai_activate_plan(plan_id: str, body: dict[str, Any] | None = None,
                     x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """激活方案；缺必需项时拒绝并说明缺什么。"""
    ws = workspace()
    snap = snapshot.build(ws, plan_id=plan_id)
    if snap["missing"]:
        raise MissingConfiguration("方案还缺必需项，不能激活：" + "、".join(snap["missing"]),
                                   field=snap["missing"][0], detail={"missing": snap["missing"]})
    reason = str((body or {}).get("reason") or "")
    result = ws.activate_plan(plan_id, actor=_actor(x_actor), reason=reason)
    _invalidate_caches()
    return {"ok": True, **result, "effective_revision": snap["versions"]["plan"],
            "pending_revision": None, "effective_from": "next_task"}


def _invalidate_caches() -> None:
    """方案/策略一变，执行链路的快照与面板报告缓存都要作废。"""
    try:
        from ..services import workflow_context

        workflow_context.invalidate()
    except Exception:  # noqa: BLE001
        pass
    try:
        view.clear_report_cache()
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 策略


class StrategyBody(BaseModel):
    """登记一个策略版本。

    本机 matching 可用 content.criteria（非空文本）保存初筛、详评共用的个人标准；
    设置后不再叠加 stage1.system/stage2.system；省略时兼容保留两处原文，一次评估。
    """

    stage: Literal["resume_revision", "matching", "greeting", "interview"]
    name: str = ""
    source: Literal["builtin", "custom_prompt", "external_skill"] = "custom_prompt"
    content: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    requires: list[str] = Field(default_factory=list)


@router.get("/strategies", tags=["ai"], operation_id="ai_list_strategies")
def ai_list_strategies() -> dict[str, Any]:
    """列出已登记策略与内置模板。"""
    ws = workspace()
    return {"ok": True, "builtin": sorted(strategies_mod.BUILTIN),
            "items": [{"id": row.get("id"), "stage": row.get("stage"), "name": row.get("name"),
                       "source": row.get("source"), "revision": row.get("revision")}
                      for row in ws.list("strategy")[::-1]]}


@router.post("/strategies", tags=["ai"], operation_id="ai_register_strategy")
def ai_register_strategy(body: StrategyBody,
                         x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """登记策略；不下载、不执行、不等于激活。"""
    ws = workspace()
    saved = ws.save_strategy(body.model_dump(), actor=_actor(x_actor))
    return {"ok": True, "strategy": saved,
            "next": [f"POST /api/ai/strategies/{saved['id']}/bind"]}


class BindBody(BaseModel):
    """绑定阶段到某版本方案。"""

    plan_id: str
    stage: Literal["resume_revision", "matching", "greeting", "interview"]
    expected_revision: str | None = None


@router.post("/strategies/{strategy_id}/bind", tags=["ai"], operation_id="ai_bind_strategy")
def ai_bind_strategy(strategy_id: str, body: BindBody,
                     x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """把策略绑到方案阶段，生成新方案版本（不自动激活）。"""
    ws = workspace()
    strategy = ws.get("strategy", strategy_id)
    if strategy.get("stage") != body.stage:
        raise InvalidInput("策略阶段与要绑定的阶段不一致",
                           field="stage", detail={"strategy": strategy.get("stage")})
    plan = ws.get("plan", body.plan_id)
    updated = {**plan, "strategies": {**(plan.get("strategies") or {}), body.stage: strategy_id}}
    for key in ("id", "revision", "created_at"):
        updated.pop(key, None)
    saved = ws.save_plan(updated, actor=_actor(x_actor), base=body.expected_revision,
                         parent=body.plan_id)
    return {"ok": True, "plan": saved, "bound": {body.stage: strategy_id}}


# ------------------------------------------------------------------ 条件判断 / 写作规则


class JudgeBody(BaseModel):
    """批量判断；一次最多 500 个岗位。"""

    jobs: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    plan_id: str | None = None


@router.post("/judge", tags=["ai"], operation_id="ai_judge")
def ai_judge(body: JudgeBody) -> dict[str, Any]:
    """按当前快照判断岗位条件，返回三态、依据和阻断项。"""
    ws = workspace()
    snap = snapshot.build(ws, plan_id=body.plan_id)
    results = []
    for job in body.jobs:
        verdict = snapshot.judge_job(job, snap)
        results.append({"job_id": job.get("job_id") or job.get("encrypt_job_id"),
                        "result": verdict["result"], "summary": verdict["summary"],
                        "blocking": verdict["blocking"], "unresolved": verdict["unresolved"],
                        "checks": verdict["checks"]})
    summary = {"pass": 0, "fail": 0, "unknown": 0}
    for row in results:
        summary[row["result"]] += 1
    return {"ok": True, "fingerprint": snap["fingerprint"], "summary": summary, "results": results}


@router.get("/stage-inputs/{stage}", tags=["ai"], operation_id="ai_stage_inputs")
def ai_stage_inputs(stage: Literal["matching", "greeting", "interview", "resume_revision"]) -> dict[str, Any]:
    """当前采用的实际输入模板，与执行使用同一套构建逻辑。"""
    from ..services.stage_inputs import read_stage_inputs

    return read_stage_inputs(stage)


@router.get("/greeting-rules", tags=["ai"], operation_id="ai_greeting_rules")
def ai_greeting_rules() -> dict[str, Any]:
    """有效写作规则：合成后的全文、逐段指纹、冲突。"""
    from ..services import workflow_context as wctx

    rules = wctx.greeting_rules()
    if rules is None:
        raise MissingConfiguration("还没有有效方案，无法合成写作规则", field="active_plan")
    return {"ok": True, "sha": rules["sha"], "max_chars": rules["max_chars"],
            "conflicts": rules["conflicts"], "deduplicated": rules.get("deduplicated", []), "parts": [
                {"id": p["id"], "source": p["source"], "chars": p["chars"], "sha": p["sha"]}
                for p in rules["parts"]],
            "text": rules["text"], "strategy_id": rules["strategy_id"]}


@router.get('/greeting-inputs/{job_id}', tags=['ai'], operation_id='ai_greeting_inputs')
def ai_greeting_inputs(job_id: str, analysis_id: str = '', text_sha: str = '') -> dict[str, Any]:
    """本地只读：写稿事实、岗位摘要/摘录、评分快照、实际规则与成稿。"""
    from ..services.greeting_inputs import read
    return read(workspace().root, job_id, analysis_id=analysis_id, text_sha=text_sha)


class PreviewStageBody(BaseModel):
    """样例评分：只跑 1–3 个岗位，不写库、不发送。"""

    job_ids: list[str] = Field(min_length=1, max_length=3)


@router.post("/preview-stage", tags=["ai"], operation_id="ai_preview_stage")
def ai_preview_stage(body: PreviewStageBody,
                     x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """对少量岗位真跑一次评分，返回样例结果（会产生模型调用成本）。"""
    from ..services import match_task
    from ..services.llm_client import LLMClient
    from ..services.llm_config_store import load_config
    from ..services.resume_store import load_llm_parse, load_resume

    ws = workspace()
    snap = snapshot.build(ws)
    cfg = load_config()
    if not getattr(cfg, "configured", False):
        raise MissingConfiguration("模型还没配置，样例评分需要先配 API Key / Base URL / 模型名",
                                   field="llm", next_steps=["PUT /api/llm/config"])
    resume_id = (snap.get("candidate", {}).get("resume") or {}).get("resume_id")
    if not resume_id:
        raise MissingConfiguration("活动候选人没有关联简历", field="resume_id")
    try:
        resume = load_resume(resume_id)
    except FileNotFoundError as exc:
        raise MissingConfiguration(f"找不到简历：{resume_id}") from exc

    from ..services import single_scoring
    ctx = single_scoring.context(resume)
    client = LLMClient(cfg)
    jobs = [match_task._load_job(jid) for jid in dict.fromkeys(body.job_ids)]
    present = [job for job in jobs if job is not None]
    try:
        evaluated = single_scoring.evaluate_batch(client, ctx['material']['text'], present, ctx=ctx) if present else {}
    except Exception as exc:
        evaluated = {job.encrypt_job_id: single_scoring.failure(str(exc)) for job in present}
    results = []
    for jid, job in zip(dict.fromkeys(body.job_ids), jobs):
        item = evaluated.get(jid) if job is not None else None
        if item is None:
            results.append({'job_id': jid, 'ok': False, 'error': '库里没有这个岗位'})
        else:
            results.append({'job_id': jid, 'ok': item.get('status') != 'failed', **item,
                'strategy': {'id': (snap.get('strategies', {}).get('matching') or {}).get('id'),
                             'prompt': snap.get('versions', {}).get('matching')}})
    ws.audit(_actor(x_actor), "stage.preview", ",".join(body.job_ids),
             {"ok": sum(1 for r in results if r["ok"]), "total": len(results)})
    return {"ok": True, "fingerprint": snap["fingerprint"], "executes": True,
            "note": "样例评分不写库、不进队列、不发送；写库请用 POST /api/ai/tasks",
            "results": results}


# ------------------------------------------------------------------ 任务


class TaskBody(BaseModel):
    """创建任务：scope 必须显式。"""

    stage: Literal["collect", "matching", "greeting", "send", "feedback"]
    scope: Literal["selected", "all"] = "selected"
    job_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=150)
    plan_id: str | None = None
    idempotency_key: str | None = None


_ACTION = {"collect": "collect", "matching": "match", "greeting": "prepare",
           "send": "send", "feedback": "feedback"}


@router.post("/tasks", tags=["ai"], operation_id="ai_create_task")
def ai_create_task(body: TaskBody, x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """创建执行任务；创建时就把岗位集合定死，重试靠幂等键不会多跑一遍。

    `scope=all` 不等于"范围随便"：创建时按 `limit` 取定具体岗位写进 payload，
    执行器按这份名单跑。拿不到名单时也会把 `limit` 一起传下去，绝不退回全库无限跑。
    """
    ws = workspace()
    snap = snapshot.build(ws, plan_id=body.plan_id)
    if body.scope == "selected" and not body.job_ids:
        raise InvalidInput("scope=selected 时必须给 job_ids；要全库请显式用 scope=all",
                           field="job_ids")
    jobs = _frozen_jobs(ws, snap, body)
    action = _ACTION[body.stage]
    if action == "send":
        raise Conflict("发送要人工审核：先 /api/workflow/sendable 预览，再由面板提交 approved",
                       field="stage", next_steps=["用 /api/workflow/control/tasks 提交 approved"])
    request_hash = digest({"stage": body.stage, "scope": body.scope,
                           "job_ids": sorted(str(x) for x in body.job_ids),
                           "limit": body.limit, "plan_id": body.plan_id or ""})
    payload = {
        "idempotency_key": body.idempotency_key or "",
        "request_hash": request_hash,
        "scope": body.scope,
        "job_ids": jobs,
        "only": jobs,
        "limit": body.limit,
        "profile_version": _profile_version(snap),
        "plan_id": snap["plan"]["id"],
        "snapshot_fingerprint": snap["fingerprint"],
    }
    store = TaskStore(ws.root)
    if body.idempotency_key:
        task, created = store.add_idempotent(action, payload, body.idempotency_key)
        if not created:
            if str((task.get("payload") or {}).get("request_hash") or "") != request_hash:
                raise Conflict("这个幂等键已经用在另一份任务内容上了", field="idempotency_key",
                               detail={"task_id": task["id"], "status": task["status"]})
            return {"ok": True, "task": task, "action": action, "jobs": len(jobs),
                    "duplicate": True,
                    "note": "同一幂等键、同一内容：返回已有任务，不重复执行"}
    else:
        task = store.add(action, payload)
    ws.audit(_actor(x_actor), "task.create", str(task["id"]),
             {"stage": body.stage, "scope": body.scope, "limit": body.limit,
              "jobs": len(jobs)})
    return {"ok": True, "task": task, "action": action, "jobs": len(jobs)}


def _frozen_jobs(ws, snap: dict[str, Any], body: "TaskBody") -> list[str]:
    """创建任务时冻结岗位集合：执行阶段不再有"按全库重新选一遍"的机会。"""
    if body.scope == "selected":
        ids = [str(x).strip() for x in body.job_ids if str(x).strip()]
        if len(ids) > body.limit:
            raise InvalidInput(f"勾了 {len(ids)} 个岗位，超过 limit={body.limit}",
                               field="limit",
                               next_steps=[f"把 limit 调到 {len(ids)} 以上，或减少勾选"],
                               detail={"selected": len(ids), "limit": body.limit})
        return ids
    if body.stage not in {"collect", "matching"}:
        # 写稿/发送的范围由待审队列和 approved 决定，不在这里冻结
        return []
    resume_id = str(((snap.get("candidate") or {}).get("resume") or {}).get("resume_id") or "")
    try:
        return view.unscored_job_ids(resume_id, limit=body.limit)
    except Exception:  # noqa: BLE001 - 库读不到时不擅自改成"全库跑"
        return []


def _profile_version(snap: dict[str, Any]) -> str:
    """投递侧版本号：沿用迁移前的 profile-*，改了事实才变。"""
    candidate = snap.get("candidate") or {}
    legacy = candidate.get("legacy") or {}
    return str(legacy.get("profile_version") or candidate.get("id") or "")


@router.get("/tasks", tags=["ai"], operation_id="ai_list_tasks")
def ai_list_tasks(limit: int = 20) -> dict[str, Any]:
    """最近任务。"""
    ws = workspace()
    return {"ok": True, "items": TaskStore(ws.root).list()[:max(1, min(limit, 100))]}


@router.get("/tasks/{task_id}", tags=["ai"], operation_id="ai_read_task")
def ai_read_task(task_id: str) -> dict[str, Any]:
    """按 task_id 查任务状态与结果位置。"""
    ws = workspace()
    store = TaskStore(ws.root)
    for row in store.list():
        if row["id"] == task_id:
            return {"ok": True, "task": row,
                    "log": str(ws.root / "runs" / "panel-jobs" / f"{task_id}.log")}
    raise NotFound(f"找不到任务：{task_id}")


@router.post("/tasks/{task_id}/cancel", tags=["ai"], operation_id="ai_cancel_task")
def ai_cancel_task(task_id: str, x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """取消任务；已发出的请求不会被强行撤回。"""
    ws = workspace()
    result = TaskStore(ws.root).cancel(task_id)
    ws.audit(_actor(x_actor), "task.cancel", task_id, result)
    return {"ok": True, **result,
            "note": "已发出的招呼语请求等待结果；不会重复发送"}


# ------------------------------------------------------------------ 外部工作项


class WorkBody(BaseModel):
    """准备工作项。"""

    stage: Literal["matching", "greeting", "interview", "resume_revision"]
    job_ids: list[str] = Field(min_length=1, max_length=50)
    plan_id: str | None = None
    session_id: str | None = None


@router.post("/work", tags=["ai"], operation_id="ai_prepare_work")
def ai_prepare_work(body: WorkBody, x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """生成外部工作项：输入快照、策略、schema、input_hash。

    这里用 `strategies.describe` 而不是 `resolve`：**准备输入**允许外部技能
    （否则绑了外部策略反而无法准备工作项，形成死路）；能不能在本机跑模型，
    由执行侧用 `resolve` 判断，不在这一步替用户拒绝。
    """
    ws = workspace()
    snap = snapshot.build(ws, plan_id=body.plan_id)
    stage = _strategy_stage(body.stage)
    strategy = strategies_mod.describe(snap, stage)
    jobs = [_job_for(ws, jid) for jid in body.job_ids]
    analysis_id = _analysis_for(body.stage, jobs)
    work = external.create(ws, stage=body.stage, jobs=jobs, snapshot=snap,
                           strategy=strategy, analysis_id=analysis_id,
                           session_id=body.session_id, actor=_actor(x_actor))
    local = bool(strategy.get("executes_locally"))
    return {"ok": True, "work": work,
            "execution": "local" if local else "host_ai",
            "note": ("本阶段策略由本机执行；外部 AI 只做复核" if local else
                     "本阶段绑定了外部技能：程序只准备输入，执行与出稿由宿主 AI 完成，"
                     "结果经 submit 写回正式存储"),
            "next": ["POST /api/ai/work/{work_id}/submit"]}


def _analysis_for(stage: str, jobs: list[dict[str, Any]]) -> str | None:
    """招呼语要挂在一份已有分析上：没有匹配结果就没有正式落点。

    面试不走这里（它挂的是会话 id），改稿也不挂分析。
    """
    if stage != "greeting":
        return None
    from ..services.resume_store import match_index

    index = match_index()
    ids = []
    for job in jobs:
        row = index.get(str(job.get("job_id"))) or {}
        if row.get("analysis_id"):
            ids.append(str(row["analysis_id"]))
    if not ids:
        raise MissingConfiguration("这个岗位还没有匹配结果，先跑 matching 再准备这个阶段",
                                   field="analysis_id",
                                   next_steps=["POST /api/ai/work（stage=matching）并提交结果",
                                               "或 POST /api/ai/tasks（stage=matching）"])
    return ids[0]


def _strategy_stage(stage: str) -> str:
    return "interview" if stage == "interview" else stage


def _job_for(ws, job_id: str) -> dict[str, Any]:
    """从语料/库里取岗位；取不到就报错，不编空 JD。"""
    from boss_db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT encrypt_job_id, job_name, brand_name, job_desc FROM jobs WHERE encrypt_job_id=?",
            (job_id,)).fetchone()
    if row is None:
        raise NotFound(f"库里没有这个岗位：{job_id}", field="job_ids")
    return {"job_id": row["encrypt_job_id"], "title": row["job_name"],
            "company": row["brand_name"], "jd": row["job_desc"]}


@router.get("/work", tags=["ai"], operation_id="ai_list_work")
def ai_list_work() -> dict[str, Any]:
    """最近的工作项摘要。"""
    ws = workspace()
    return {"ok": True, "items": external.list_works(ws)}


@router.post("/work/{work_id}/submit", tags=["ai"], operation_id="ai_submit_work")
def ai_submit_work(work_id: str, payload: dict[str, Any],
                   x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """提交外部结果：校验后**写进正式业务存储**，不是只留一份 JSON。

    有效性由服务端重算（工作项记录的方案是否仍是活动方案、快照指纹、岗位 JD 指纹），
    不依赖调用方自报；过期结果只留档，返回 `stale_input`，不会被准入或发送消费。
    """
    from ..services.external_apply import apply_result

    ws = workspace()
    result = external.submit(
        ws, work_id, payload, actor=_actor(x_actor),
        jd_lookup=lambda jid: _job_for(ws, jid).get("jd") or "",
        apply=apply_result,
    )
    _invalidate_caches()
    return result


# ------------------------------------------------------------------ 面试


class InterviewBody(BaseModel):
    """开一轮模拟面试。"""

    job_id: str
    plan_id: str | None = None


@router.post("/interviews", tags=["ai"], operation_id="ai_start_interview")
def ai_start_interview(body: InterviewBody,
                       x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """新建会话并返回下一步。"""
    ws = workspace()
    snap = snapshot.build(ws, plan_id=body.plan_id)
    strategy = (snap.get("strategies") or {}).get("interview") or {}
    session = interview.create(ws, job_id=body.job_id, snapshot=snap,
                               strategy_id=strategy.get("id"), actor=_actor(x_actor))
    return {"ok": True, "session": interview.state(ws, session["id"])}


@router.get("/interviews", tags=["ai"], operation_id="ai_list_interviews")
def ai_list_interviews(job_id: str | None = None) -> dict[str, Any]:
    """最近会话。"""
    ws = workspace()
    return {"ok": True, "items": interview.list_sessions(ws, job_id=job_id)}


@router.get("/interviews/resume", tags=["ai"], operation_id="ai_resume_interview")
def ai_resume_interview(job_id: str | None = None) -> dict[str, Any]:
    """接着没结束的会话；没有就 `session: null`。"""
    ws = workspace()
    return {"ok": True, "session": interview.resume(ws, job_id=job_id)}


@router.get("/interviews/{session_id}", tags=["ai"], operation_id="ai_read_interview")
def ai_read_interview(session_id: str) -> dict[str, Any]:
    """会话全文与下一步。"""
    return {"ok": True, "session": interview.state(workspace(), session_id)}


class QuestionsBody(BaseModel):
    """给会话写入题库（模型生成或外部 AI 提交）。"""

    questions: list[dict[str, Any]] = Field(min_length=1, max_length=50)


@router.post("/interviews/{session_id}/questions", tags=["ai"], operation_id="ai_interview_questions")
def ai_interview_questions(session_id: str, body: QuestionsBody,
                           x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """写入题目；已问过的顺序不变，新题追加在后。"""
    ws = workspace()
    session = interview.set_questions(ws, session_id, body.questions, actor=_actor(x_actor))
    return {"ok": True, "session": interview.state(ws, session["id"])}


class TurnBody(BaseModel):
    """追加一轮；`key` 是幂等键。"""

    role: Literal["interviewer", "user", "feedback", "system"]
    text: str = Field(min_length=1, max_length=4000)
    key: str | None = None
    question_index: int | None = None


@router.post("/interviews/{session_id}/turns", tags=["ai"], operation_id="ai_answer_interview")
def ai_answer_interview(session_id: str, body: TurnBody,
                        x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """记录提问/回答/反馈；同一个 key 重复提交只算一次。"""
    ws = workspace()
    result = interview.add_turn(ws, session_id, role=body.role, text=body.text,
                                key=body.key, actor=_actor(x_actor),
                                question_index=body.question_index)
    return {"ok": True, **result, "state": interview.state(ws, session_id)}


@router.post("/interviews/{session_id}/finish", tags=["ai"], operation_id="ai_finish_interview")
def ai_finish_interview(session_id: str, body: dict[str, Any] | None = None,
                        x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """结束会话。"""
    ws = workspace()
    session = interview.finish(ws, session_id, actor=_actor(x_actor),
                               reason=str((body or {}).get("reason") or ""))
    return {"ok": True, "session": session}


# ------------------------------------------------------------------ 审计


@router.get("/audit", tags=["ai"], operation_id="ai_audit")
def ai_audit(limit: int = 100) -> dict[str, Any]:
    """最近审计记录。"""
    ws = workspace()
    return {"ok": True, "items": ws.audit_rows(limit=max(1, min(limit, 500)))}


# ------------------------------------------------------------------ 启动 / 初始化


class SetupBody(BaseModel):
    """工作区初始化（新用户首次设置）。"""

    name: str = ""
    migrate: bool = False


@router.post("/setup", tags=["ai"], operation_id="ai_setup")
def ai_setup(body: SetupBody, x_actor: str | None = Header(default=None, alias="X-Actor")) -> dict[str, Any]:
    """创建空工作区；`migrate=true` 时把旧 config/greeting 迁成个人方案。"""
    from boss_core import migration
    from boss_core.workspace import Workspace

    root = view.workflow_dir()
    if root is None:
        raise MissingConfiguration("没有配置工作区目录 BOSS_WORKFLOW_DIR")
    if body.migrate:
        return migration.run(root, actor=_actor(x_actor))
    ws = Workspace.create(root, name=body.name, actor=_actor(x_actor))
    return {"ok": True, "workspace": ws.meta()}
