"""网页控制台的路径与默认参数。

状态全在状态库 `data/boss.db`（见 `boss_db`），这里只留静态资源与运行参数。

Attributes:
    PROJECT_ROOT: 项目根目录，与 cwd 无关；打包成 exe 后指向 PyInstaller 解包目录。
    DATA_DIR: 运行时数据目录（状态库 `boss.db` 所在处）；开发态仓库 `data/`，打包态 exe 同目录。
    STATIC_DIR: 静态资源目录；dev = 仓库 `boss_web/static`，打包 = 解包目录同名路径。
    LOGIN_WAIT_TIMEOUT: 登录任务等验证码 / 滑块的人工上限（秒）。
    ANALYZE_INTERVAL: 简历分析时单次 LLM 调用之间的硬间隔（秒），压限流。
    MATCH_CONCURRENCY: 匹配页并行度（同时在途的 LLM 调用数）；调小缓解限流，调大易撞 429。
    DEFAULT_ANALYZE_TOP_K: 分析批量默认看多少个职位。
    DEFAULT_DELIVER_MIN_SCORE: 一键投递默认评分阈值；可在界面改，落 `doc('deliver_config')`。
    LLM_TEMPERATURE: 采样温度。
    LLM_MAX_TOKENS: 通用单次回复 token 上限（招呼语等）。
    LLM_RESUME_PARSE_MAX_TOKENS: 简历结构化解析单次回复 token 上限。
    LLM_RESUME_PARSE_RETRY_MAX_TOKENS: 简历输出被截断时唯一一次重试的 token 上限。
    LLM_MATCH_MAX_TOKENS: 人岗匹配单次回复 token 上限。
    LLM_REVISE_MAX_TOKENS: 简历优化单次回复 token 上限（输出是整份简历）。
    LLM_REVISE_RETRY_MAX_TOKENS: 简历优化输出被截断时唯一一次重试的 token 上限。
    LLM_INTERVIEW_MAX_TOKENS: 面试题生成单次回复 token 上限（一岗 10-15 道题）。
    INTERVIEW_QUESTION_MIN: 每个职位最少生成几道题。
    INTERVIEW_QUESTION_MAX: 每个职位最多生成几道题。
    LLM_TIMEOUT: 单次请求超时（秒）。
"""

from __future__ import annotations

from .runtime import app_data_dir, resource_root

PROJECT_ROOT = resource_root()

DATA_DIR = app_data_dir()

STATIC_DIR = PROJECT_ROOT / "boss_web" / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

LOGIN_WAIT_TIMEOUT = 300.0

ANALYZE_INTERVAL = 0.6

MATCH_CONCURRENCY = 4

# 两段式匹配：先用「批量粗筛」压一行一个岗位的输出，再对够分的岗位做完整分析。
# 依据 2026-10-29 实测：单次调用平均输入 1206 tokens（命中缓存 512）、输出 1116 tokens；
# 输出比输入还贵，而 78% 的岗位最终 <80 分，没必要给它们写 pros/cons/advice。
MATCH_STAGE1_BATCH = 6

MATCH_STAGE1_MAX_TOKENS = 1800

MATCH_STAGE2_MIN_SCORE = 75

# C1：单岗预算与单次调用超时。格式修正/重试共用同一个单岗预算，不各自重新计时。
MATCH_CALL_TIMEOUT = 90

MATCH_JOB_BUDGET = 120

MATCH_RETRY_DELAY = 5

MATCH_MAX_ATTEMPTS = 2

DEFAULT_ANALYZE_TOP_K = 20

DEFAULT_DELIVER_MIN_SCORE = 70

LLM_TEMPERATURE = 0.7
LLM_MAX_TOKENS = 2048
LLM_RESUME_PARSE_MAX_TOKENS = 8192
LLM_RESUME_PARSE_RETRY_MAX_TOKENS = 16384
LLM_MATCH_MAX_TOKENS = 4096
LLM_REVISE_MAX_TOKENS = 16384
LLM_REVISE_RETRY_MAX_TOKENS = 32768
LLM_INTERVIEW_MAX_TOKENS = 8192
INTERVIEW_QUESTION_MIN = 10
INTERVIEW_QUESTION_MAX = 15
LLM_TIMEOUT = 120.0
