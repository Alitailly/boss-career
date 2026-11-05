"""只读的当前输入：复用执行模板，不调模型、不访问招聘平台。"""
from __future__ import annotations

from typing import Any

from boss_core import strategies
from boss_core.errors import Unsupported

from . import workflow_context as wctx


def read_stage_inputs(stage: str) -> dict[str, Any]:
    snap = wctx.snapshot()
    description = strategies.describe(snap or {}, stage)
    plan = (snap or {}).get("plan") or {}
    out: dict[str, Any] = {
        "ok": True, "stage": stage, "scope": "current", "source": description["source"],
        "strategy_id": description.get("id"), "revision": description.get("revision"),
        "versions": (snap or {}).get("versions") or {},
        "resume_id": (((snap or {}).get("candidate") or {}).get("resume") or {}).get("resume_id"),
        "sections": [], "notice": description.get("note") or "",
        "conditions": {"target": plan.get("target"), "requirements": plan.get("requirements"),
                       "salary": plan.get("salary")},
    }
    if not description["executes_locally"]:
        out["mode"] = "external"
        out["sections"] = [{"title": "交给用户 AI 的外部方法内容", "text": description.get("content") or {}}]
        out["notice"] = "已绑定外部方法。以下是保存的方法内容，不代表程序已执行该方法；实际结果以已提交记录为准。"
        return out

    def section(title: str, text: str, **meta: Any) -> None:
        out["sections"].append({"title": title, "text": text, **meta})

    out["mode"] = "local"
    if stage == "matching":
        from . import single_scoring
        standard = single_scoring.resolve_standard()
        section("一次评分 · 实际系统提示词", standard['system'], origin=standard['origin'], status='resolved')
        section("一次评分 · 候选人和批量岗位输入模板", single_scoring.USER_TEMPLATE, secondary=True)
        section("一次评分 · 单个岗位资料模板", single_scoring.JOB_TEMPLATE, secondary=True)
        out['execution_mode'] = 'single_pass'
        out['criteria'] = {'text': standard['text'], 'shared': True, 'legacy': standard['legacy']}
        out['candidate_material'] = single_scoring.candidate_material(snap=snap or {})
        out['ready'] = out['candidate_material']['ready']
        out['thresholds'] = {'admit_min_score': wctx.admit_min_score(0)}
        out['limits'] = wctx.limits()
        out['material_note'] = '新评分仅使用下方已确认材料，不追加简历全文。每批一次评估：未达标简评、符合标准详评、关键未知待核实。'
        out['notice'] = '当前标准用于后续评分。旧初筛门槛不再参与；候选池仍核对硬条件、分数和待核实项。'
    elif stage == "greeting":
        from . import match_task as match
        from .greeting_prompt import resolve_greeting_requirement

        out['candidate_facts'] = ((snap or {}).get('candidate') or {}).get('facts') or []
        out['material_note'] = '当前个人事实供核对；每条成稿实际收到的事实、评分摘要与JD摘录见该条输入输出。'
        section("单条重生成 · 系统提示词", match._greeting_system_prompt())
        rules = wctx.greeting_rules()
        section("当前合成写作规则", rules["text"] if rules else resolve_greeting_requirement() or "尚未配置写作规则")
        section("单条重生成 · 简历与岗位输入模板", match.GREETING_USER_TEMPLATE, secondary=True)
        out["notice"] = "当前规则用于后续生成，不会改写已有草稿。批量写稿的完整执行指令在下方单独展示。"
    elif stage == "interview":
        from . import interview_generator as generator

        section("基础题库生成 · 系统提示词", generator._system_prompt())
        section("基础题库生成 · 岗位与简历输入模板", generator.USER_TEMPLATE, secondary=True)
        section("补充出题模板", generator.SUPPLEMENT_BLOCK, secondary=True)
        out["notice"] = "这是程序基础题库的当前模板。与用户 AI 进行的模拟对话以对话中采用的方法为准，基础出题不自动包含已发送招呼语。"
    elif stage == "resume_revision":
        from . import resume_revision as revision
        from . import resume_revision_free as free

        messages = revision.build_messages("【运行时填入所选简历原文】", "【运行时填入所选岗位画像】")
        section("按目标岗位优化 · 系统提示词", messages[0]["content"])
        section("按目标岗位优化 · 资料输入模板", messages[1]["content"], secondary=True)
        messages = free.build_messages(resume_md="【运行时填入所选简历原文】",
                                       selected_sections=["【运行时填入所选章节】"],
                                       optimization_prompt="【运行时填入本次优化要求】")
        section("按章节优化 · 系统提示词", messages[0]["content"], secondary=True)
        section("按章节优化 · 资料输入模板", messages[1]["content"], secondary=True)
        out["notice"] = "当前模板用于程序内的基础优化。简历页面的原文、解析及采用版本仍是实际资料；查看模板不执行优化。"
    else:
        raise Unsupported(f"未提供该阶段输入：{stage}")
    return out
