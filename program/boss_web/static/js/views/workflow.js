/** 工作流操作与运行记录。 */

import { api } from "../api.js";
import { mountWorkflowStatus } from "./workflow_pages.js";
import { mountCollectionPanel } from "./collection_panel.js";
import { mountScoringProgress, readScoringReport } from "./scoring_progress.js";
import { mountReplyReview } from './reply_review.js';
import { escapeHtml, fmtTime, toast } from "../ui.js";

/** 把数字补成 "n / cap" 的形式；缺值返回 "—"。 */
function ratio(n, cap) {
  if (n === null || n === undefined) return "—";
  return cap ? `${n} / ${cap}` : String(n);
}

/** 秒数转人话。 */
function gapText(sec) {
  const n = Number(sec || 0);
  if (!n) return "—";
  if (n < 60) return `${n} 秒`;
  if (n % 60 === 0) return `${n / 60} 分钟`;
  return `${Math.floor(n / 60)} 分 ${n % 60} 秒`;
}

/** KPI 卡片。 */
function kpi(title, value, sub, pill) {
  const badge = pill ? `<span class="pill ${escapeHtml(pill.cls)}">${escapeHtml(pill.text)}</span>` : "";
  return `
    <div class="card span-4 lift">
      <div class="card-head"><div class="card-title">${escapeHtml(title)}</div>${badge}</div>
      <div class="stat-num">${escapeHtml(value)}</div>
      <div class="stat-label">${escapeHtml(sub)}</div>
    </div>`;
}

/**
 * 渲染工作流面板。
 *
 * @param {HTMLElement} view 内容容器
 * @returns {Function} 换页时调用的清理函数
 */
export async function renderWorkflow(view) {
  view.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">求职进展</h2>
        <div class="card-sub" id="wf-sub">读取中…</div>
      </div>
      <div class="flex center gap-8">
        <span class="muted" id="wf-stamp"></span>
        <button class="btn sm" id="wf-refresh">刷新</button>
      </div>
    </div>
    <div id="wf-controls"></div>
    <div id="wf-collection" class="mb-16"></div>
    <div id="wf-scoring"></div>
    <h3>运行概览</h3>
    <div id="wf-kpi" class="bento mb-16"></div>
    <div class="card mb-16">
      <div class="card-head">
        <div class="card-title">匹配与招呼语进展</div>
        <div class="card-sub" id="wf-sendable-sub">按发送时同一套判断算的</div>
      </div>
      <div id="wf-sendable-body"></div>
    </div>
    <div id="wf-config" class="mb-16" hidden></div>
    <div id="wf-reply-review"></div>
    <section class="card wc-next-step mb-16">
      <div><h3 class="card-title">投递之后，继续和你的 AI 对齐</h3><p class="card-sub">每天看回复变化；确定要面试哪个岗位时，再开始针对性准备。</p></div>
      <div class="wc-followup-grid">
        <div><h4>每天复盘回复</h4><p>结合上方的回复数、读取覆盖和原发送日期，与 AI 判断是否调整采集、评分或招呼语。</p><p class="muted">定时记录需实际启用回复同步任务；只保存时间偏好不会自动执行。</p><a class="btn sm" href="#/review">查看发送记录</a></div>
        <div><h4>收到邀请，再准备面试</h4><p>告诉 AI 公司和岗位，或粘贴邀请；让它找回岗位全文、采用的简历和已发送招呼语。</p><p class="muted">基础题库、对话定制、寻找外部 Skill，由你选择。</p><a class="btn sm" href="#/interview">面试准备</a></div>
      </div>
    </section>
    <details class="wc-history-details"><summary>匹配与执行记录</summary>
    <div class="card mb-16">
      <div class="card-head">
        <div class="card-title">匹配任务历史</div>
        <div class="card-sub" id="wf-mt-sub">最近 15 批 · 与「匹配 → 分析历史」同一份数据</div>
      </div>
      <div id="wf-match-tasks"></div>
    </div>
    <div class="card mb-16">
      <div class="card-head">
        <div class="card-title">最近轮次</div>
        <div class="card-sub">rounds.jsonl</div>
      </div>
      <div id="wf-rounds"></div>
    </div>
    <div class="card">
      <div class="card-head">
        <div class="card-title">台账事件</div>
        <div class="card-sub">greeting/queue.jsonl · 最近 40 条</div>
      </div>
      <div id="wf-events"></div>
    </div>
    </details>`;

  const $ = (id) => view.querySelector("#" + id);
  let timer = null;
  let collectionPanel = null;
  let scoringPanel = null;
  let replyPanel = null;

  /** 拉一次数据并重绘。 */
  async function load() {
    let data;
    try {
      data = await api.get("/api/workflow/overview");
    } catch (err) {
      $("wf-sub").textContent = err.message || "读取失败";
      return;
    }
    if (!data.ok) {
      $("wf-sub").textContent = data.error || "工作流目录不可用";
      $("wf-kpi").innerHTML = `<div class="banner bad">${escapeHtml(data.error || "读取失败")}</div>`;
      return;
    }

    const q = data.quota || {};
    const p = data.corpus || {};
    const cfg = data.config || {};
    const win = data.pricing || {};
    const mt = data.match || {};

    $("wf-sub").textContent = `数据截至 ${data.server_time}`;
    $("wf-stamp").textContent = "每 30 秒自动刷新";

    $("wf-kpi").innerHTML = [
      kpi("今日已发", ratio(q.confirmed, q.daily_cap),
          q.stale ? `新的一天还没发 · 上次（${q.date}）发了 ${q.confirmed_raw ?? 0}`
                  : `未决占用 ${q.reserved ?? 0} · 剩余 ${q.remaining ?? "—"}`,
          q.stale ? { cls: "warn", text: "额度已换日" } : { cls: "ok", text: String(q.date || "") }),
      kpi("语料库", String(p.total_jobs ?? "—"),
          `含 JD ${p.with_jd ?? "—"}（${p.jd_ratio != null ? Math.round(p.jd_ratio * 100) : "—"}%）`,
          { cls: "static", text: `已招呼 ${p.greeted ?? "—"}` }),
      kpi("计价窗口", win.peak ? "高峰" : "半价",
          win.peak ? `只抓取不匹配，${win.next_switch || "稍后"} 起半价`
                   : `匹配照常跑${win.next_switch ? `，${win.next_switch} 转高峰` : ""}`,
          win.peak ? { cls: "warn", text: `现在 ${win.now || ""}` }
                   : { cls: "ok", text: `现在 ${win.now || ""}` }),
    ].join("");

    const gate = cfg.salary_gate || {};
    const roundsCfg = cfg.rounds || {};
    const roundText = Object.entries(roundsCfg)
      .map(([k, v]) => `${k}: 备${v.prepare ?? "—"}/上限${v.cap ?? "—"}${v.refill ? `，补${v.refill_pages ?? "—"}页` : ""}`)
      .join("　");
    $("wf-config").innerHTML = `
      <div class="card">
        <div class="card-head">
          <div class="card-title">当前生效的设置</div>
          <div class="card-sub">${escapeHtml(cfg.workflow_version || "")} · ${escapeHtml(cfg.search_version || "")}</div>
        </div>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead><tr><th>项目</th><th>值</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>发送节奏</td><td>每批 ${escapeHtml(String(cfg.chunk ?? "—"))} 条，间隔 ${escapeHtml(gapText(cfg.chunk_gap))}</td><td>chunk / chunk_gap</td></tr>
              <tr><td>每日额度</td><td>${escapeHtml(String(cfg.daily_cap ?? "—"))} 条</td><td>平台上限，不是目标</td></tr>
              <tr><td>准入分数</td><td>≥ ${escapeHtml(String(cfg.min_score ?? "—"))}</td><td>min_score</td></tr>
              <tr><td>日薪区间</td><td>${escapeHtml(String(gate.daily_min ?? "—"))} – ${escapeHtml(String(gate.daily_max ?? "—"))} 元/天</td><td>salary_gate</td></tr>
              <tr><td>活跃要求</td><td>${escapeHtml(String(cfg.activity_within_days ?? "—"))} 天内活跃</td><td>activity.within_days</td></tr>
              <tr><td>各轮配置</td><td>${escapeHtml(roundText || "—")}</td><td>rounds</td></tr>
              <tr><td>写稿批大小</td><td>${escapeHtml(String(cfg.batch_size ?? "—"))} 条/批</td><td>batch_size · 桥接超时 ${escapeHtml(String(cfg.bridge_timeout_sec ?? "—"))} 秒</td></tr>
              <tr><td>采集节奏</td><td>每 ${escapeHtml(gapText(cfg.cycle_sleep_sec))} 一轮，每轮 ${escapeHtml(String(cfg.pages_per_query ?? "—"))} 页</td><td>cycle_sleep_sec / pages_per_query</td></tr>
              <tr><td>补 JD</td><td>${escapeHtml(String(cfg.jd_batch ?? "—"))} 个 / ${escapeHtml(String(cfg.jd_interval ?? "—"))} 秒</td><td>jd_batch / jd_interval</td></tr>
              <tr><td>探活跃</td><td>${escapeHtml(String(cfg.activity_batch ?? "—"))} 个 / ${escapeHtml(String(cfg.activity_interval ?? "—"))} 秒</td><td>activity_batch / activity_interval</td></tr>
              <tr><td>平台日预算</td><td>${escapeHtml(String(cfg.daily_page_cap ?? "—"))} 页 / ${escapeHtml(String(cfg.daily_jd_cap ?? "—"))} JD</td><td>search.json</td></tr>
              <tr><td>搜索词</td><td>${escapeHtml(String((cfg.queries || []).length))} 个</td><td>${escapeHtml((cfg.queries || []).slice(0, 4).join("，"))}${(cfg.queries || []).length > 4 ? " …" : ""}</td></tr>
            </tbody>
          </table>
        </div>
      </div>`;

    const rows = data.rounds || [];
    const tasks = data.match_tasks || [];
    $("wf-match-tasks").innerHTML = tasks.length ? `
      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>时间</th><th>岗位数</th><th>最高分</th><th>状态</th><th>模型</th></tr></thead>
          <tbody>${tasks.map((t) => `
            <tr>
              <td>${escapeHtml(fmtTime(t.created_at) || "—")}</td>
              <td>${escapeHtml(String(t.job_count ?? "—"))}</td>
              <td>${escapeHtml(String(t.top_score ?? "—"))}</td>
              <td><span class="pill ${t.status === "done" ? "ok" : t.status === "error" ? "bad" : "warn"}">${escapeHtml(String(t.status || ""))}</span></td>
              <td class="muted">${escapeHtml(String(t.model || ""))}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>` : `<div class="empty"><div class="empty-icon">◌</div><p>还没有匹配任务</p></div>`;
    $("wf-rounds").innerHTML = rows.length ? `
      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>时间</th><th>轮次</th><th>准入</th><th>写稿</th><th>成功</th><th>失败</th><th>额度</th><th>配置版本</th></tr></thead>
          <tbody>${rows.map((r) => `
            <tr>
              <td>${escapeHtml(String(r.time || ""))}</td>
              <td>${escapeHtml(String(r.round || ""))}</td>
              <td>${escapeHtml(String(r.eligible ?? "—"))}</td>
              <td>${escapeHtml(String(r.prepared ?? "—"))}</td>
              <td>${escapeHtml(String(r.ok ?? "—"))}</td>
              <td>${escapeHtml(String(r.failed ?? "—"))}${r.aborted ? ` <span class="pill bad">中断</span>` : ""}</td>
              <td>${escapeHtml(`${r.used_before ?? "—"} → ${r.used_after ?? "—"}`)}</td>
              <td class="muted">${escapeHtml(String(r.config_version || ""))}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>` : `<div class="empty"><div class="empty-icon">◌</div><p>还没有轮次记录</p></div>`;

    const evs = data.events || [];
    const pillOf = (name) => {
      const map = { sent: "ok", validated: "ok", revalidated: "ok", failed: "bad", unknown: "warn",
                    voided: "warn", skipped: "warn", reserved: "accent" };
      return `<span class="pill ${map[name] || "static"}">${escapeHtml(String(name || ""))}</span>`;
    };
    $("wf-events").innerHTML = evs.length ? `
      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>时间</th><th>事件</th><th>岗位</th><th>分数</th><th>备注</th></tr></thead>
          <tbody>${evs.map((e) => `
            <tr>
              <td>${escapeHtml(fmtTime(e.at) || "—")}</td>
              <td>${pillOf(e.event)}</td>
              <td class="muted">${escapeHtml(String(e.job_id || "").slice(0, 12))}</td>
              <td>${escapeHtml(String(e.score ?? "—"))}</td>
              <td class="muted">${escapeHtml(e.reason || (e.chars ? `${e.chars} 字` : ""))}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>` : `<div class="empty"><div class="empty-icon">◌</div><p>台账为空</p></div>`;
  }

  /** 拉「现在能发什么」：漏斗 + 可发清单 + 准入待写稿。 */
  async function loadSendable(force) {
    const sub = $("wf-sendable-sub");
    const body = $("wf-sendable-body");
    sub.textContent = "计算中…（要跑一次准入判断，约 3 秒）";
    let d;
    try {
      d = await readScoringReport(force);
    } catch (err) {
      body.innerHTML = `<div class="banner bad">${escapeHtml(err.message || "读取失败")}</div>`;
      sub.textContent = "";
      return;
    }
    if (!d.ok) {
      body.innerHTML = `<div class="banner bad">${escapeHtml(d.error || "计算失败")}</div>`;
      sub.textContent = "";
      return;
    }
    sub.textContent = `${d.cached ? `缓存 ${d.age} 秒` : "刚算完"} · 准入线 ${d.min_score} 分 · 本轮备稿上限 ${d.round_prepare}`;
    const chips = [
      ["库内岗位", d.db_total],
      ["已判匹配", d.analyzed],
      ["准入合格", d.eligible],
      ["可发（已写稿）", d.ready],
      ["待写稿", d.eligible_not_ready],
      [`今日已发 ${d.sent_today}/${d.daily_cap}`, d.sent_today],
    ];
    const funnel = chips.map(([label, value]) => `
      <div class="card span-2" style="padding:12px">
        <div class="stat-num" style="font-size:22px">${escapeHtml(String(value ?? "—"))}</div>
        <div class="stat-label">${escapeHtml(label)}</div>
      </div>`).join("");
    const rows = d.rows || [];
    const pending = d.pending || [];
    body.innerHTML = `
      <div class="mb-16" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:8px">${funnel}</div>
      ${rows.length ? `
        <div class="card-sub mb-8">已校验清单（${rows.length} 条；发送需在“审核与发送”页审核并勾选）</div>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead><tr><th>分数</th><th>公司</th><th>岗位</th><th>字数</th></tr></thead>
            <tbody>${rows.map((r) => `
              <tr><td>${escapeHtml(String(r.score))}</td>
                  <td>${escapeHtml(r.company || "")}</td>
                  <td>${escapeHtml(r.title || "")}</td>
                  <td class="muted">${escapeHtml(String(r.chars))}</td></tr>`).join("")}</tbody>
          </table>
        </div>`
      : `<div class="banner info">还没有写好的稿子。可以和 AI 对齐示例，再生成招呼语。</div>`}
      <div class="wc-actions"><a class="btn primary" href="#/review">查看招呼语与发送候选</a><span class="muted wc-help">这里只展示进展，不会自动发送。</span></div>
      ${pending.length ? `
        <details class="wc-pending-list"><summary>查看待写稿岗位 · ${d.eligible_not_ready} 条（本次读到 ${pending.length} 条）</summary>
        <div class="data-table-wrap">
          <table class="data-table">
            <thead><tr><th>分数</th><th>公司</th><th>岗位</th></tr></thead>
            <tbody>${pending.map((r) => `
              <tr><td>${escapeHtml(String(r.score))}</td>
                  <td>${escapeHtml(r.company || "")}</td>
                  <td>${escapeHtml(r.title || "")}</td></tr>`).join("")}</tbody>
          </table>
        </div></details>` : ""}`;
  }

  $("wf-refresh").addEventListener("click", () => {
    load();
    loadSendable(true);
    collectionPanel?.refresh();
    scoringPanel?.refresh(true);
    replyPanel?.refresh();
  });
  [collectionPanel, scoringPanel] = await Promise.all([mountCollectionPanel($("wf-collection")), mountScoringProgress($("wf-scoring"))]);
  await load();
  replyPanel = await mountReplyReview($("wf-reply-review"));
  const disposeControls = await mountWorkflowStatus($("wf-controls"));
  loadSendable(false).catch(() => {});
  timer = setInterval(() => { load().catch(() => {}); loadSendable(false).catch(() => {}); }, 30000);
  return () => { if (timer) clearInterval(timer); disposeControls(); collectionPanel?.dispose(); scoringPanel?.dispose(); replyPanel?.dispose(); };
}
