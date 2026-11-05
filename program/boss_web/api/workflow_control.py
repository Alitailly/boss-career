"""Human-facing controls for the existing workflow; no alternate sending path."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Literal
import uuid

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from . import workflow as view
from ..errors import ConflictError, ValidationWebError
from ..services import authz, auto_send
from ..services.resume_store import list_resumes, load_resume, load_llm_parse, load_analysis
from ..services.workflow_tasks import TaskStore

router = APIRouter()


@router.get("/deliveries")
def deliveries():
    from ..services.workflow_history import delivery_history
    root, _ = paths()
    return delivery_history(root / 'greeting/queue.jsonl', analysis_loader=load_analysis)


def paths():
    """执行阶段要用的两个目录：状态目录 + 代码目录。缺一样都不给跑，但要说明缺哪样。"""
    root = view.workflow_dir()
    if root is None or not view.workspace_ready(root):
        raise ValidationWebError(
            "还没有可用的工作区：先到「设置」页创建或绑定一个工作区目录")
    code = view.workflow_code_dir(root)
    if code is None:
        from ..services import workspace_setup

        state = workspace_setup.current()
        lacking = [str(item.get("message") or "") for item in state.get("missing") or []]
        actions = [str(item.get("action") or "") for item in state.get("missing") or []]
        detail = "；".join(x for x in lacking if x) or "还没有配置工作流的代码目录"
        todo = "；".join(x for x in actions if x)
        raise ValidationWebError("还不能执行阶段：" + detail,
                                 detail={"missing": state.get("missing") or [],
                                         "next": todo,
                                         "workspace": state.get("dir"),
                                         "stage": state.get("stage")})
    return root, code


def read_root() -> Path:
    """读现状用的状态目录：有工作区就行，**不要求代码目录**。

    以前 `context` / `recipe` 也走 `paths()`，于是新建的工作区光想看一眼前配置
    就 422——"看现状"和"跑阶段"是两个门槛，不该共用一道。缺的配置文件按空处理，
    "还没配"不是错误。
    """
    root = view.workflow_dir()
    if root is None or not view.workspace_ready(root):
        raise ValidationWebError(
            "还没有可用的工作区：先到「设置」页创建或绑定一个工作区目录",
            detail={"next": "填一个目录并点「创建/绑定」"})
    return root


def read_optional(path: Path) -> dict:
    """可选配置文件：不存在就当还没配（空字典），不报错；存在但读坏了照旧报错。"""
    if not path.is_file():
        return {}
    return read_object(path)


def read_object(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("必须是 JSON 对象")
        return value
    except (OSError, ValueError) as exc:
        raise ValidationWebError(f"读取 {path.name} 失败：{exc}") from exc


def save_versioned(root: Path, relative: str, value: dict, expected: str):
    path = root / relative
    store = TaskStore(root)
    # Serialize panel edits and check optimistic version. Existing script readers see atomic files.
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = read_optional(path)
        if str(current.get("version") or "") != expected:
            raise ConflictError("设置已被其他操作更新，请刷新后重新确认")
        history = root / "runs" / "panel-config-history"
        history.mkdir(parents=True, exist_ok=True)
        # 内容没变就不产生新版本：原样保存一次表单不该把已有稿子全标成「口径/资料变了要重写」
        incoming = {k: v for k, v in value.items() if k != "version"}
        existing = {k: v for k, v in current.items() if k != "version"}
        if incoming == existing:
            return {**incoming, "version": current.get("version") or ""}
        stamp = uuid.uuid4().hex
        (history / f"{path.stem}-{stamp}.json").write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        value["version"] = f"{path.stem}-panel-{stamp[:12]}"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + stamp + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
    view._REPORT_CACHE.clear()
    try:
        from ..services import workflow_context

        workflow_context.invalidate()
    except Exception:  # noqa: BLE001 - 缓存清不掉不影响保存结果
        pass
    return value


@router.get("/context")
def context():
    """当前配置现状。新工作区里没有旧配置文件时按空返回，不报错。"""
    root = read_root()
    profile = read_optional(root / "greeting/profile.json")
    config = read_optional(root / "config/workflow.json")
    search = read_optional(root / "config/search.json")
    from ..services.workflow_context import effective_profile
    effective = effective_profile(root)
    return {"ok": True, "resumes": list_resumes(), "profile": profile, "config": config,
            "search": search,
            "effective_profile": effective,
            "active_resume_id": effective.get("resume_id") if effective.get("active_for_workflow") else config.get("resume_id"),
            "note": "active_resume_id/effective_profile 是评分、写稿实际采用的资料；"
                    "profile/config/search 保留旧结构镜像，没有就是空对象。"}


def resume_facts(data: dict):
    rows = []
    def add(text):
        if text.strip():
            rows.append({"id": f"F{len(rows) + 1}", "text": text.strip()[:500]})
    for item in data.get("education") or []:
        add("教育经历：" + "；".join(str(item.get(k) or "") for k in ("degree", "major", "period")))
    for category, label in (("work", "工作经历"), ("project", "项目经历（请确认归属）")):
        for item in data.get(category) or []:
            parts = [str(item.get(k) or "") for k in ("company", "name", "title", "role", "period")]
            parts += [str(x) for x in item.get("highlights") or []]
            add(label + "：" + "；".join(x for x in parts if x))
    if data.get("skills"):
        add("简历列出的技能（熟练程度需本人确认）：" + "、".join(data["skills"]))
    return rows[:30]


@router.get("/resume/{resume_id}/preview")
def resume_preview(resume_id: str):
    paths()
    try:
        resume = load_resume(resume_id)
        parsed = load_llm_parse(resume_id)
    except FileNotFoundError as exc:
        raise ValidationWebError("简历不存在") from exc
    if not parsed or not isinstance(parsed.get("data"), dict):
        raise ValidationWebError("这份简历尚未解析，请先打开简历页完成解析")
    root, _ = paths()
    profile = read_optional(root / 'greeting/profile.json')
    return {"resume_id": resume_id, "source_sha": hashlib.sha256(resume.raw.encode()).hexdigest(),
            "source_text": resume.raw, "source_title": resume.title,
            "active_facts": profile.get('facts', []) if profile.get('active_for_workflow') and profile.get('resume_id') == resume_id else [],
            "parsed": parsed, "facts": resume_facts(parsed["data"])}


class Fact(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    text: str = Field(min_length=1, max_length=500)


class CandidateBody(BaseModel):
    resume_id: str
    source_sha: str
    expected_version: str
    facts: list[Fact] = Field(min_length=1, max_length=30)
    confirmed: Literal[True]


@router.put("/candidate")
def activate_candidate(body: CandidateBody):
    root, _ = paths()
    preview = resume_preview(body.resume_id)
    if preview["source_sha"] != body.source_sha:
        raise ConflictError("简历原文已变，请重新预览并确认")
    facts = [f.model_dump() for f in body.facts]
    if len({f["id"] for f in facts}) != len(facts) or any(not f["text"].strip() for f in facts):
        raise ValidationWebError("事实编号必须唯一，内容不能为空")
    profile = {"resume_id": body.resume_id, "source_sha": body.source_sha,
               "source": "用户确认的简历解析", "confirmed_at": time.time(),
               "active_for_workflow": True, "facts": facts}
    saved = save_versioned(root, "greeting/profile.json", profile, body.expected_version)
    # 确认简历 = 换候选人版本；执行链路读的是这一份，不是 greeting/profile.json
    from boss_core.workspace import Workspace

    ws = Workspace(root)
    if ws.exists():
        plan = ws.active_plan() or {}
        current = ws.get("candidate", plan["candidate_id"]) if plan.get("candidate_id") else ws.active_candidate() or {}
        candidate = {
            **{k: v for k, v in current.items()
               if k not in ("id", "revision", "parent", "created_at", "created_by")},
            "label": current.get("label") or "面板确认的简历",
            "resume": {"resume_id": body.resume_id, "source_sha": body.source_sha,
                       "raw_path": None, "title": None, "format": "markdown"},
            "facts": facts,
            "legacy": {**(current.get("legacy") or {}),
                       "profile_version": saved.get("version")},
        }
        saved_candidate = ws.save_candidate(candidate, actor="panel")
        apply_to_plan(root, {"candidate_id": saved_candidate["id"]},
                      reason="用户确认采用简历资料")
        from ..services import workflow_context

        workflow_context.invalidate()
    return {"ok": True, "profile": saved, "message": "匹配和写稿已使用此简历；旧稿按资料版本重新检查"}


class SettingsBody(BaseModel):
    expected_version: str
    min_score: int = Field(ge=0, le=100)
    daily_cap: int = Field(ge=1, le=150)
    chunk: int = Field(ge=1, le=25)
    chunk_gap: int = Field(ge=0, le=86400)
    batch_size: int = Field(ge=1, le=15)


def _deep_merge(base: dict, patch: dict) -> dict:
    """深合并；带 `mode` 的选择器是叶子，整体替换。"""
    out = dict(base)
    for key, value in patch.items():
        if value is None:
            out[key] = None
        elif isinstance(value, dict) and isinstance(out.get(key), dict) and "mode" not in value:
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def apply_to_plan(root: Path, updates: dict, *, actor: str = "panel", reason: str = "") -> dict | None:
    """把面板改的东西同时落到**活动方案**。

    面板这几个端点历史上是直接写 `config/*.json` 的；执行链路现在读方案快照，
    只写文件等于"改了不生效"。这里生成一个新方案版本并激活，文件写入保留作兼容记录。
    """
    from boss_core.jsonio import digest
    from boss_core.workspace import Workspace

    ws = Workspace(root)
    if not ws.exists():
        return None
    plan = ws.active_plan()
    if plan is None:
        return None
    merged = _deep_merge(plan, updates)
    before = digest({k: v for k, v in plan.items() if k not in ("created_at", "revision", "id")})
    after = digest({k: v for k, v in merged.items() if k not in ("created_at", "revision", "id")})
    if before == after:
        return plan
    payload = {k: v for k, v in merged.items() if k not in ("id", "revision", "created_at")}
    saved = ws.save_plan(payload, actor=actor, parent=plan["id"])
    ws.activate_plan(saved["id"], actor=actor, reason=reason or "面板修改")
    try:
        from ..services import workflow_context

        workflow_context.invalidate()
    except Exception:  # noqa: BLE001
        pass
    return saved


@router.put("/settings")
def settings(body: SettingsBody):
    root, _ = paths()
    current = read_object(root / "config/workflow.json")
    current.update(body.model_dump(exclude={"expected_version"}))
    saved = save_versioned(root, "config/workflow.json", current, body.expected_version)
    plan = apply_to_plan(root, {"runtime": {
        "min_score": int(body.min_score), "daily_cap": int(body.daily_cap),
        "chunk": int(body.chunk), "chunk_gap": int(body.chunk_gap),
        "batch_size": int(body.batch_size)}}, reason="改准入分数/额度/批次")
    return {"ok": True, "config": saved, "plan": (plan or {}).get("id")}


class SearchBody(BaseModel):
    expected_version: str
    queries: list[str] = Field(min_length=1, max_length=50)

@router.put("/search")
def search(body: SearchBody):
    root, _ = paths()
    queries = list(dict.fromkeys(x.strip() for x in body.queries if x.strip()))
    if not queries or any(len(x) > 100 for x in queries):
        raise ValidationWebError("请填写有效搜索词，每个不超过 100 字")
    current = read_object(root / "config/search.json")
    current["queries"] = queries
    saved = save_versioned(root, "config/search.json", current, body.expected_version)
    plan = apply_to_plan(root, {"search": {"keywords": queries}}, reason="改搜索词")
    return {"ok": True, "search": saved, "plan": (plan or {}).get("id")}


#: 采集器可调项：键 → (下限, 上限, 名称, 单位, 一句话作用)
#: 只放"改了确实影响行为"的键；数值范围是保护值，不是产品定义。
COLLECTOR_FIELDS: dict[str, tuple[int, int, str, str, str]] = {
    "pages_per_query": (1, 5, "每轮翻页", "页", "一轮抓几页；越大越快，也越容易撞风控"),
    "cycle_sleep_sec": (60, 3600, "轮间隔", "秒", "两轮之间睡多久"),
    "daily_page_cap": (0, 2000, "每日页数上限", "页", "到上限后当天只消化积压"),
    "jd_batch": (1, 20, "每轮补 JD", "个", "新岗位要先有 JD 才能打分"),
    "jd_interval": (1, 30, "补 JD 间隔", "秒", "每补一个详情之间的间隔"),
    "daily_jd_cap": (0, 5000, "每日补 JD 上限", "个", "0 = 当天不补"),
    "activity_batch": (0, 20, "每轮探活跃", "个", "给够分岗位补「最近活跃」时间"),
    "activity_interval": (1, 60, "探活跃间隔", "秒", "每探一个之间的间隔"),
    "match_batch": (1, 200, "每批打分", "个", "一次交给匹配的岗位数"),
    "match_force_backlog": (0, 500, "高峰也跑的积压阈值", "个", "积压超过它，高峰时段也打分"),
    "corpus_export_every": (0, 200, "语料库复核间隔", "轮", "库没变化时也定期同步一次"),
}


@router.get("/recipe")
def recipe():
    """每个任务按什么条件跑：参数、当前值、可调范围、来源文件。

    面板要能回答"我能调什么"，所以把可调项和它的上下限一起给出去；
    只读的规则（200 字上限、日薪门槛、高峰窗口）也列出来，免得人去代码里找。
    """
    root = read_root()
    wf = read_optional(root / "config/workflow.json")
    search = read_optional(root / "config/search.json")
    base = search.get("base") or {}
    collect_params = [
        {"key": k, "label": label, "unit": unit, "min": lo, "max": hi, "hint": hint,
         "value": search.get(k)}
        for k, (lo, hi, label, unit, hint) in COLLECTOR_FIELDS.items()
    ]
    return {
        "ok": True,
        "collector": {
            "version": search.get("version"),
            "params": collect_params,
            "queries": list(search.get("queries") or []),
            "filter": {
                "experience": list(base.get("experience") or []),
                "degree": list(base.get("degree") or []),
                "scale": list(base.get("scale") or []),
                "salary": base.get("salary") or "不限",
                "max_pages_per_query": (search.get("pages") or {}).get("max_pages_per_query"),
            },
        },
        "run": {
            "version": wf.get("version"),
            "min_score": wf.get("min_score"),
            "daily_cap": wf.get("daily_cap"),
            "chunk": wf.get("chunk"),
            "chunk_gap": wf.get("chunk_gap"),
            "batch_size": wf.get("batch_size"),
            "max_delivery_attempts": wf.get("max_delivery_attempts"),
            "salary_gate": wf.get("salary_gate") or {},
            "pricing": wf.get("pricing") or {},
        },
        "rounds": wf.get("rounds") or {},
    }


class CollectorBody(BaseModel):
    expected_version: str
    values: dict[str, int] = Field(default_factory=dict)
    queries: list[str] | None = None


@router.put("/collector")
def save_collector(body: CollectorBody):
    """改采集器参数（写入 config/search.json；采集器在下一轮开始时热重载）。

    这些键以前只能进代码改，面板上看不见也调不了——用户的原话是
    "只知道有这些任务，不知道可以调节什么"。
    """
    root, _ = paths()
    current = read_object(root / "config/search.json")
    for key, value in body.values.items():
        if key not in COLLECTOR_FIELDS:
            raise ValidationWebError(f"不认识的采集参数：{key}")
        lo, hi, label, _unit, _hint = COLLECTOR_FIELDS[key]
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValidationWebError(f"{label}要是整数") from None
        if not lo <= number <= hi:
            raise ValidationWebError(f"{label}要在 {lo}–{hi} 之间")
        current[key] = number
    if body.queries is not None:
        queries = list(dict.fromkeys(x.strip() for x in body.queries if x.strip()))
        if not queries or any(len(x) > 100 for x in queries):
            raise ValidationWebError("请填写有效搜索词，每个不超过 100 字")
        current["queries"] = queries
    saved = save_versioned(root, "config/search.json", current, body.expected_version)
    updates: dict = dict(body.values)
    if body.queries is not None:
        updates["keywords"] = current["queries"]
    plan = apply_to_plan(root, {"search": updates}, reason="改采集参数")
    return {"ok": True, "search": saved, "note": "采集器下一轮开始生效",
            "plan": (plan or {}).get("id")}


DEFAULT_PREFERENCES = {
    "version": "prefs-empty",
    "updated_at": "",
    "updated_by": "",
    "start": "",
    "days_per_week": None,
    "months": None,
    "cities": [],
    "remote": None,
    "note": "个人求职口径：只影响招呼语里怎么写可用时间/城市，不改简历事实；留空 = 不提这项。",
}


def normalise_preferences(data: dict | None) -> dict:
    """把磁盘上的口径补齐成固定形状（缺字段按"没配置"处理，不猜值）。"""
    value = {**DEFAULT_PREFERENCES, **(data or {})}
    value["start"] = str(value.get("start") or "").strip()[:40]
    for key, low, high in (("days_per_week", 1, 7), ("months", 1, 36)):
        num = value.get(key)
        value[key] = int(num) if isinstance(num, int) and not isinstance(num, bool) and low <= num <= high else None
    value["cities"] = [str(x).strip()[:20] for x in (value.get("cities") or []) if str(x).strip()][:6]
    value["remote"] = value.get("remote") if isinstance(value.get("remote"), bool) else None
    value["version"] = str(value.get("version") or DEFAULT_PREFERENCES["version"])
    return value


def plan_preset(plan: dict | None) -> str:
    """方案里选的模板预设 id；缺省 / 未知都回落到 `general`。"""
    from boss_core import templates

    return templates.plan_preset_id(plan)


def preference_facts(value: dict, preset_id: str | None = None) -> list[dict]:
    """口径 → 写稿时能引用的事实条目（与工作流侧 availability_facts 同一套规则）。

    "可实习 / 可持续"的说法由方案的模板预设决定，不默认套用实习口径。
    """
    from boss_core import templates

    month_label = templates.variables(preset_id).get("availability_month_label") or "可持续"
    rows: list[dict] = []
    if value.get("start"):
        rows.append({"id": "P1", "text": f"到岗时间：{value['start']}"})
    if value.get("days_per_week"):
        rows.append({"id": "P2", "text": f"每周可到岗 {value['days_per_week']} 天"})
    if value.get("months"):
        rows.append({"id": "P3", "text": f"{month_label} {value['months']} 个月"})
    if value.get("cities"):
        rows.append({"id": "P4", "text": "可接受城市：" + "、".join(value["cities"])})
    if isinstance(value.get("remote"), bool):
        rows.append({"id": "P5", "text": "异地线下：" + ("接受" if value["remote"] else "不接受")})
    return rows


@router.get("/preferences")
def preferences():
    """个人口径：优先读活动方案（唯一真源），没有工作区才退回旧文件。"""
    from boss_core.models import c_mode, c_scalar, c_values
    from boss_core.workspace import Workspace

    root, _ = paths()
    ws = Workspace(root)
    plan = ws.active_plan() if ws.exists() else None
    if plan is not None:
        avail = plan.get("availability") or {}
        target = plan.get("target") or {}
        value = normalise_preferences({
            "version": plan.get("id"),
            "start": c_scalar(avail.get("start")) or "",
            "days_per_week": c_scalar(avail.get("days_per_week")),
            "months": c_scalar(avail.get("months")),
            "cities": c_values(target.get("cities")),
            "remote": c_scalar(target.get("accepts_relocation")),
            "note": DEFAULT_PREFERENCES["note"],
        })
        return {"ok": True, "preferences": value,
                "facts": preference_facts(value, plan_preset(plan)),
                "source": "plan", "plan_id": plan.get("id"),
                "message": "留空的项不会写进招呼语；保存后已写好的稿子需要重写"}

    path = root / "greeting/preferences.json"
    data = read_object(path) if path.is_file() else {}
    value = normalise_preferences(data)
    return {"ok": True, "preferences": value, "facts": preference_facts(value),
            "source": "legacy_file",
            "message": "留空的项不会写进招呼语；保存后已写好的稿子需要重写"}


class PreferencesBody(BaseModel):
    expected_version: str = Field(default="", max_length=80)
    start: str = Field(default="", max_length=40)
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    months: int | None = Field(default=None, ge=1, le=36)
    cities: list[str] = Field(default_factory=list, max_length=6)
    remote: bool | None = None
    updated_by: str = Field(default="panel", max_length=40)


@router.put("/preferences")
def save_preferences(body: PreferencesBody):
    """保存个人口径。这是个设置：个人 AI 也可以按 `greeting/skill-求职口径.md` 直接写这里。"""
    root, _ = paths()
    path = root / "greeting/preferences.json"
    value = normalise_preferences({
        "start": body.start, "days_per_week": body.days_per_week, "months": body.months,
        "cities": body.cities, "remote": body.remote,
        "updated_at": time.strftime("%Y-%m-%d %H:%M"), "updated_by": body.updated_by.strip()[:40] or "panel",
    })
    if not path.is_file():
        # 第一次保存：先落一份默认值，save_versioned 才能读到当前版本做乐观锁
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_PREFERENCES, ensure_ascii=False, indent=2), encoding="utf-8")
    saved = save_versioned(root, "greeting/preferences.json", value, body.expected_version or DEFAULT_PREFERENCES["version"])
    # 口径的真正消费者是方案快照：可用安排与城市/异地必须同步过去，否则改了不生效
    from boss_core.models import c_list, c_unknown, c_value

    def choice(number):
        return c_value(int(number)) if isinstance(number, int) else c_unknown()

    plan = apply_to_plan(root, {
        "availability": {"start": c_value(value["start"]) if value["start"] else c_unknown(),
                         "days_per_week": choice(value["days_per_week"]),
                         "months": choice(value["months"])},
        "target": {"cities": c_list(value["cities"]),
                   "accepts_relocation": (c_value(value["remote"])
                                          if isinstance(value["remote"], bool) else c_unknown())},
    }, reason="改个人求职口径")
    return {"ok": True, "preferences": normalise_preferences(saved),
            "facts": preference_facts(saved, plan_preset(plan)),
            "plan": (plan or {}).get("id"),
            "message": "口径已保存；下次写稿生效，已写好的稿子需要重写"}


class TaskBody(BaseModel):
    action: Literal["collect", "match", "prepare", "send", "feedback"]
    due: float | None = None
    limit: int = Field(default=6, ge=1, le=30)
    only: list[str] = Field(default_factory=list, max_length=30)
    approved: dict[str, str] = Field(default_factory=dict)
    confirmed: Literal[True]

@router.get("/tasks")
def tasks():
    root, _ = paths()
    return {"items": TaskStore(root).list(), "server_time": time.time()}


#: Windows 计划任务查询约 0.5 秒；面板 5 秒刷一次，没必要每次都查
_OS_TASKS_CACHE: dict = {"at": 0.0, "data": []}
_OS_TASKS_TTL = 60.0
#: 配置里 rounds 的键 → 计划执行时刻。没列出来的键（如 manual）就是"只能手动"。
ROUND_AT = {"09": "09:00", "14": "14:00", "21": "21:00"}


def _os_tasks() -> list[dict[str, str]]:
    """系统计划任务里属于本项目的条目；没有就是空列表。

    面板要回答的是"到点到底谁会去跑"，所以必须看系统里的真凭据（schtasks）。
    配置里有 `rounds` 不等于有人按时触发——2026-11-01 查下来就一条都没有，
    结果 10-31、11-01 两天一轮都没跑，面板却看不出来。
    """
    now = time.time()
    if _OS_TASKS_CACHE["data"] and now - float(_OS_TASKS_CACHE["at"]) < _OS_TASKS_TTL:
        return list(_OS_TASKS_CACHE["data"])
    rows: list[dict[str, str]] = []
    text = ""
    try:
        proc = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/nh"],
                              capture_output=True, timeout=15, check=False)
        text = (proc.stdout or b"").decode("gbk", "replace")
    except Exception:  # noqa: BLE001 - 查不到就当没有，不把整个面板打挂
        text = ""
    keys = ("boss", "deliver_round", "boss_auto", "boss-auto", "投递", "greeting")
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = [p.strip('"') for p in next(csv.reader([line]), [])]
        if len(parts) < 3:
            continue
        name, nxt, status = parts[0], parts[1], parts[2]
        low = name.lower()
        # 系统自带的条目不算：`\Microsoft\Windows\...DataCollector` 里也有 "collector"，
        # 放进来会让面板谎报"有 1 条计划任务在触发"。
        if low.startswith("\\microsoft\\"):
            continue
        if any(k in low for k in keys):
            rows.append({"name": name, "next": nxt, "status": status})
    _OS_TASKS_CACHE.update(at=now, data=rows)
    return rows


def _optional_object(path: Path) -> dict:
    """读配置；缺文件或坏了都当"没设置"，让页面能显示而不是报错。"""
    try:
        return read_object(path)
    except ValidationWebError:
        return {}


def _recent_runs(root: Path, limit: int = 400) -> list[dict]:
    """rounds.jsonl 的尾部若干条（轮次记录）。坏行跳过，不让面板 500。"""
    path = root / "rounds.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _run_brief(row: dict | None) -> dict | None:
    if not row:
        return None
    return {"time": str(row.get("time") or ""), "round": str(row.get("round") or ""),
            "mode": str(row.get("mode") or ""), "eligible": row.get("eligible"),
            "prepared": row.get("prepared"), "ready": row.get("ready"),
            "ok": row.get("ok"), "failed": row.get("failed"),
            "aborted": str(row.get("aborted") or "")}


@router.get("/schedule")
def schedule():
    """今天的安排：每个轮次该几点跑、今天跑没跑、上次结果、以及谁在触发。

    这一块存在的理由：面板以前只列"我手动排过的任务"，看起来像空的；
    真正该给人核对的是"计划是什么 / 实际做了什么 / 差在哪"。
    """
    root, _ = paths()
    # 新工作区可能只有 workspace.json：缺配置文件时按"没设置"显示，不要 422
    wf = _optional_object(root / "config/workflow.json")
    search = _optional_object(root / "config/search.json")
    now = dt.datetime.now()
    today = now.date().isoformat()
    weekday = now.isoweekday()  # 1=周一 … 7=周日
    runs = _recent_runs(root)
    rounds: list[dict] = []
    for name, conf in (wf.get("rounds") or {}).items():
        conf = conf if isinstance(conf, dict) else {}
        at = ROUND_AT.get(str(name), "")
        today_run = next((r for r in reversed(runs)
                          if str(r.get("round") or "") == str(name)
                          and str(r.get("date") or "") == today), None)
        last_run = next((r for r in reversed(runs) if str(r.get("round") or "") == str(name)), None)
        if not at:
            state = "manual"
        elif weekday > 5:
            state = "weekend"
        elif today_run:
            state = "done"
        elif now.strftime("%H:%M") >= at:
            state = "missed"
        else:
            state = "upcoming"
        rounds.append({
            "name": str(name), "at": at, "state": state,
            "cap": conf.get("cap"), "prepare": conf.get("prepare"),
            "refill": bool(conf.get("refill")),
            "note": str(conf.get("note") or ""),
            "today": _run_brief(today_run), "last": _run_brief(last_run),
        })
    rounds.sort(key=lambda r: (r["at"] == "", r["at"]))
    collector = view._collector(root) or {}
    interval = int(search.get("cycle_sleep_sec") or 0)
    next_at = ""
    if collector.get("at") and interval:
        try:
            next_at = (dt.datetime.strptime(str(collector["at"]), "%Y-%m-%d %H:%M:%S")
                       + dt.timedelta(seconds=interval)).strftime("%H:%M")
        except ValueError:
            next_at = ""
    all_tasks = TaskStore(root).list()
    queued = [t for t in all_tasks if t.get("status") == "queued"]
    last_task = None
    for item in all_tasks:
        if last_task is None or float(item.get("due") or item.get("created") or 0) > float(
                last_task.get("due") or last_task.get("created") or 0):
            last_task = item
    return {
        "ok": True,
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "weekday": weekday,
        "weekend": weekday > 5,
        "rounds": rounds,
        "collector": {**collector, "interval_sec": interval, "next_at": next_at,
                      "page_cap": search.get("daily_page_cap")},
        "match": view._match_live() or {},
        "triggers": {"os": _os_tasks(), "checked_at": now.strftime("%H:%M:%S")},
        "queued": [{"id": t.get("id"), "action": t.get("action"), "due": t.get("due")} for t in queued],
        # 最近一次面板任务：这一页以前只列"我排过的任务"，看起来像老数据，
        # 其实是因为采集器/轮次都不写这张表——把最近一条和它的时间摆出来，就不用猜了。
        "last_task": ({"id": last_task.get("id"), "action": last_task.get("action"),
                       "status": last_task.get("status"), "due": last_task.get("due"),
                       "created": last_task.get("created"), "finished": last_task.get("finished")}
                      if last_task else None),
        "task_total": len(all_tasks),
        "last_any": _run_brief(runs[-1]) if runs else None,
        # 周期计划：存了没有、启用没有、下次什么时候、上次跑成什么
        "plans": TaskStore(root).list_schedules(),
        **_runner_state(),
    }


def background_receipt(task):
    runner = _runner_state()['runner']
    return {**task, 'background': {
        'runner_alive': runner['alive'], 'chat_required': False,
        'handoff_ready': runner['alive'],
        'status_url': '/api/workflow/control/tasks/' + task['id'],
        'message': ('程序后台已接管，可以结束AI对话；保持程序服务运行和电脑唤醒，结果会保存在任务及发送记录中。'
                    if runner['alive'] else '任务已保存，但后台执行器未启动；启动程序服务后才会执行，暂不能报告正在后台运行。')}}


@router.get('/tasks/{job_id}')
def task_detail(job_id: str):
    root, _ = paths()
    try:
        return background_receipt(TaskStore(root).get(job_id))
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


@router.post("/tasks")
def create_task(body: TaskBody,
                idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    """交给服务后台执行；接管成功即可结束AI对话，无需持续轮询维持任务。"""
    root, _ = paths()
    if body.action == 'send':
        from ..services.delivery_control import ensure_available
        ensure_available()
    from ..services.workflow_context import effective_profile
    profile = effective_profile(root)
    if body.action in {"match", "prepare", "send"} and not profile.get("version"):
        raise ValidationWebError("还没有已采用的简历资料，请先确认简历与个人方案")
    payload = {"profile_version": str(profile.get("version") or ""), "limit": body.limit}
    if body.action == "match":
        # 补分：只把「有 JD、没打过分、也没被硬条件挡掉」的岗位交给匹配任务。
        # 空 job_ids 在匹配后端等于「全库重跑」，会把已经打过的上千条再烧一遍。
        # 名单优先用报告算出来的（报告里已排除日薪/学历/经验不符的，采集器也是同一套口径）。
        # 只有报告本身算不出来（老版本/接口挂了）才退回范围更宽的旧口径；
        # 报告明确说"没有真待办"时不能再退回旧口径——那会把被硬条件挡掉的岗位也拿去烧钱。
        report = view.sendable()
        if report.get("ok") and "unscored_todo_ids" in report:
            missing = list(report.get("unscored_todo_ids") or [])
        else:
            missing = view.unscored_job_ids(str(profile.get("resume_id") or ""))
        if not missing:
            blocked = int(report.get("unscored_blocked") or 0)
            raise ValidationWebError(
                "没有需要补打分的岗位"
                + (f"（另有 {blocked} 个被日薪/学历/经验挡掉，不会去打分）" if blocked else ""))
        # 一批只发 80 个：和采集器一轮的量对齐，既能把进度往前推，又不会一次烧掉几百次调用
        payload["job_ids"] = missing[:80]
        payload["limit"] = len(payload["job_ids"])
    if body.only:
        if body.action != "prepare":
            raise ValidationWebError("只有写稿任务可以指定具体岗位")
        payload["only"] = list(dict.fromkeys(x.strip() for x in body.only if x.strip()))[:30]
        if not payload["only"]:
            raise ValidationWebError("请选择要写稿的岗位")
        payload["limit"] = len(payload["only"])
    if body.action == "send":
        if not 1 <= len(body.approved) <= 40:
            raise ValidationWebError("请先勾选并审核 1–40 条待发送稿件")
        current = view.sendable(force=True)
        if not current.get("ok"):
            raise ValidationWebError(current.get("error") or "无法核对待发稿件")
        hashes = {r["job_id"]: r.get("review_hash") for r in current.get("rows", [])}
        if any(not value or hashes.get(jid) != value for jid, value in body.approved.items()):
            raise ConflictError("待发稿件或资料已变，请刷新后重新审核")
        payload["approved"] = body.approved
    try:
        store = TaskStore(root)
        if idempotency_key and body.due is None:
            request_hash = hashlib.sha256(json.dumps({'action': body.action, 'payload': payload},
                                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            payload['request_hash'] = request_hash
            task, created = store.add_idempotent(body.action, payload, idempotency_key)
            if not created and (task.get('payload') or {}).get('request_hash') != request_hash:
                raise ConflictError('这个幂等键已经用于另一份任务，请为新请求使用新键')
        else:
            task = store.add(body.action, payload, body.due)
        return background_receipt(task)
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


@router.post("/tasks/{job_id}/cancel")
def cancel_task(job_id: str):
    root, _ = paths()
    try:
        store = TaskStore(root)
        job = store.get(job_id)
        result = store.cancel(job_id)
        if job['status'] == 'running' and job['action'] in {'send', 'auto_send'}:
            from ..services.deliver_task import deliver_tasks
            current = deliver_tasks.current()
            if current is not None and current.status == 'running':
                deliver_tasks.cancel(current.task_id)
        return result
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


class AcknowledgeBody(BaseModel):
    confirmed: Literal[True]


@router.post("/tasks/{job_id}/acknowledge")
def acknowledge_task(job_id: str, body: AcknowledgeBody):
    root, _ = paths()
    try:
        return TaskStore(root).acknowledge(job_id)
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


# ---------------------------------------------------------------- 规则范围授权
#
# 用户一次性确认「资料/方案版本 + 岗位范围 + 准入规则 + 额度 + 有效期」，之后程序在
# 范围内自动发送。确认走"预览 → 回执"两步：文本由服务端生成、原样存进记录，创建时
# 必须回传 `preview_sha` 与一次性 `token`。详见 `boss_core/authorization.py` 的说明。


class AuthzPreviewBody(BaseModel):
    spec: dict = Field(default_factory=dict)
    goal: str = Field(default="", max_length=200)


class AuthzCreateBody(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    preview_sha: str = Field(min_length=1, max_length=64)
    parent: str = Field(default="", max_length=80)
    confirmed: Literal[True]


class AuthzStateBody(BaseModel):
    status: Literal["active", "paused", "revoked"]
    reason: str = Field(default="", max_length=200)
    confirmed: Literal[True]


class AuthzRunBody(BaseModel):
    due: float | None = None
    confirmed: Literal[True]


@router.get("/authorization")
def authorization():
    """当前授权：生效中的那条、用量、确认原文、以及能不能发。"""
    root, _ = paths()
    return authz.summary(root)


@router.get("/authorization/{grant_id}")
def authorization_detail(grant_id: str):
    """单条授权 + 逐条使用记录（对账用）。"""
    root, _ = paths()
    grants = authz.store(root)
    try:
        grant = grants.get(grant_id)
    except Exception as exc:  # noqa: BLE001 - 找不到就是找不到
        raise ValidationWebError(f"找不到授权：{grant_id}") from exc
    return {"ok": True, "grant": grant, "usage": grants.usage(grant_id),
            "uses": grants.rows(grant_id)}


@router.post("/authorization/preview")
def authorization_preview(body: AuthzPreviewBody):
    """生成待确认的授权文本。**这一步不发任何消息**，也不改变现有授权。"""
    root, _ = paths()
    base = authz.default_spec(root)
    preview = authz.store(root).preview(
        body.spec, labels=authz.labels(root, goal=body.goal), base=base)
    return {"ok": True, "token": preview["token"], "preview_sha": preview["preview_sha"],
            "text": preview["text"], "spec": preview["spec"],
            "expires_at": preview["expires_at"],
            "next": ["把 text 给用户看", "POST /api/workflow/control/authorization"
                     "（带 token + preview_sha + confirmed）"]}


@router.post("/authorization")
def authorization_create(body: AuthzCreateBody,
                          x_actor: str | None = Header(default=None, alias="X-Actor")):
    """回执核销 → 新建（或按上一版改出）一条生效中的授权。

    `X-Actor` 只记成 `created_by`，是自报标签，不当确认依据；确认依据是回执本身。
    """
    root, _ = paths()
    grants = authz.store(root)
    try:
        if body.parent:
            grant = grants.revise(body.parent, {}, token=body.token,
                                  preview_sha=body.preview_sha,
                                  actor=_actor_of(x_actor), via="api/authorization")
        else:
            grant = grants.create(token=body.token, preview_sha=body.preview_sha,
                                  actor=_actor_of(x_actor), via="api/authorization")
    except Exception as exc:  # noqa: BLE001 - 回执类错误统一成 422
        raise ValidationWebError(str(exc)) from exc
    return {"ok": True, "grant": grant, "usage": grants.usage(grant["id"]),
            "next": ["POST /api/workflow/control/authorization/{id}/run"]}


@router.post("/authorization/{grant_id}/state")
def authorization_state(grant_id: str, body: AuthzStateBody,
                        x_actor: str | None = Header(default=None, alias="X-Actor")):
    """暂停 / 恢复 / 撤销。三个动作都只缩小范围，不需要重新确认。

    暂停或撤销时**同时叫停正在跑的这一批**：发送子进程每发一条前只看
    `runs/panel-jobs/<task>.stop`，不读授权状态。以前这里只改了授权状态，
    子进程毫不知情，暂停之后剩下的岗位照发——"暂停"只在下一个任务生效。
    """
    root, _ = paths()
    grants = authz.store(root)
    try:
        grant = grants.set_status(grant_id, body.status, reason=body.reason)
    except Exception as exc:  # noqa: BLE001
        raise ValidationWebError(str(exc)) from exc
    stopped = TaskStore(root).stop_grant(grant_id) if body.status in {"paused", "revoked"} else []
    return {"ok": True, "grant": grant, "usage": grants.usage(grant_id),
            "stopped_tasks": stopped, "by": _actor_of(x_actor)}


@router.post("/authorization/{grant_id}/run")
def authorization_run(grant_id: str, body: AuthzRunBody,
                      x_actor: str | None = Header(default=None, alias="X-Actor")):
    """按授权挑出这一轮该发的岗位，交给**既有**发送链执行。

    这里只做三件事：判定（是否仍在授权范围内）、占额度、建任务。正文指纹校验、
    去重、冷却、平台限制全部仍由 `deliver_round_v2 --send-ready-only` 执行。
    """
    root, _ = paths()
    from ..services.delivery_control import ensure_available
    ensure_available()
    try:
        plan = auto_send.prepare(root, grant_id)
    except auto_send.NotAllowed as exc:
        if exc.reasons:
            raise ConflictError(str(exc) + "：" + "；".join(exc.reasons)) from exc
        raise ValidationWebError(str(exc)) from exc

    payload = {"profile_version": _profile_version(root), "limit": len(plan["approved"]),
               "approved": plan["approved"], "grant_id": grant_id}
    try:
        task = TaskStore(root).add("auto_send", payload, body.due, jid=plan["task_id"])
    except Exception:
        authz.store(root).release_task(plan["task_id"])
        raise
    return {"ok": True, "task": background_receipt(task), "approved": len(plan["approved"]),
            "blocked": plan["blocked"], "usage": plan["usage"], "by": _actor_of(x_actor)}


# ---------------------------------------------------------------- 周期计划
#
# 周期计划只是"什么时候该排一个任务"的一行记录；到点由执行器 materialize 成普通
# 任务，之后完全走既有串行/失败不重试/额度规则。`next_run` 落盘，是唯一的下次执行依据。


class ScheduleBody(BaseModel):
    action: Literal["collect", "match", "prepare", "feedback", "auto_send"]
    payload: dict = Field(default_factory=dict)
    kind: Literal["interval", "daily"]
    every_seconds: int = Field(default=0, ge=0, le=604800)
    at_hhmm: str = Field(default="", max_length=5)
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    catch_up: Literal["auto", "once", "skip"] = "auto"
    note: str = Field(default="", max_length=200)
    enabled: bool = True
    confirmed: Literal[True]


class ScheduleStateBody(BaseModel):
    enabled: bool
    confirmed: Literal[True]


@router.get("/schedule/plans")
def schedule_plans():
    """全部周期计划 + 执行器状态（到点到底会不会有人跑）。"""
    root, _ = paths()
    return {"ok": True, "items": TaskStore(root).list_schedules(), **_runner_state(),
            "server_time": time.time()}


def _runner_state() -> dict:
    from ..services.workflow_tasks import RUNNER

    return {"runner": {"kind": "in_process", "alive": bool(RUNNER.get("alive")),
                       "since": float(RUNNER.get("since") or 0),
                       "tick_seconds": int(RUNNER.get("tick_seconds") or 2),
                       "note": "执行器只在服务进程运行期间工作；程序退出或电脑休眠时不执行"}}


@router.post("/schedule/plans")
def create_schedule(body: ScheduleBody,
                    x_actor: str | None = Header(default=None, alias="X-Actor")):
    """新建一条周期计划。**不立即执行**，第一次执行时间由 `next_run` 决定。"""
    root, _ = paths()
    if body.action == "auto_send" and not str(body.payload.get("grant_id") or ""):
        raise ValidationWebError("周期发送要绑定一条规则范围授权（payload.grant_id）")
    if body.action == "auto_send":
        try:
            authz.store(root).get(str(body.payload["grant_id"]))
        except Exception as exc:  # noqa: BLE001
            raise ValidationWebError(f"找不到授权：{body.payload.get('grant_id')}") from exc
        body.payload.setdefault("evaluate", True)
    try:
        task = TaskStore(root).add_schedule(
            body.action, body.payload, kind=body.kind, every_seconds=body.every_seconds,
            at_hhmm=body.at_hhmm, weekdays=",".join(str(x) for x in body.weekdays),
            catch_up=body.catch_up, note=body.note)
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc
    if not body.enabled:
        task = TaskStore(root).set_schedule_enabled(task["id"], False)
    return {"ok": True, "plan": task, "by": _actor_of(x_actor), **_runner_state()}


@router.post("/schedule/plans/{plan_id}/state")
def schedule_state(plan_id: str, body: ScheduleStateBody,
                   x_actor: str | None = Header(default=None, alias="X-Actor")):
    """启用 / 停用一条周期计划。停用后不再排新任务，已排队的不受影响。"""
    root, _ = paths()
    try:
        plan = TaskStore(root).set_schedule_enabled(plan_id, body.enabled)
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc
    return {"ok": True, "plan": plan, "by": _actor_of(x_actor)}


@router.delete("/schedule/plans/{plan_id}")
def delete_schedule(plan_id: str):
    """删掉周期计划。"""
    root, _ = paths()
    try:
        return {"ok": True, **TaskStore(root).delete_schedule(plan_id)}
    except ValueError as exc:
        raise ValidationWebError(str(exc)) from exc


def _actor_of(value: str | None) -> str:
    """自报标签，只用于回溯。"""
    return str(value or "unknown").strip()[:80] or "unknown"


def _profile_version(root: Path) -> str:
    from ..services.workflow_context import effective_profile
    return str(effective_profile(root).get("version") or "")
