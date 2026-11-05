"""定制能力清单：这台程序**到底能改什么、怎么改、什么时候生效、改坏了谁会拒绝**。

这份清单是给"接手的 AI"看的契约，也是给维护者看的自检表。规矩：

1. 每一项都必须指向**真实存在**的接口和**真实读取它**的代码位置；
   只写"这里可以改提示词"不算交付，要能证明执行链路真的采用了。
2. 没实现的能力写进 `unsupported`，不假装支持。
3. 默认值写的是"新用户空工作区时实际生效的值"，不是文档里的理想值。

生成给人看的版式::

    python -m boss_web.services.capabilities > docs/定制能力清单.md
"""

from __future__ import annotations

import json
from typing import Any

CATALOG: list[dict[str, Any]] = [
    {
        "id": "resume.candidate",
        "stage": "resume",
        "title": "简历与个人事实",
        "purpose": "决定评分、写稿、面试共用的那套事实。所有对外文案只能引用这里的事实。",
        "read": ["GET /api/workflow/control/context", "GET /api/ai/context",
                 "GET /api/workflow/control/resume/{resume_id}/preview"],
        "write": ["PUT /api/workflow/control/candidate"],
        "example": {
            "method": "PUT", "path": "/api/workflow/control/candidate",
            "body": {"resume_id": "res_xxx", "source_sha": "<预览返回的 sha>",
                     "expected_version": "prefs-empty",
                     "facts": [{"id": "F1", "text": "本科，数据科学专业（届别按本人实际填）"},
                               {"id": "F2", "text": "两段数据看板项目，负责取数与看板搭建"}],
                     "confirmed": True},
        },
        "defaults": "空工作区没有简历；`facts` 为空数组，不虚构任何事实。",
        "validation": "必须带预览拿到的 `source_sha`（防正文已改）；`facts` 至少 1 条、"
                      "编号唯一、每条 ≤500 字；`expected_version` 做乐观锁，过期报 409。",
        "effective_at": "下一个任务（执行链读方案快照，不读面板文件）",
        "enforced_in": "boss_core/snapshot.py::build → 评分/写稿/面试",
        "impact": "改简历等于换候选人版本：旧稿会被判「资料变了」并要求重写。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["从优化稿反向推定新经历（只接受用户确认的事实）"],
    },
    {
        "id": "resume.revision",
        "stage": "resume_revision",
        "title": "简历优化（可选）",
        "purpose": "在保留原稿的前提下生成一版修改稿；不采用也能走完整流程。",
        "read": ["GET /api/resume/revise/presets", "GET /api/resume/revisions",
                 "GET /api/resume/revisions/{revision_id}", "GET /api/ai/strategies"],
        "write": ["POST /api/resume/revise/start", "POST /api/resume/revise/free-start",
                  "POST /api/resume/revisions/{revision_id}/save",
                  "POST /api/ai/strategies", "POST /api/ai/strategies/{id}/bind"],
        "example": {
            "step1": {"method": "POST", "path": "/api/resume/revise/start",
                      "body": {"resume_id": "res_xxx", "job_ids": ["j1", "j2"]}},
            "step2": {"method": "POST",
                      "path": "/api/resume/revisions/{revision_id}/save",
                      "body": {"format": "md", "title": "简历-数据方向-v2"}},
            "step3": {"method": "PUT", "path": "/api/workflow/control/candidate",
                      "body": {"resume_id": "<step2 返回的 saved_resume_id>",
                               "source_sha": "<先 GET 该简历 preview 拿到的 sha>",
                               "expected_version": "<当前版本>",
                               "facts": [{"id": "F1", "text": "（沿用已确认的事实）"}],
                               "confirmed": True},
                      "note": "保存新简历 ≠ 切换生效版本；这一步才让评分/写稿/面试改用新稿"},
        },
        "defaults": "策略 `resume_revision` 未绑定时用内置模板（按方向渲染，不含具体人的身份）。",
        "validation": "策略 `source` 只认 builtin/custom_prompt/external_skill；外部方法只登记不执行。",
        "effective_at": "草稿即时；`save` 只新建一份简历，切换生效版本还要第 3 步 candidate",
        "enforced_in": "boss_web/services/resume_revision.py、resume_store 的活动简历指针",
        "impact": "不采用时原稿继续有效，搜索/评分/写稿/面试都不受影响。"
                  "**`save` 返回 `saved_resume_id` 不等于改绑**：生效版本仍由候选人的 "
                  "`resume_id` 决定，必须显式 PUT candidate 才算采用。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["自动下载或安装第三方 Skill", "把外部 Skill 的方法直接当成本机可执行步骤"],
    },
    {
        "id": "plan.target",
        "stage": "plan",
        "title": "求职目标与采集条件",
        "purpose": "决定搜什么、哪些算硬条件、哪些只是排序偏好。",
        "read": ["GET /api/ai/plans/{id}", "POST /api/ai/plans/{id}/preview",
                 "GET /api/workflow/control/preferences", "GET /api/filters/conditions", "GET /api/filters/locations?city=城市代码"],
        "write": ["POST /api/ai/plans", "PATCH /api/ai/plans/{id}",
                  "POST /api/ai/plans/{id}/activate", "PUT /api/workflow/control/search"],
        "example": {
            "method": "PATCH", "path": "/api/ai/plans/plan_xxx",
            "body": {"expected_revision": "plan_xxx",
                     "patch": {"target": {"cities": {"mode": "list", "values": ["上海"]},
                                          "employment_types": {"mode": "list", "values": ["full_time"]}},
                               "requirements": {"degree_min": {"mode": "value", "value": "本科"}},
                               "salary": {"min": 10000, "period": "month", "max": None}}},
        },
        "defaults": "所有目标字段都是 `{\"mode\":\"unknown\"}`；`unknown` / `any` / `list` 三种含义分开存。",
        "validation": "按程序内置/本地目录及 selection/profiles 向用户介绍可选项，再生成条件；"
                      "首次了解能力可读 /api/filters/conditions，同版本目录可复用，日常不访问平台；"
                      "代码校验兜底，未知值/拼错筛选字段或不属于当前网页分支的条件返回422；"
                      "validate/context 返回 search.bases 与 search.audit 核对实际条件和未确认项。"
                      "职位类别用 position 的末级选项；区/商圈用 multiBusinessDistrict，地铁用 multiSubway，按本地城市目录校验父子关系。"
                      "多城市精细位置分别保存 search.city_filters；空目录可显式 refresh=true 维护一次，日常不联网。"
                      "雇佣类型只认 internship/full_time/part_time/any；薪资币种与周期有枚举；"
                      "下限不能高于上限；`days_per_month` 1–31、`months_per_year` 1–12。",
        "effective_at": "下一个任务",
        "enforced_in": "boss_core/snapshot.py::compile_search（采集）、boss_core/conditions.py（判定）",
        "impact": "改目标会让旧的规则范围授权失效（锁定字段变化），发送暂停到重新授权。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["一次平台请求覆盖多个城市（采集器逐城市×关键词运行，共享原有额度/间隔）",
                        "当前网页快照未提供实习专选/日薪档；兼职切换为结算方式和兼职时段，日结不等于日薪金额",
                        "融资阶段仅关键词搜索非兼职分支启用；职位类别/区/商圈/地铁已接入关键词搜索，期望流不接受这些新字段；手机App选项及日月薪转换未核实",
                        "选项表 source=fallback 或 degraded 维度不能称为平台最新选项"],
    },
    {
        "id": "plan.keywords",
        "stage": "plan",
        "title": "关键词与搜索覆盖",
        "purpose": "决定采集广度。新增 JD 是唯一收益口径。",
        "read": ["GET /api/collection/coverage", "GET /api/collection/yield",
                 "GET /api/collection/digest"],
        "write": ["PUT /api/workflow/control/search", "PUT /api/workflow/control/collector"],
        "example": {"method": "PUT", "path": "/api/workflow/control/search",
                    "body": {"expected_version": "s1",
                             "queries": ["数据分析", "商业分析", "数据运营"]}},
        "defaults": "关键词空数组；采集器不会自己发明词表。",
        "validation": "去空去重、每个 ≤100 字、最多 50 个。",
        "effective_at": "采集器下一轮开始时热重载",
        "enforced_in": "boss_core/snapshot.py::compile_search → collector.py 逐词翻页",
        "impact": "翻页游标按关键词保存；加词不清空已跑过的词，删词会丢掉它的深度。",
        "executor": ["program_model"],
        "unsupported": ["按「关键词×城市×筛选」组合分别记游标（当前按关键词记）",
                        "按词统计新增（没有归属记录，见 capability=collection.yield）"],
    },
    {
        "id": "matching.strategy",
        "stage": "matching",
        "title": "匹配评分方法",
        "purpose": "已确认候选人材料＋个人标准＋岗位JD，每批一次评分，未达标简评、合格详评、关键未知待核实。",
        "read": ["GET /api/ai/stage-inputs/{stage}", "GET /api/ai/strategies", "POST /api/ai/plans/{id}/validate",
                 "GET /api/workflow/sendable"],
        "write": ["PUT /api/ai/scoring-material", "POST /api/ai/strategies", "POST /api/ai/strategies/{id}/bind",
                  "PATCH /api/ai/plans/{id}"],
        "example": {"method": "POST", "path": "/api/ai/strategies",
                    "body": {"stage": "matching", "name": "按项目证据排序", "source": "custom_prompt",
                             "content": {"criteria": "## 优先考虑\n按用户确认的职责偏好与真实技能证据判断"}}},
        "defaults": "`plan.strategies.matching` 为空 → 内置模板（按 `plan.template.id` 渲染维度名）。",
        "validation": "缺少 `{{变量}}` 会直接报缺配置，不会静默留空；外部 Skill 只登记不执行。",
        "effective_at": "下一个任务",
        "enforced_in": "boss_web/services/match_task.py + boss_web/services/workflow_context.py",
        "impact": "换方法后旧分数含义会变，必须用样例重新校准门槛。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["自动安装外部 Skill；用未确认材料评分"],
    },
    {
        "id": "matching.threshold",
        "stage": "matching",
        "title": "准入门槛与额度",
        "purpose": "决定多少分以上才算「可发」，以及每天最多发几条。",
        # 阈值没有 GET：当前值从 recipe 的 run 段读，只能 PUT 回写。
        "read": ["GET /api/workflow/control/recipe", "GET /api/ai/plans/{id}"],
        "write": ["PUT /api/workflow/control/settings", "PATCH /api/ai/plans/{id}"],        "example": {"method": "PUT", "path": "/api/workflow/control/settings",
                    "body": {"expected_version": "w1", "min_score": 70, "daily_cap": 20,
                             "chunk": 2, "chunk_gap": 60, "batch_size": 3}},
        "defaults": "min_score 70、daily_cap 150（方案中0表示当天不投递）、chunk 1、chunk_gap 20 秒；以活动方案回读值为准。",
        "validation": "min_score 0–100；daily_cap 0–150；chunk 1–25；chunk_gap 0–86400。",
        "effective_at": "下一个任务",
        "enforced_in": "boss_core/compat.py::delivery_config → deliver_round_v2 的 room 计算",
        "impact": "调高额度不能绕过规则范围授权的额度；两者取小。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["超过 150 条的日额度（平台侧风险，不做成可调项）"],
    },
    {
        "id": "greeting.strategy",
        "stage": "greeting",
        "title": "招呼语写法与披露",
        "purpose": "决定第一句话怎么写、能披露什么。正文仍要过事实层与平台校验。",
        "read": ["GET /api/workflow/control/preferences", "GET /api/ai/strategies",
                 "GET /api/system/greeting-prompt", "GET /api/ai/greeting-rules"],
        "write": ["PUT /api/workflow/control/preferences", "PUT /api/system/greeting-prompt",
                  "POST /api/ai/strategies", "POST /api/ai/strategies/{id}/bind"],
        "example": {
            "step1": {"method": "PUT", "path": "/api/workflow/control/preferences",
                      "body": {"start": "随时", "days_per_week": 5, "months": 6,
                               "cities": ["上海"], "remote": True}},
            "step2": {"method": "PUT", "path": "/api/system/greeting-prompt",
                      "body": {"requirement": "第一句先写能为这个岗位解决的具体问题；"
                                             "不超过 150 字；不写「贵司」「贵公司」。"}},
        },
        "defaults": "没有出厂范文；口吻与字数上限来自 `plan.template` 预设（默认 200 字）。",
        "validation": "披露开关只决定「能不能写进招呼语」，不参与筛选；空值 = 不提这项。",
        "effective_at": "下一批写稿",
        "enforced_in": "boss_core/greeting.py::compose + deliver_round_v2 的正文/事实校验",
        "impact": "改口径不会重做简历与采集；已写好的稿子会判「口径变了」需要重写。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["不同岗位复用同一条正文（每岗重新选亮点）"],
    },
    {
        "id": "interview.strategy",
        "stage": "interview",
        "title": "模拟面试",
        "purpose": "按简历与岗位逐轮提问、追问、给反馈。",
        "read": ["GET /api/ai/interviews", "GET /api/ai/interviews/{session_id}",
                 "GET /api/ai/interviews/resume", "GET /api/ai/strategies"],
        "write": ["POST /api/ai/interviews", "POST /api/ai/interviews/{session_id}/questions",
                  "POST /api/ai/interviews/{session_id}/turns",
                  "POST /api/ai/interviews/{session_id}/finish",
                  "POST /api/ai/strategies"],
        "example": {
            "step1": {"method": "POST", "path": "/api/ai/interviews",
                      "body": {"job_id": "j1", "plan_id": None}},
            "step2": {"method": "POST", "path": "/api/ai/interviews/{session_id}/turns",
                      "body": {"role": "user", "text": "（候选人的回答）", "key": "t-1"}},
            "step3": {"method": "POST", "path": "/api/ai/interviews/{session_id}/finish",
                      "body": {"reason": "练完"}},
        },
        "defaults": "内置模板按 `plan.template` 渲染题型与题量（默认 12 题、逐题反馈）。",
        "validation": "题量 1–30；类型与难度是枚举；没有方案时也能练（用通用预设）。",
        "effective_at": "新建会话时",
        "enforced_in": "boss_web/services/interview_generator.py（读策略）+ boss_core/interview.py（存回合）",
        "impact": "换面试方法只影响新会话，已有会话保留原题与进度。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["外部 Skill 控制整段对练（当前只能提供题面与反馈，不能接管会话）"],
    },
    {
        "id": "send.authorization",
        "stage": "send",
        "title": "规则范围授权（自动发送）",
        "purpose": "用户一次性确认范围、额度、期限；之后由程序在范围内自动发送，不再逐条问。",
        "read": ["GET /api/workflow/control/authorization",
                 "GET /api/workflow/control/authorization/{id}"],
        "write": ["POST /api/workflow/control/authorization/preview",
                  "POST /api/workflow/control/authorization",
                  "POST /api/workflow/control/authorization/{id}/state",
                  "POST /api/workflow/control/authorization/{id}/run"],
        "example": {
            "step1": {"method": "POST", "path": "/api/workflow/control/authorization/preview",
                      "body": {"goal": "上海数据分析全职",
                               "spec": {"quota": {"total": 20, "per_day": 5},
                                        "admission": {"min_score": 70, "exclude_companies": ["外包"]}}}},
            "step2": {"method": "POST", "path": "/api/workflow/control/authorization",
                      "body": {"token": "<preview.token>", "preview_sha": "<preview.preview_sha>",
                               "confirmed": True}},
        },
        "defaults": "空工作区没有任何授权。两种模式：默认**锁版本**（绑定 8 个方案字段，"
                    "改了就失效）；把 `basis.locked_plan_fields` 显式传 `[]` 就是**只锁边界**"
                    "（额度/期限/岗位范围/准入阈值仍管，方案怎么改都算覆盖）——调优期用这一种。",
        "validation": "必须先预览拿到 `token` + `preview_sha`（一次性、15 分钟有效）；"
                      "额度 1–150；额度不能原地改，只能按上一版新建并重新确认。"
                      "判定结果里的 `changed_fields` 会列出具体哪几项变了。",
        "effective_at": "创建即生效；每个任务执行前再判一次（暂停/改方案在执行那一刻也生效）",
        "enforced_in": "boss_core/authorization.py::evaluate/reserve；发送仍走 "
                       "deliver_round_v2 --send-ready-only 的逐条正文指纹校验",
        "impact": "锁版本的授权在资料/锁定字段/策略变化后不再覆盖，发送暂停并提示具体变了哪几项；"
                  "只锁边界的授权不受方案改动影响。",
        "executor": ["program_model"],
        "unsupported": ["证明'人一定读过确认文本'（本机应用没有第三方签名通道；"
                        "记录能证明的是范围与参数未被事后改动）",
                        "由 AI 自行提高额度"],
    },
    {
        "id": "send.task",
        "stage": "send",
        "title": "按岗位审批发送（人工路径）",
        "purpose": "已审核稿件整批交给程序后台；接管后可以结束AI对话，无需逐条调用或持续轮询。",
        "read": ["GET /api/workflow/sendable", "GET /api/workflow/control/tasks/{id}"],
        "write": ["POST /api/workflow/control/tasks"],
        "example": {"method": "POST", "path": "/api/workflow/control/tasks",
                    "body": {"action": "send", "approved": {"j1": "<review_hash>"},
                             "confirmed": True}},
        "defaults": "没有待发正文就没有可审批项。条间隔20秒，另受共享请求节奏控制，不能承诺每20秒发一条。",
        "validation": "1–40 条；每条的 `review_hash` 必须等于当前待发稿件的指纹，否则 409。",
        "effective_at": "任务被领取时（2 秒轮询）",
        "enforced_in": "boss_web/services/workflow_tasks.py::build_command → deliver_round_v2",
        "impact": "正文/资料/方案/策略任一变化都会让旧指纹失效；发送只复核本地硬条件、评分依据及用户冲突标记，"
                  "不额外逐岗请求详情；可选详情刷新独立限速及暂停。"
                  "回执 background.handoff_ready 表示服务已接管；chat_required=false，保持程序服务运行即可。",
        "executor": ["program_model"],
        "unsupported": ["把审批记录伪造给自动发送用"],
    },
    {
        "id": "schedule.plans",
        "stage": "runtime",
        "title": "周期计划",
        "purpose": "让某个动作按间隔或每天固定时间自动排一个任务。",
        "read": ["GET /api/workflow/control/schedule/plans", "GET /api/workflow/control/schedule"],
        "write": ["POST /api/workflow/control/schedule/plans",
                  "POST /api/workflow/control/schedule/plans/{id}/state",
                  "DELETE /api/workflow/control/schedule/plans/{id}"],
        "example": {"method": "POST", "path": "/api/workflow/control/schedule/plans",
                    "body": {"action": "collect", "kind": "interval", "every_seconds": 3600,
                             "catch_up": "once", "confirmed": True}},
        "defaults": "没有任何周期计划。",
        "validation": "间隔 60 秒–7 天；每天模式要 `HH:MM` 且星期 1–7；"
                      "`auto_send` 类型必须绑定一条授权。",
        "effective_at": "保存后由执行器的 2 秒轮询在 `next_run` 到点时排任务",
        "enforced_in": "boss_web/services/workflow_tasks.py::materialize",
        "impact": "错过多次只补一次；发送类默认不补，避免休眠恢复后集中发送。",
        "executor": ["program_model"],
        "unsupported": ["程序退出时执行（执行器随服务进程；面板会如实显示 alive=false）"],
    },
    {
        "id": "runtime.account_binding",
        "stage": "runtime",
        "title": "发送账号与回复归属",
        "purpose": "绑定任务账号和本地会话版本，保留跨账号投递与回复档案，阻止旧任务跟随换号。",
        "read": ["GET /api/auth/account", "GET /api/workflow/feedback", "GET /api/workflow/feedback/records"],
        "write": ["POST /api/deliver/start"],
        "defaults": "新投递记录包含 sender_account_id/application_id；旧记录缺少证据时归属待确认。",
        "validation": "后台平台任务在提交时绑定账号与会话；换号或重登后旧任务停止，必须重新确认创建。回读仅处理当前账号所属投递。",
        "effective_at": "新任务提交、每次平台请求及回复入账前",
        "enforced_in": "boss_login/account.py；boss_jobs/client.py；workflow_tasks、deliver_task、feedback API 和 feedback_sync.py",
        "impact": "本地条件、评分与草稿可沿用；会话和实际发送/回复按账号保留。原共享频率及暂停不随换号清空。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["自动轮换账号接续受限任务", "自动猜测旧记录所属账号", "保证跨账号平台标识和可见性一致"],
    },
    {
        "id": "runtime.platform_requests",
        "stage": "runtime",
        "title": "共享平台请求节奏与暂停",
        "purpose": "避免采集、补全文、探活、发送、回复和登录检查跨任务突发请求。",
        "read": ["GET /api/platform/control", "GET /api/platform/diagnostics?hours=24&limit=100"],
        "write": ["POST /api/platform/pause", "POST /api/platform/resume"],
        "defaults": "同一数据目录跨进程共用：发起间隔至少8秒，每分钟最多6次，每10分钟最多40次；这些是程序参考限制。",
        "validation": "恢复必须 confirmed=true；不因计时到期或重启自动恢复。账号限制暂停全部平台操作，code37暂停对应请求链。",
        "effective_at": "每次实际业务请求/消息发送/浏览器令牌更新前",
        "enforced_in": "boss_jobs/request_control.py；SDK客户端、登录检查与元数据维护入口",
        "impact": "只限制平台操作，本地读取/评分可继续；失败尝试计入，发送不自动重试。"
                  "本地诊断按请求链记录请求耗时、排队/锁等待、额度等待原因及暂停拦截；旧记录无耗时，不倒推补造。",
        "executor": ["program_model", "host_ai"],
        "unsupported": ["保证平台不再风控", "控制程序外浏览器/App流量", "逐个控制浏览器资源加载和WebSocket心跳"],
    },
    {
        "id": "runtime.actor",
        "stage": "runtime",
        "title": "操作者标签（只用于回溯）",
        "purpose": "记录「谁改了」。**不是**身份认证。",
        "read": ["GET /api/workflow/control/authorization/{id}"],
        "write": ["请求头 X-Actor"],
        "example": {"headers": {"X-Actor": "my-ai"}},
        "defaults": "`unknown`。",
        "validation": "≤80 字，仅原样记录。",
        "effective_at": "写入即记录",
        "enforced_in": "boss_core/workspace.py::audit、授权记录的 created_by",
        "impact": "不影响任何判定；不能替代用户确认。",
        "executor": ["host_ai"],
        "unsupported": ["把自报标签当认证依据"],
    },
    {
        "id": "collection.yield",
        "stage": "collection",
        "title": "采集收益（新增 JD 主指标）",
        "purpose": "回答「这两天到底新增了多少可用岗位」，而不是「进程还在跑」。",
        "read": ["GET /api/collection/yield", "GET /api/collection/digest",
                 "GET /api/collection/events"],
        "write": ["POST /api/collection/sweep", "POST /api/collection/events/{id}/ack"],
        "example": {"method": "GET", "path": "/api/collection/yield?hours=24"},
        "defaults": "窗口 24 小时。低新增判据默认：至少 10 页、窗口 ≥ 3 小时、"
                    "正文每页 ≥ 0.3；可在方案里用 `search.low_yield` 覆盖（只接受合理区间内的数字）。",
        "validation": "窗口 1–720 小时；判据看的是**可匹配 JD 正文新增**——卡片在涨但正文为 0 "
                      "会判 `cards_without_jd`，不会因为「卡片多」就算有收益。",
        "effective_at": "读即时；事件最多每天一条（同窗口去重）",
        "enforced_in": "boss_web/services/collection_yield.py + jobs.first_seen_at / jd_first_at",
        "impact": "新增岗位 = `first_seen_at` 在窗口内；正文新增 = 同窗口内 `job_desc` 非空。"
                  "重抓、再次见到、补全正文都不算新增。按日趋势用两条事件轴各自聚合后再合并。",
        "executor": ["host_ai", "program_model"],
        "unsupported": ["按关键词统计新增（采集器只记页码游标，没有归属）"],
    },
    {
        "id": "collection.low_yield",
        "stage": "collection",
        "title": "低新增事件与接续（含调整前后对比）",
        "purpose": "新增偏少时落一条可追踪的事件，等人或 AI 接续诊断；处理时留下"
                   "「改了什么 → 改完指标变成多少 → 保留还是撤回」。",
        "read": ["GET /api/collection/events"],
        "write": ["POST /api/collection/sweep", "POST /api/collection/events/{id}/ack"],
        "example": {"method": "POST", "path": "/api/collection/events/ev_xxx/ack",
                    "body": {"by": "my-ai", "note": "换了两个同义词再采一轮",
                             "keep": True, "hours": 24}},
        "defaults": "事件状态 `pending`（待接续）。采集器与面板执行器都会自动采样"
                    "（执行器 10 分钟一次），不依赖定时发送。`channel` 字段如实说明没有后台 AI 通道。",
        "validation": "事件 id 必须存在；`keep` 三态（true 保留 / false 撤回 / 省略=还没结论）；"
                      "`hours` 1–720，省略用事件自己的窗口。",
        "effective_at": "立即",
        "enforced_in": "runs/collection-events.jsonl（只追加）；ack 时按同一窗口再算一次指标",
        "impact": "ack 会存下 `before` / `after` / `verdict`，接口返回差值与结论文，"
                  "面板事件列表里也能看到「调整后新增 N（前 M，keep）」。"
                  "没有通道时不会假装已自动优化；面板显示待处理条数。",
        "executor": ["host_ai"],
        "unsupported": ["没有宿主 AI 在线时自动唤醒后台 AI（不做，如实标 pending）"],
    },
]

STAGE_LABEL = {
    "resume": "简历与事实", "resume_revision": "简历优化", "plan": "求职目标与采集",
    "matching": "评分", "greeting": "招呼语", "interview": "面试", "send": "任务与发送",
    "runtime": "运行与后台", "collection": "采集效果",
}


def catalog() -> dict[str, Any]:
    """机器可读的完整清单（接口用）。"""
    return {"ok": True, "version": 3, "stages": STAGE_LABEL, "items": CATALOG,
            "rules": [
                "默认值写的是空工作区实际生效的值，不是理想值。",
                "每一项的 enforced_in 是真实读取它的代码位置；只写'能改'不算数。",
                "没实现的能力在 unsupported 里，接口返回不支持时不会假装成功。",
                "发送类能力必须经过既有校验链（正文指纹、去重、冷却、平台限制）。",
                "调优期别建授权：改方案/策略/关键词不需要它，建了反而每改一次就失效。"
                "要边调边发就用不锁版本的轻授权（basis.locked_plan_fields = []）。",
                "授权只决定'发不发'，不拦任何'改'：改方案、换策略、改关键词、重打分、写稿都不经过它。",
            ]}


def render_markdown() -> str:
    """把清单渲染成人看的 Markdown（交付文档用）。"""
    lines = ["# 定制能力清单", "",
             "日期：2026-11-01。来源：`boss_web/services/capabilities.py`（本文由它生成，改代码后重跑）：",
             "",
             "```",
             "python -m boss_web.services.capabilities",
             "```",
             "",
             "读法：每个阶段先看「它能改什么」，再看「改完在哪生效、改坏了谁拒绝」。",
             "**默认值一栏写的是空工作区实际生效的值**——这也是判断「有没有把作者偏好写死」的依据。",
             ""]
    for stage, label in STAGE_LABEL.items():
        items = [item for item in CATALOG if item["stage"] == stage]
        if not items:
            continue
        lines += [f"## {label}", ""]
        for item in items:
            lines += [f"### {item['title']}", "",
                      f"- **用途**：{item['purpose']}",
                      f"- **读**：{'；'.join(f'`{x}`' for x in item['read'])}",
                      f"- **写**：{'；'.join(f'`{x}`' for x in item['write'])}",
                      f"- **默认值**：{item['defaults']}",
                      f"- **校验**：{item['validation']}",
                      f"- **生效时间**：{item['effective_at']}",
                      f"- **生效位置**：`{item['enforced_in']}`",
                      f"- **影响范围**：{item['impact']}",
                      f"- **支持的执行器**：{', '.join(item['executor'])}"]
            if item.get("unsupported"):
                lines.append("- **未实现 / 限制**：" + "；".join(item["unsupported"]))
            example = item.get("example")
            if example:
                lines.append("- **示例**：")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(example, ensure_ascii=False, indent=2))
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    """打印 Markdown 版清单（重定向到 `docs/定制能力清单.md`）。"""
    print(render_markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
