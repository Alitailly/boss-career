"""规则范围授权：用户一次性确认「资料/方案版本 + 岗位范围 + 准入规则 + 额度 + 有效期」，
之后由程序在范围内自动发送，不再逐条确认。

和"逐条审批"的区别要说清楚，否则很容易被当成同一种东西：

- 逐条审批记录的是"用户看过**这条正文**"，证据是正文指纹。
- 范围授权记录的是"用户授权了**这个范围**"。它**不**声称用户看过任何一条正文；
  证据是"确认时展示的原文 + 绑定的版本 + 额度与期限"，以及事后可核对：每一条发送
  都落在这个范围内。

因此这里不复制、也不伪造逐条批准：`deliver_round_v2` 的正文指纹校验照旧执行，
范围授权只决定**把哪些岗位交给那条既有校验链**。

可信度边界（与 `Workspace.audit` 同一口径，不夸大）：

- `actor` / `created_by` 只是自报标签，**不能**当作用户确认。
- 确认证据是"预览 → 回执"握手：授权文本由服务端生成并原样存进记录，创建时必须
  回传 `preview_sha` 和一次性 `token`。Skill 无法凭空造出一条"看起来像用户批准过"
  的记录——它必须先取预览，而预览原文会留在记录里，事后能逐字核对。
- 单机本机应用没有第三方签名通道，所以**不能**证明"人一定读过这段文本"。这份记录
  能证明的是：发送的每一条都在某次授权声明的参数之内，且那些参数未被事后改动。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from .errors import InvalidInput, NotFound
from .jsonio import digest

SCHEMA = "authorization/v1"

#: 落库的授权状态。`draft` 只存在于预览表，不进 grants。
STATUSES = ("active", "paused", "revoked", "expired")

#: 预览回执有效期（秒）。过期必须重新预览，避免很久以前看过的一段话被拿来确认。
PREVIEW_TTL = 900

#: 默认锁定的方案字段：改了这些等于换了求职标准，原授权不再覆盖。
#: 未列出的字段（批次、间隔、每批上限等）是运行参数，改了不需要重新授权。
DEFAULT_LOCKED = ("target", "requirements", "salary", "availability", "disclosure",
                  "strategies", "search.keywords", "runtime.min_score")

#: 可锁字段全集。授权里写别的路径会被忽略（只比对这里列出的），避免"看起来锁了其实没锁"。
LOCKABLE = DEFAULT_LOCKED

#: 拒绝原因 → 人能读懂的说法。API 与面板都读这一份，不各自翻译。
REASONS: dict[str, str] = {
    "grant_missing": "找不到这条授权",
    "grant_not_active": "授权不在有效状态",
    "grant_paused": "授权已暂停",
    "grant_revoked": "授权已撤销",
    "grant_expired": "授权已过期",
    "window_not_started": "还没到授权的生效时间",
    "window_ended": "已过授权的有效期",
    "quota_total_exhausted": "授权总额度已用完",
    "quota_daily_exhausted": "今天的额度已用完",
    "plan_changed": "求职方案在授权后被改过（目标/条件/门槛），需要重新确认",
    "candidate_changed": "简历资料在授权后被改过，需要重新确认",
    "strategy_changed": "评分或写稿策略在授权后被换过，需要重新确认",
    "profile_version_mismatch": "面板确认的简历版本与授权时不一致",
    "job_not_in_scope": "岗位不在授权范围内",
    "job_score_missing": "岗位还没有评分",
    "job_below_min_score": "评分低于授权门槛",
    "job_no_jd": "岗位没有可用正文",
    "job_greeting_not_ready": "招呼语还没通过校验",
    "job_excluded_company": "公司命中授权里的排除名单",
    "job_excluded_title": "岗位名命中授权里的排除词",
    "job_stale_activity": "岗位活跃时间超出授权范围",
    "job_employment_not_allowed": "雇佣类型不在授权范围",
    "job_body_missing": "缺少待发正文",
}


def reason_text(code: str) -> str:
    """原因码 → 中文；未登记的原样返回，方便一眼看出漏登记。"""
    return REASONS.get(code, code)


def day_start(now: float) -> float:
    """本地当天 0 点的时间戳；"每日额度"按本机时区算，和面板显示一致。"""
    tm = time.localtime(now)
    return time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday, 0, 0, 0, 0, 0, -1))


def _number(value: Any, name: str, low: float, high: float) -> int:
    """取整数并做范围校验；越界报错而不是夹断（夹断会静默改掉用户的意思）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInput(f"{name} 要是数字", field=name)
    if int(value) != value:
        raise InvalidInput(f"{name} 要是整数", field=name)
    if not low <= value <= high:
        raise InvalidInput(f"{name} 要在 {low}–{high} 之间", field=name)
    return int(value)


def _strings(value: Any, name: str, limit: int) -> list[str]:
    """字符串列表：去空、去重、限长。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise InvalidInput(f"{name} 要是数组", field=name)
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:80])
    if len(out) > limit:
        raise InvalidInput(f"{name} 最多 {limit} 项", field=name)
    return out


def default_spec() -> dict[str, Any]:
    """中性默认：额度小、期限短、门槛跟随方案；不含任何人的真实资料。"""
    return {
        "goal": "",
        "account": {"mode": "unknown", "value": ""},
        "basis": {
            "candidate_id": "", "candidate_revision": 0, "candidate_facts": {},
            "plan_id": "", "plan_revision": 0,
            "locked_plan_fields": list(DEFAULT_LOCKED), "locked_values": {},
            "locked_digest": "",
            "strategies": {"matching": "", "greeting": ""},
            "profile_version": "",
        },
        "tolerance": {"candidate_fact_add": True, "plan_revision_bump": False,
                      "strategy_change": False},
        "jobs": {"mode": "eligible", "job_ids": []},
        "admission": {"min_score": 70, "require_jd": True, "require_greeting_ready": True,
                      "exclude_companies": [], "exclude_titles": []},
        "quota": {"total": 20, "per_day": 5},
        "window": {"start": None, "end": None, "timezone": "local"},
    }


def _merge(base: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """只合并已知键；`candidate_facts` 是映射，按叶子整体替换。"""
    out = dict(base)
    for key, value in (raw or {}).items():
        if (isinstance(value, dict) and isinstance(out.get(key), dict)
                and key != "candidate_facts"):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def normalise_spec(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """补全并校验授权参数。`base` 用于"按上一版改"（改完仍要重新确认）。"""
    if raw is not None and not isinstance(raw, dict):
        raise InvalidInput("授权参数必须是 JSON 对象")
    spec = _merge(_merge(default_spec(), base or {}), raw or {})

    spec["goal"] = str(spec.get("goal") or "").strip()[:200]

    account = spec.get("account") if isinstance(spec.get("account"), dict) else {}
    mode = str(account.get("mode") or "unknown")
    if mode not in {"unknown", "value"}:
        raise InvalidInput("account.mode 只支持 unknown/value", field="account.mode")
    spec["account"] = {"mode": mode, "value": str(account.get("value") or "").strip()[:80]}

    tolerance = spec.get("tolerance") if isinstance(spec.get("tolerance"), dict) else {}
    spec["tolerance"] = {key: bool(tolerance.get(key)) for key in
                         ("candidate_fact_add", "plan_revision_bump", "strategy_change")}

    basis = spec.get("basis") if isinstance(spec.get("basis"), dict) else {}
    raw_locked = basis.get("locked_plan_fields")
    locked = _strings(raw_locked, "basis.locked_plan_fields", 24)
    # 显式给空列表 = "不绑版本，只锁边界"（调优期用）；完全没给 = 保守默认（锁版本）
    if raw_locked is None:
        locked = list(DEFAULT_LOCKED)
    locked = [path for path in locked if path in LOCKABLE]
    facts = basis.get("candidate_facts")
    if facts is not None and not isinstance(facts, dict):
        raise InvalidInput("basis.candidate_facts 要是对象", field="basis.candidate_facts")
    values = basis.get("locked_values")
    if values is not None and not isinstance(values, dict):
        raise InvalidInput("basis.locked_values 要是对象", field="basis.locked_values")
    spec["basis"] = {
        "candidate_id": str(basis.get("candidate_id") or "")[:80],
        "candidate_revision": _number(basis.get("candidate_revision") or 0,
                                      "basis.candidate_revision", 0, 10**9),
        "candidate_facts": {str(k)[:80]: str(v)[:32] for k, v in (facts or {}).items()},
        "plan_id": str(basis.get("plan_id") or "")[:80],
        "plan_revision": _number(basis.get("plan_revision") or 0, "basis.plan_revision", 0, 10**9),
        "locked_plan_fields": locked,
        # 建授权那一刻这些字段的值；执行时逐字段比对，"哪一项变了"要说得出名字
        "locked_values": {str(k): v for k, v in (values or {}).items() if str(k) in LOCKABLE},
        "locked_digest": str(basis.get("locked_digest") or "")[:32],
        "strategies": {key: str((basis.get("strategies") or {}).get(key) or "")[:120]
                       for key in ("matching", "greeting")},
        "profile_version": str(basis.get("profile_version") or "")[:80],
    }

    jobs = spec.get("jobs") if isinstance(spec.get("jobs"), dict) else {}
    mode = str(jobs.get("mode") or "eligible")
    if mode not in {"eligible", "list"}:
        raise InvalidInput("jobs.mode 只支持 eligible/list", field="jobs.mode")
    ids = _strings(jobs.get("job_ids"), "jobs.job_ids", 40)
    if mode == "list" and not ids:
        raise InvalidInput("指定岗位模式必须至少给一个岗位", field="jobs.job_ids")
    spec["jobs"] = {"mode": mode, "job_ids": ids}

    adm = spec.get("admission") if isinstance(spec.get("admission"), dict) else {}
    spec["admission"] = {
        "min_score": _number(adm.get("min_score", 70), "admission.min_score", 0, 100),
        "require_jd": bool(adm.get("require_jd", True)),
        "require_greeting_ready": bool(adm.get("require_greeting_ready", True)),
        "exclude_companies": _strings(adm.get("exclude_companies"),
                                      "admission.exclude_companies", 30),
        "exclude_titles": _strings(adm.get("exclude_titles"), "admission.exclude_titles", 30),
    }

    quota = spec.get("quota") if isinstance(spec.get("quota"), dict) else {}
    spec["quota"] = {
        "total": _number(quota.get("total", 20), "quota.total", 1, 150),
        "per_day": _number(quota.get("per_day", 5), "quota.per_day", 1, 150),
    }

    window = spec.get("window") if isinstance(spec.get("window"), dict) else {}
    start, end = window.get("start"), window.get("end")
    for key, value in (("window.start", start), ("window.end", end)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise InvalidInput(f"{key} 要是时间戳或 null", field=key)
    if start is not None and end is not None and float(start) >= float(end):
        raise InvalidInput("有效期开始必须早于结束", field="window.end")
    spec["window"] = {"start": None if start is None else float(start),
                      "end": None if end is None else float(end),
                      "timezone": str(window.get("timezone") or "local")[:40]}
    return spec


def lock_fields(plan: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """按点号路径把方案投影成"被锁定的那部分"，用来算锁定指纹。

    只认一层点号：`search.keywords` 取 `plan["search"]["keywords"]`，其余取顶层键。
    """
    out: dict[str, Any] = {}
    for path in fields:
        head, _, tail = str(path).partition(".")
        value = plan.get(head)
        out[path] = value.get(tail) if (tail and isinstance(value, dict)) else value
    return out


def fact_fingerprints(facts: Iterable[dict[str, Any]]) -> dict[str, str]:
    """事实 → {编号: 内容指纹}；用于"只追加、不改旧"的容忍判断。"""
    out: dict[str, str] = {}
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        fid = str(fact.get("id") or "").strip()
        if fid:
            out[fid] = digest(str(fact.get("text") or ""))
    return out


def render_text(spec: dict[str, Any], *, labels: dict[str, Any] | None = None,
                now: float | None = None) -> str:
    """把授权参数渲染成"用户确认时看到的那段话"。

    这段文本会原样存进授权记录：事后要能逐字核对"当时到底授权了什么"。
    工程上它也是一份校验清单——凡是会限制发送的东西，都必须出现在这里。
    """
    labels = labels or {}
    now = time.time() if now is None else now
    adm = spec["admission"]
    quota = spec["quota"]
    window = spec["window"]
    basis = spec["basis"]
    tol = spec["tolerance"]

    def stamp(value: float | None) -> str:
        return "不限" if value is None else time.strftime("%Y-%m-%d %H:%M",
                                                          time.localtime(float(value)))

    def names(items: list[str], empty: str = "不限") -> str:
        return "、".join(items) if items else empty

    if spec["jobs"]["mode"] == "eligible":
        scope = "- 岗位范围：当前方案判定合格的岗位（有正文、评分达门槛、招呼语通过校验）"
    else:
        head = "、".join(spec["jobs"]["job_ids"][:6])
        more = "…" if len(spec["jobs"]["job_ids"]) > 6 else ""
        scope = f"- 岗位范围：指定 {len(spec['jobs']['job_ids'])} 个岗位（{head}{more}）"

    tolerance_bits = ["追加简历事实仍算覆盖" if tol["candidate_fact_add"] else "简历事实改动要重新授权"]
    declared = [path for path in (basis.get("locked_plan_fields") or []) if path in LOCKABLE]
    if declared:
        version_line = (f"- 版本绑定：锁定 {names(declared)}（这几项一改，授权失效，要重新确认）")
    else:
        version_line = ("- 版本绑定：不锁版本——方案/策略怎么改都算覆盖，"
                        "只受额度、期限、岗位范围与准入阈值约束")
    if tol["plan_revision_bump"]:
        tolerance_bits.append("方案版本变更仍算覆盖")
    if tol["strategy_change"]:
        tolerance_bits.append("策略变更仍算覆盖")

    lines = [
        "【规则范围授权】确认后，程序会在下面这个范围内自动发送招呼语，不再逐条询问。",
        "",
        f"目标：{spec.get('goal') or labels.get('goal') or '（未填写，仅作说明）'}",
        f"工作区：{labels.get('workspace_name') or '当前工作区'}",
        "账号：" + ("已记录：" + spec["account"]["value"]
                    if spec["account"]["mode"] == "value" else "未登记（按当前登录态执行）"),
        "",
        "一、绑定的资料与方案（这些是发送时依据的标准）",
        f"- 简历：{basis['candidate_id'] or '当前活动资料'} 第 {basis['candidate_revision']} 版"
        + (f"（{labels['candidate_label']}）" if labels.get("candidate_label") else ""),
        f"- 方案：{basis['plan_id'] or '当前活动方案'} 第 {basis['plan_revision']} 版"
        + (f"（{labels['plan_label']}）" if labels.get("plan_label") else ""),
        f"- 策略：评分 {basis['strategies']['matching'] or '内置默认'}；"
        f"写稿 {basis['strategies']['greeting'] or '内置默认'}",
        version_line,
        f"- 容忍范围：{names(tolerance_bits)}",
        "",
        "二、允许发送的岗位",
        scope,
        f"- 评分门槛：≥ {adm['min_score']}",
        "- 必须有岗位正文：" + ("是" if adm["require_jd"] else "否"),
        "- 招呼语必须通过校验：" + ("是" if adm["require_greeting_ready"] else "否"),
        "- 雇佣类型：" + names(adm.get("employment_types") or []),
        "- 排除公司：" + names(adm["exclude_companies"]),
        "- 排除岗位名：" + names(adm["exclude_titles"]),
        "",
        "三、额度与期限",
        f"- 总额度：{quota['total']} 条（用掉或结果未明的都算，取消/没发出去的退回）",
        f"- 每日额度：{quota['per_day']} 条（按本机时区 0 点重置）",
        f"- 生效时间：{stamp(window['start'])}",
        f"- 失效时间：{stamp(window['end'])}（到期自动停止，不自动续）",
        "",
        "四、边界",
        "- 超出上述范围的岗位不发；额度用完即停，程序不会自己加额度。",
        "- 资料或锁定字段在授权后被改动时，发送会暂停并提示重新授权；不锁版本的授权不受影响。",
        "- 可以随时暂停、撤销；暂停期间不发，恢复后继续在剩余额度内执行。",
        "- 每条发送前仍会重新校验正文指纹与平台限制，这条范围授权不替代那些校验。",
    ]
    return "\n".join(lines)


class GrantStore:
    """授权与使用记录。放在工作区里，跟着用户数据走，不跟代码走。

    额度按"尝试次数"计：pending / sent / unknown / failed 都算占用，
    只有明确没发出去的（released）才退回。宁可少发，不因为失误多扣。
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "authorization" / "grants.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, schema TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, created_by TEXT NOT NULL,
                confirmed_at REAL, confirmed_via TEXT NOT NULL DEFAULT '',
                preview TEXT NOT NULL, preview_sha TEXT NOT NULL,
                spec TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                parent TEXT, updated REAL NOT NULL,
                paused_at REAL, paused_reason TEXT,
                revoked_at REAL, revoked_reason TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS previews (
                token TEXT PRIMARY KEY, preview_sha TEXT NOT NULL,
                text TEXT NOT NULL, spec TEXT NOT NULL, created REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS uses (
                id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, job_id TEXT NOT NULL,
                at REAL NOT NULL, body_sha TEXT NOT NULL, task_id TEXT,
                outcome TEXT NOT NULL DEFAULT 'pending', outcome_at REAL,
                note TEXT NOT NULL DEFAULT '')""")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uses_live "
                         "ON uses(grant_id, job_id) WHERE outcome<>'released'")
            conn.execute("CREATE INDEX IF NOT EXISTS uses_task ON uses(task_id)")

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------- 预览 / 创建

    def preview(self, spec: dict[str, Any], *, labels: dict[str, Any] | None = None,
                now: float | None = None, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """生成待确认的授权文本；返回的 `token` + `preview_sha` 是创建时的回执。"""
        now = time.time() if now is None else now
        value = normalise_spec(spec, base=base)
        text = render_text(value, labels=labels, now=now)
        sha = digest(text)
        token = uuid.uuid4().hex[:10]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM previews WHERE created<?", (now - PREVIEW_TTL,))
            conn.execute("INSERT INTO previews(token,preview_sha,text,spec,created) VALUES(?,?,?,?,?)",
                         (token, sha, text, _dumps(value), now))
        return {"token": token, "preview_sha": sha, "text": text, "spec": value,
                "expires_at": now + PREVIEW_TTL}

    def create(self, *, token: str, preview_sha: str, actor: str = "unknown",
               via: str = "", now: float | None = None) -> dict[str, Any]:
        """回执核销 → 生成生效中的授权。回执只能消费一次。"""
        now = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM previews WHERE token=?", (str(token),)).fetchone()
            if row is None:
                raise InvalidInput("确认回执不存在或已过期，请重新预览授权内容",
                                   field="token")
            if str(row["preview_sha"]) != str(preview_sha):
                raise InvalidInput("确认回执与预览内容不一致，请重新预览",
                                   field="preview_sha")
            if now - float(row["created"]) > PREVIEW_TTL:
                conn.execute("DELETE FROM previews WHERE token=?", (str(token),))
                raise InvalidInput("确认回执已过期，请重新预览授权内容", field="token")
            conn.execute("DELETE FROM previews WHERE token=?", (str(token),))
            gid = f"auth_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO grants(id,schema,status,created,created_by,confirmed_at,"
                "confirmed_via,preview,preview_sha,spec,revision,parent,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, SCHEMA, "active", now, str(actor or "unknown")[:80], now,
                 str(via or "api")[:80], row["text"], row["preview_sha"], row["spec"], 1,
                 None, now))
        return self.get(gid)

    def revise(self, gid: str, spec: dict[str, Any], *, token: str, preview_sha: str,
               actor: str = "unknown", via: str = "", now: float | None = None) -> dict[str, Any]:
        """按上一版改出**新的一条**授权；旧的那条自动撤销，不原地改。

        原地改额度等于"AI 自己给自己加额度"，所以只能新建 + 重新确认。
        """
        old = self.get(gid)
        created = self.create(token=token, preview_sha=preview_sha, actor=actor, via=via, now=now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE grants SET parent=?,revision=? WHERE id=?",
                         (old["id"], int(old["revision"]) + 1, created["id"]))
            conn.execute("UPDATE grants SET status='revoked',revoked_at=?,revoked_reason=? WHERE id=?",
                         (time.time() if now is None else now, f"被 {created['id']} 取代", old["id"]))
        created["parent"] = old["id"]
        created["revision"] = int(old["revision"]) + 1
        return created

    # ------------------------------------------------------------- 读 / 状态

    def get(self, gid: str) -> dict[str, Any]:
        """按 id 读授权；不存在抛 `NotFound`。"""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM grants WHERE id=?", (str(gid),)).fetchone()
        if row is None:
            raise NotFound(f"找不到授权：{gid}")
        return _public(row)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """授权列表，新在前。"""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM grants ORDER BY created DESC LIMIT ?",
                                (int(limit),)).fetchall()
        return [_public(row) for row in rows]

    def active(self) -> dict[str, Any] | None:
        """最近一条仍在生效的授权；没有就 None（不是报错）。"""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM grants WHERE status='active' "
                               "ORDER BY created DESC LIMIT 1").fetchone()
        return _public(row) if row else None

    def set_status(self, gid: str, status: str, *, reason: str = "",
                   now: float | None = None) -> dict[str, Any]:
        """暂停 / 恢复 / 撤销。这三个动作只会**缩小**范围，不需要重新确认。"""
        if status not in {"active", "paused", "revoked"}:
            raise InvalidInput("状态只支持 active/paused/revoked", field="status")
        now = time.time() if now is None else now
        grant = self.get(gid)
        if grant["status"] == "revoked" and status != "revoked":
            raise InvalidInput("已撤销的授权不能恢复，只能重新授权", field="status")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if status == "paused":
                conn.execute("UPDATE grants SET status='paused',paused_at=?,paused_reason=?,"
                             "updated=? WHERE id=?", (now, str(reason or "")[:200], now, gid))
            elif status == "revoked":
                conn.execute("UPDATE grants SET status='revoked',revoked_at=?,revoked_reason=?,"
                             "updated=? WHERE id=?", (now, str(reason or "")[:200], now, gid))
            else:
                conn.execute("UPDATE grants SET status='active',paused_at=NULL,"
                             "paused_reason=NULL,updated=? WHERE id=?", (now, gid))
        return self.get(gid)

    # ------------------------------------------------------------- 用量

    def usage(self, gid: str, *, now: float | None = None) -> dict[str, Any]:
        """用了多少、还剩多少。按尝试计，不按成功计（失败也占过一次请求）。"""
        now = time.time() if now is None else now
        start = day_start(now)
        with self.connect() as conn:
            rows = conn.execute("SELECT outcome,at FROM uses WHERE grant_id=? AND outcome<>'released'",
                                (str(gid),)).fetchall()
        grant = self.get(gid)
        total = int(grant["spec"]["quota"]["total"])
        per_day = int(grant["spec"]["quota"]["per_day"])
        used = len(rows)
        today = sum(1 for row in rows if float(row["at"]) >= start)
        counts: dict[str, int] = {}
        for row in rows:
            counts[str(row["outcome"])] = counts.get(str(row["outcome"]), 0) + 1
        return {"used_total": used, "used_today": today,
                "left_total": max(0, total - used), "left_today": max(0, per_day - today),
                "quota_total": total, "quota_per_day": per_day,
                "by_outcome": counts, "day_start": start}

    def rows(self, gid: str, limit: int = 200) -> list[dict[str, Any]]:
        """这条授权的逐条使用记录（新在前），用于对账与审计。"""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM uses WHERE grant_id=? ORDER BY at DESC LIMIT ?",
                                (str(gid), int(limit))).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------- 判定

    def evaluate(self, grant: dict[str, Any] | str, *, snapshot: dict[str, Any],
                 jobs: Iterable[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
        """对一批岗位做完整判定：授权状态 → 版本漂移 → 有效期 → 额度 → 逐条准入。

        返回结构（不抛异常，让调用方一次看全所有原因）::

            {"grant_id","ok","blocking":[原因码],"drift":[原因码],"window":[原因码],
             "quota":{...},"allowed":[{job_id,body_sha}],"blocked":[{job_id,reasons}],
             "checked":数量}
        """
        now = time.time() if now is None else now
        # 一律按 id 重读：调用方手里那份可能是暂停之前取的，拿旧状态判定会误发。
        try:
            grant = self.get(grant if isinstance(grant, str) else grant["id"])
        except (KeyError, NotFound):
            pass
        spec = grant["spec"]
        blocking: list[str] = []
        drift: list[str] = []
        window: list[str] = []

        if grant["status"] == "paused":
            blocking.append("grant_paused")
        elif grant["status"] == "revoked":
            blocking.append("grant_revoked")
        elif grant["status"] == "expired":
            blocking.append("grant_expired")
        elif grant["status"] != "active":
            blocking.append("grant_not_active")

        start = spec["window"]["start"]
        end = spec["window"]["end"]
        if start is not None and now < float(start):
            window.append("window_not_started")
        if end is not None and now >= float(end):
            window.append("window_ended")
        blocking.extend(window)

        drift_reasons, changed_fields = self._drift(spec, snapshot)
        drift.extend(drift_reasons)
        blocking.extend(drift)

        usage = self.usage(grant["id"], now=now)
        if usage["left_total"] <= 0:
            blocking.append("quota_total_exhausted")
        if usage["left_today"] <= 0:
            blocking.append("quota_daily_exhausted")

        remaining = min(usage["left_total"], usage["left_today"])
        allowed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        seen: set[str] = set()
        for job in jobs or []:
            jid = str((job or {}).get("job_id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            reasons = _job_reasons(spec, job)
            if reasons:
                blocked.append({"job_id": jid, "reasons": reasons})
            elif len(allowed) < remaining:
                allowed.append({"job_id": jid, "body_sha": str(job.get("body_sha") or "")})
            else:
                blocked.append({"job_id": jid, "reasons": ["quota_total_exhausted"]})

        return {"grant_id": grant["id"], "ok": not blocking,
                "blocking": _unique(blocking), "drift": _unique(drift),
                "window": _unique(window), "quota": usage,
                "allowed": allowed, "blocked": blocked,
                "checked": len(seen),
                # 「哪一项变了」要说得出名字，AI 才能一次问对问题，而不是让用户反复猜
                "changed_fields": changed_fields,
                "locked_fields": list(spec["basis"].get("locked_plan_fields") or []),
                "reasons_text": _reason_texts(blocking, changed_fields)}

    def _drift(self, spec: dict[str, Any], snapshot: dict[str, Any]) -> tuple[list[str], list[str]]:
        """授权时绑定的是什么，现在还是不是。返回 `(原因码, 变了的锁定字段)`。"""
        out: list[str] = []
        changed_fields: list[str] = []
        basis = spec["basis"]
        tol = spec["tolerance"]
        declared = [path for path in (basis.get("locked_plan_fields") or []) if path in LOCKABLE]

        cand = snapshot.get("candidate") or {}
        if basis["candidate_id"] and str(cand.get("id") or "") != basis["candidate_id"]:
            out.append("candidate_changed")
        else:
            now_facts = cand.get("candidate_facts") or {}
            old_facts = basis["candidate_facts"] or {}
            if old_facts:
                same = all(now_facts.get(k) == v for k, v in old_facts.items())
                if not same:
                    out.append("candidate_changed")
                elif set(now_facts) - set(old_facts) and not tol["candidate_fact_add"]:
                    # 追加了新事实：默认允许（只会让写稿更准，且正文仍要过校验）
                    out.append("candidate_changed")

        plan = snapshot.get("plan") or {}
        now_values = plan.get("locked_values") or {}
        old_values = basis.get("locked_values") or {}
        # `locked_plan_fields` 为空 = 只锁边界（额度/期限/岗位范围/准入阈值），不绑版本。
        # 这是调优期要用的模式：用户和 AI 在改方案时不该每改一次就重新授权一次。
        if declared and not tol["plan_revision_bump"]:
            changed = [path for path in declared if now_values.get(path) != old_values.get(path)]
            if not changed and not now_values and basis["locked_digest"]:
                # 老记录里只有指纹没有逐字段值：退化成整串比对
                if str(plan.get("locked_digest") or "") != basis["locked_digest"]:
                    changed = ["（整组）"]
            if changed:
                out.append("plan_changed")
                changed_fields.extend(changed)

        if "strategies" in declared:
            strategies = snapshot.get("strategies") or {}
            for key in ("matching", "greeting"):
                want = basis["strategies"].get(key) or ""
                have = str(strategies.get(key) or "")
                if want != have and not tol["strategy_change"]:
                    out.append("strategy_changed")
                    break

        want_profile = basis["profile_version"]
        have_profile = str(snapshot.get("profile_version") or "")
        if want_profile and want_profile != have_profile:
            out.append("profile_version_mismatch")
        return _unique(out), changed_fields

    # ------------------------------------------------------------- 占用 / 结算

    def reserve(self, gid: str, allowed: Iterable[dict[str, Any]], *, task_id: str,
                now: float | None = None) -> list[dict[str, Any]]:
        """把判定通过的岗位登记成"要发"，同时原子地再核一次额度。

        `evaluate` 与真正创建任务之间可能有别的操作抢先消耗额度，所以这里在
        `BEGIN IMMEDIATE` 里重算一遍；抢不到的不给。
        """
        now = time.time() if now is None else now
        grant = self.get(gid)
        if grant["status"] != "active":
            raise InvalidInput("授权不在有效状态，不能占用额度", field="grant")
        start = day_start(now)
        total = int(grant["spec"]["quota"]["total"])
        per_day = int(grant["spec"]["quota"]["per_day"])
        taken: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT job_id,at FROM uses WHERE grant_id=? AND outcome<>'released'",
                                (gid,)).fetchall()
            used = len(rows)
            today = sum(1 for row in rows if float(row["at"]) >= start)
            have = {str(row["job_id"]) for row in rows}
            for item in allowed or []:
                jid = str(item.get("job_id") or "")
                if not jid or jid in have:
                    continue
                if used >= total or today >= per_day:
                    break
                conn.execute("INSERT INTO uses(id,grant_id,job_id,at,body_sha,task_id,outcome) "
                             "VALUES(?,?,?,?,?,?, 'pending')",
                             (f"use_{uuid.uuid4().hex[:12]}", gid, jid, now,
                              str(item.get("body_sha") or ""), str(task_id), ))
                taken.append({"job_id": jid, "body_sha": str(item.get("body_sha") or "")})
                have.add(jid)
                used += 1
                today += 1
        return taken

    def settle(self, task_id: str, outcomes: dict[str, str], *, note: str = "",
               now: float | None = None) -> int:
        """任务结束后把每条的实际结果写回去。

        `outcomes` 是 `{job_id: sent|failed|unknown|released}`；没提到的保持 pending，
        宁可让额度被"未知"占着，也不默认成功或默认退回。
        """
        now = time.time() if now is None else now
        changed = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for jid, outcome in (outcomes or {}).items():
                if outcome not in {"sent", "failed", "unknown", "released"}:
                    raise InvalidInput("结果只支持 sent/failed/unknown/released", field="outcome")
                cur = conn.execute("UPDATE uses SET outcome=?,outcome_at=?,note=? "
                                   "WHERE task_id=? AND job_id=?", (outcome, now,
                                                                    str(note)[:200],
                                                                    str(task_id), str(jid)))
                changed += int(cur.rowcount or 0)
        return changed

    def release_task(self, task_id: str, *, keep: Iterable[str] = (),
                     now: float | None = None) -> int:
        """任务没发出去（被取消/提前失败）：把还是 pending 的退回额度。"""
        now = time.time() if now is None else now
        keep_set = {str(x) for x in keep or []}
        freed = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT id,job_id FROM uses WHERE task_id=? AND outcome='pending'",
                                (str(task_id),)).fetchall()
            for row in rows:
                if str(row["job_id"]) in keep_set:
                    continue
                conn.execute("UPDATE uses SET outcome='released',outcome_at=? WHERE id=?",
                             (now, row["id"]))
                freed += 1
        return freed


def _job_reasons(spec: dict[str, Any], job: dict[str, Any]) -> list[str]:
    """单个岗位的准入判定；返回空列表才算通过。"""
    adm = spec["admission"]
    reasons: list[str] = []

    if spec["jobs"]["mode"] == "list":
        if str(job.get("job_id") or "") not in set(spec["jobs"]["job_ids"]):
            reasons.append("job_not_in_scope")

    score = job.get("score")
    if score is None:
        reasons.append("job_score_missing")
    elif float(score) < float(adm["min_score"]):
        reasons.append("job_below_min_score")

    if adm["require_jd"] and not job.get("has_jd"):
        reasons.append("job_no_jd")
    if adm["require_greeting_ready"] and not job.get("greeting_ready"):
        reasons.append("job_greeting_not_ready")

    within = int(adm.get("activity_within_days") or 0)
    if within:
        days = job.get("active_days")
        if days is None or float(days) > within:
            reasons.append("job_stale_activity")

    kinds = adm.get("employment_types") or []
    if kinds and str(job.get("employment") or "") not in kinds:
        reasons.append("job_employment_not_allowed")

    company = str(job.get("company") or "")
    title = str(job.get("title") or "")
    for word in adm["exclude_companies"]:
        if word and word in company:
            reasons.append("job_excluded_company")
            break
    for word in adm["exclude_titles"]:
        if word and word in title:
            reasons.append("job_excluded_title")
            break

    if not str(job.get("body_sha") or ""):
        reasons.append("job_body_missing")
    return reasons


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _reason_texts(blocking: Iterable[str], changed_fields: Iterable[str]) -> list[str]:
    """原因码 → 人话；"方案变了"要顺带说清哪几项变了，否则用户只能反复试。"""
    changed = list(changed_fields or [])
    texts: list[str] = []
    for code in _unique(blocking):
        text = reason_text(code)
        if code == "plan_changed" and changed:
            text += "（变的是：" + "、".join(changed) + "）"
        if text not in texts:
            texts.append(text)
    return texts


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _public(row: sqlite3.Row) -> dict[str, Any]:
    """数据库行 → 对外字典；`spec` 与 `preview` 解析开，`preview` 保持原文。"""
    import json
    out = dict(row)
    out["spec"] = json.loads(out["spec"])
    out["schema"] = SCHEMA
    return out
