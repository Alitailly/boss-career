# BOSS 求职助手 · AI 驱动版

一套「程序 + Skill」的求职工作流：程序只提供**能被执行的东西**，方法交给**用户和用户自己的 AI**，面板只负责**把结果给用户看**。

```
登录 ──► 采集岗位 ──► 简历匹配评分 ──► AI 写招呼语 ──► 授权发送 ──► 每日复盘
                                                              └──► 面试准备
```

## 为什么这样设计

求职偏好高度个人化——采集什么城市、什么算好岗位、招呼语用什么口吻、面试怎么练，程序不可能预设所有人的答案。

所以这里做了一个分工：

| 层 | 由谁负责 | 提供什么 |
|---|---|---|
| **程序** `program/` | 代码 | 数据源、模板、执行接口、台账、可读面板。不替你猜偏好 |
| **Skill** `skill/boss-career/` | 用户自己的 AI | 把通用骨架变成"这个人的求职流程"：对齐条件、校准评分、定制文案 |
| **面板** | 用户 | 只看结果：采到什么、评了多少分、文案长什么样、回复率如何 |

程序和 Skill 是配套的：Skill 告诉 AI 该在什么时候问什么、什么时候写回程序、什么不算"已经生效"。面板不承担调优，调优发生在用户和 AI 的对话里。

## 目录

```
program/                 程序本体（FastAPI + SQLite，本地运行）
├── boss_login/          登录（短信 / 扫码）、本地会话
├── boss_jobs/           岗位抓取、详情、请求节奏控制、安全网关令牌
├── boss_filter/         平台筛选项目录与校验
├── boss_core/           方案编译、快照、条件判定
├── boss_db/             SQLite 表结构与存取
├── boss_web/            Web 服务与面板（static/ 前端，api/ 接口，services/ 业务）
└── workflow/            采集、补全文、写稿、发送等执行脚本

skill/boss-career/       配套 Skill（给用户 AI 读）
├── SKILL.md             工作流与各环节可定制方向
├── references/          程序接入、评分对齐、个人规则、账号异常处理
└── agents/openai.yaml   入口配置
```

`program/CONTEXT.md` 是这套设计的词表：用户 AI、配套 Skill、个人方案、评分模型、运行结果、求职面板——几个容易混淆的概念在这里被固定下来。

## 快速开始

环境：Python 3.10+、Chrome/Edge、一个能收短信的手机号。

```bash
cd program
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -r requirements.txt

python -m boss_web
# 默认 http://127.0.0.1:8787
```

打开后：登录 → 采集条件 → 上传简历 → 配置模型（可选）→ 看匹配 → 审核招呼语 → 授权发送。

配上 Skill 使用时，把 `skill/boss-career/` 交给你的 AI，让它读 `SKILL.md` 后带着你走——条件、评分标准、文案偏好都在对话里对齐，AI 负责写回程序。

## 两条执行路线

程序自己有后台模型（在「AI 与设置 → 模型与 API」里配 OpenAI 兼容接口），也能由**用户 AI 直接执行并回写**：

- 程序后台跑：适合批量评分、批量写稿
- 用户 AI 跑：适合先小批校准、或用户不想额外配 Key

两条路线共用同一份个人方案，结果都落在同一套台账里。

## 免责声明

本项目仅供**学习与技术研究**使用。使用即表示你同意：

1. 遵守 BOSS 直聘的用户协议与法律法规，账号行为自负；
2. 不得用于批量骚扰、数据倒卖或任何商业牟利用途；
3. 控制请求频率，不要对目标站点造成压力；
4. 因使用本项目导致的账号限制、封禁或法律后果，作者不承担责任。

平台若认为本项目不妥，联系即下架。

## 许可与来源

MIT。程序部分基于 [boss-automatic-delivery](https://github.com/handsomekai924/boss-automatic-delivery)（MIT）改造：保留其抓取、登录与投递能力，增加 AI 驱动的方案/评分/文案层与配套 Skill。
