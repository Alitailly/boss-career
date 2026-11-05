/** Visible, operable workflow controls backed by the same execution ledger. */
import { api } from "../api.js";
import { escapeHtml as e, modal, scoreRing, toast } from "../ui.js";
import { scoringQueueHtml } from './scoring_progress.js';
import { wireSections } from '../panel_ui.js';
import { scoringRuleHtml } from './stage_inputs.js';
import { openGreetingReview } from './greeting_review.js';

const labels = { collect: "采集一轮", match: "匹配已有岗位", prepare: "生成招呼语", send: "发送审核批次", feedback: "同步回复" };
const statuses = { queued: "等待执行", running: "执行中", done: "已完成", failed: "失败", cancelled: "已取消", expired: "已过期，未执行", needs_review: "异常中断，队列暂停" };
const when = n => n ? new Date(n * 1000).toLocaleString("zh-CN") : "—";

export async function mountControls(host, mode = "tasks") {
  host.innerHTML = `
    <div id="control-error" role="status"></div>
    <p data-region="tasks" id="control-live-match" role="status"></p>
    <div class="wf-control-grid">
      <section data-region="candidate" class="card"><h3>当前求职资料</h3>
        <label class="field">使用的简历<select id="control-resume" aria-label="使用的简历"></select></label>
        <div class="flex gap-8 wrap"><button class="btn" id="control-open-resume">上传 / 查看 / 解析</button><button class="btn" id="control-preview">对照原文核对事实</button></div>
        <p id="control-active" class="muted"></p>
        <div id="control-facts" hidden><div class="banner warn">这些事实来自 AI 提取或人工修改，采用只影响本程序的求职资料，不改原文件与 BOSS 简历。</div><details open><summary>上传原文（提取文本）</summary><pre id="control-source" class="wf-result"></pre></details><details><summary>当前已采用的事实</summary><pre id="control-current-facts" class="wf-result"></pre></details><label class="field">允许写入招呼语的事实（每行一条，可修改）<textarea id="control-facts-text" rows="8"></textarea></label>
          <label><input type="checkbox" id="control-facts-confirm"> 我已核对这些事实，项目归属和表达强度准确</label>
          <button class="btn primary" id="control-activate">用于后续匹配与写稿</button>
        </div>
      </section>
      <section data-region="execution" class="card"><h3>采集与执行设置</h3>
        <label class="field">搜索关键词（每行一个）<textarea id="control-queries" rows="3"></textarea></label>
        <button class="btn sm" id="control-save-search">保存搜索词</button> <a href="#/jobs">更多岗位筛选</a>
        <div class="wf-fields">
          <label>最低匹配分<input id="control-score" type="number" min="0" max="100"></label>
          <label>每日发送上限<input id="control-cap" type="number" min="1" max="150"></label>
          <label>每批发送数<input id="control-chunk" type="number" min="1" max="25"></label>
          <label>批次间隔（秒）<input id="control-gap" type="number" min="0"></label>
          <label>每批写稿数<input id="control-batch" type="number" min="1" max="15"></label>
        </div><button class="btn" id="control-save">保存执行设置</button>
      </section>
    </div>
    <section data-region="tasks" class="card mb-16"><h3>执行与安排时间</h3>
      <div class="flex gap-12 wrap center">
        <label>执行时间 <input id="control-due" type="datetime-local" aria-label="执行时间"></label><span class="muted">留空 = 立即</span>
        <label data-region="tasks">本次最多写稿 <input id="control-limit" type="number" min="1" max="30" value="6" style="width:80px"></label>
      </div>
      <div data-region="tasks" class="flex gap-8 wrap mt-16">
        <button class="btn" data-action="collect">采集一轮</button><button class="btn" data-action="match">匹配已有岗位</button>
        <button class="btn primary" data-action="prepare">生成招呼语（不发送）</button><button class="btn" data-action="feedback">同步回复</button>
      </div>
    </section>
    <section data-region="review" class="card mb-16" id="wf-review">
      <div class="flex between center wrap"><h3>审核与发送</h3>
        <div class="flex gap-8 center"><span class="muted" id="control-review-busy"></span><span class="muted" id="control-review-stamp"></span><button class="btn sm" id="control-load-drafts">刷新</button></div></div>
      <div class="wf-funnel" id="control-funnel"></div>
      <div id="control-delivery-state"></div>
      <details class="review-coverage"><summary>查看从入库、评分到写稿的完整数量</summary><div class="wf-coverage" id="control-coverage"></div></details>
      <div id="control-review-hint"></div>
      <div class="section-tabs" role="tablist" aria-label="写稿与发送查看方式"><button type="button" role="tab" id="review-tab-ready" data-section-tab="ready" aria-controls="wf-stage-ready">待审核发送</button><button type="button" role="tab" id="review-tab-pending" data-section-tab="pending" aria-controls="wf-stage-pending">待写稿与问题</button><button type="button" role="tab" id="review-tab-history" data-section-tab="history" aria-controls="wf-send-history">发送记录</button></div>
      <div class="wf-stage" id="wf-stage-pending" data-section-panel="pending" role="tabpanel" aria-labelledby="review-tab-pending" hidden>
        <div class="flex between center wrap wf-stage-head">
          <h4>评分通过，待写稿或重写 <span class="pill static" id="control-pending-count">0</span></h4>
          <div class="flex gap-8 center">
            <label class="muted">本次写 <input id="control-write-limit" type="number" min="1" max="30" value="6" style="width:72px"> 条</label>
            <button class="btn primary" id="control-write">写招呼语（不发送）</button>
          </div>
        </div>
        <p class="muted" id="control-pending-note">评分与写稿是两个任务。启动写稿并通过校验后，才进入待发送；AI 可按已确认规则分批补齐，写稿本身不会发送。</p>
        <div id="control-pending-reasons" class="wf-reason-chips"></div>
        <div id="control-pending-list"></div>
      </div>
      <div class="wf-stage" id="wf-stage-ready" data-section-panel="ready" role="tabpanel" aria-labelledby="review-tab-ready">
        <div class="flex between center wrap wf-stage-head">
          <h4>已写好，待审核发送 <span class="pill static" id="control-ready-count">0</span></h4>
          <div class="flex gap-8 center">
            <label class="muted"><input type="checkbox" id="wf-select-all"> 全选</label>
            <button class="btn primary" id="control-send">发送所选（0）</button>
          </div>
        </div>
        <p class="muted" id="control-ready-note">这里就是这一轮会真发出去的稿子：正文全文、分数、字数都在卡上，勾选后点发送。</p>
        <div id="control-drafts"></div>
      </div>
    </section>
    <section data-region="review" class="card mb-16" id="wf-send-history" data-section-panel="history" role="tabpanel" aria-labelledby="review-tab-history" hidden><div class="flex between center wrap"><h3>实际发送记录</h3><button class="btn sm ghost" id="control-refresh-history">刷新</button></div><p class="section-note">正文预览只显示两行；点击岗位查看完整 JD、匹配理由与实际发送内容。</p><div id="control-history"></div></section>
    <section data-region="tasks" class="card mb-16" id="wf-schedule">
      <div class="flex between center wrap"><h3>今天的安排</h3><span class="muted" id="control-schedule-stamp"></span></div>
      <div id="control-schedule"></div>
    </section>
    <section data-region="tasks" class="card mb-16" id="wf-recipe-run">
      <div class="flex between center wrap"><h3>打分 / 写稿 / 发送</h3><span class="muted" id="rr-status"></span></div>
      <div class="wf-fields">
        <label>最低匹配分<input id="rr-score" type="number" min="0" max="100"></label>
        <label>每日发送上限<input id="rr-cap" type="number" min="1" max="150"></label>
        <label>每批发送数<input id="rr-chunk" type="number" min="1" max="25"></label>
        <label>批次间隔（秒）<input id="rr-gap" type="number" min="0"></label>
        <label>每批写稿数<input id="rr-batch" type="number" min="1" max="15"></label>
      </div>
      <div class="wf-rule-line" id="rr-rules"></div>
      <button class="btn" id="rr-save">保存执行参数</button>
    </section>
    <section data-region="tasks" class="card mb-16" id="wf-task-list"><div class="flex between center"><h3>面板任务记录</h3><button class="btn sm" id="control-refresh-tasks">刷新任务</button></div>
      <div id="control-tasks"></div>
    </section>`;
  host.querySelectorAll('[data-region]').forEach(node => { node.hidden = !node.dataset.region.split(' ').includes(mode); });
  const grid = host.querySelector('.wf-control-grid');
  grid.hidden = !['candidate', 'execution'].includes(mode);
  if (!grid.hidden) grid.style.gridTemplateColumns = '1fr';
  let showReviewSection = null;
  if (mode === 'review') {
    // 放在同一面板内切换，选择与实时数据不会因切页丢失。
    host.querySelector('#wf-review').appendChild(host.querySelector('#wf-send-history'));
    const requested = new URLSearchParams(location.hash.split('?')[1] || '').get('tab');
    showReviewSection = wireSections(host, { initial:['ready','pending','history'].includes(requested) ? requested : 'ready' });
  }
  if (mode === 'tasks') {
    const manual = document.createElement('details');
    manual.className = 'manual-tools';
    manual.innerHTML = '<summary>手动执行与参数（可选，通常交给 AI）</summary><div class="manual-tools-body"></div>';
    const target = manual.querySelector('.manual-tools-body');
    const execute = host.querySelector('#control-due').closest('section');
    target.append(execute, host.querySelector('#wf-recipe-run'));
    host.querySelector('#wf-task-list').after(manual);
    host.querySelector('#wf-schedule').before(host.querySelector('#wf-task-list'));
  }
  const $ = id => host.querySelector("#" + id);
  let context = null, preview = null, review = null, disposed = false;
  const chosen = new Set();  // 已勾选待发送的 job_id
  let pendingOpen = false;   // ① 欠稿列表是否展开全部
  let lastReviewAt = 0;      // 上次拉待发清单的时间（用于自动刷新节流）
  let lastReviewSig = "";    // 上次渲染的数据指纹：没变就不重画
  const historyIndex = new Map();  // 卡片 key → 这条发送记录，点开弹窗用
  let matchState = {};             // 最近一次匹配任务快照（"今天的安排"里要用）
  const error = err => { if (!disposed) $("control-error").innerHTML = `<div class="banner bad">${e(err.message || String(err))}</div>`; };
  async function action(button, fn) {
    button.disabled = true;
    $("control-error").textContent = "";
    try { if (!context && button.id !== "control-open-resume") throw new Error("工作流尚未就绪，请先按上方错误提示完成配置并刷新页面"); await fn(); } catch (err) { error(err); } finally { if (!disposed) button.disabled = false; }
  }
  function bind(id, fn) { $(id).addEventListener("click", event => action(event.currentTarget, fn)); }
  function due() {
    const text = $("control-due").value;
    if (!text) return null;
    const value = new Date(text).getTime() / 1000;
    if (!Number.isFinite(value) || value <= Date.now() / 1000) throw new Error("请选择未来的执行时间");
    return value;
  }
  async function loadContext() {
    const selected = $("control-resume").value;
    context = await api.get("/api/workflow/control/context");
    $("control-resume").innerHTML = `<option value="">请选择简历</option>` + context.resumes.map(r => `<option value="${e(r.resume_id)}">${e(r.title || r.filename || r.resume_id)}</option>`).join("");
    $("control-resume").value = selected || context.active_resume_id || "";
    $("control-active").textContent = `当前生效简历：${context.resumes.find(r => r.resume_id === context.active_resume_id)?.title || context.active_resume_id || "未选择"} · 资料版本：${context.profile.version}`;
    $("control-queries").value = (context.search.queries || []).join("\n");
    for (const [id, key] of [["score", "min_score"], ["cap", "daily_cap"], ["chunk", "chunk"], ["gap", "chunk_gap"], ["batch", "batch_size"]]) $("control-" + id).value = context.config[key] ?? "";
  }
  $("control-resume").addEventListener("change", () => { preview = null; $("control-facts").hidden = true; });
  bind("control-open-resume", async () => { location.hash = "/resume?resume=" + encodeURIComponent($("control-resume").value); });
  bind("control-preview", async () => {
    if (!$("control-resume").value) throw new Error("请先选择简历");
    preview = await api.get("/api/workflow/control/resume/" + encodeURIComponent($("control-resume").value) + "/preview");
    $("control-source").textContent = preview.source_text;
    $("control-current-facts").textContent = preview.active_facts.map(f => f.text).join("\n") || '这份简历尚未采用事实';
    $("control-facts-text").value = (preview.active_facts.length ? preview.active_facts : preview.facts).map(f => f.text).join("\n");
    $("control-facts-confirm").checked = false;
    $("control-facts").hidden = false;
  });
  bind("control-activate", async () => {
    if (!preview || !$("control-facts-confirm").checked) throw new Error("请先核对并确认简历事实");
    const facts = $("control-facts-text").value.split("\n").map(x => x.trim()).filter(Boolean).map((text, i) => ({ id: "F" + (i + 1), text }));
    await api.put("/api/workflow/control/candidate", { resume_id: preview.resume_id, source_sha: preview.source_sha, expected_version: context.profile.version, facts, confirmed: true });
    $("control-facts").hidden = true;
    await loadContext(); clearDrafts(); toast("已采用核实后的简历事实", "ok");
  });
  bind("control-save-search", async () => {
    await api.put("/api/workflow/control/search", { expected_version: context.search.version, queries: $("control-queries").value.split("\n") });
    await loadContext(); toast("搜索词已保存，下一轮采集生效", "ok");
  });
  bind("control-save", async () => {
    const body = { expected_version: context.config.version };
    for (const [id, key] of [["score", "min_score"], ["cap", "daily_cap"], ["chunk", "chunk"], ["gap", "chunk_gap"], ["batch", "batch_size"]]) body[key] = Number($("control-" + id).value);
    await api.put("/api/workflow/control/settings", body);
    await loadContext(); clearDrafts(); toast("执行设置已保存", "ok");
  });
  host.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", () => action(button, async () => {
    const kind = button.dataset.action, scheduled = due();
    if (!window.confirm(`${labels[kind]}${scheduled ? "，安排于 " + when(scheduled) : "，立即开始"}？${kind === "match" || kind === "prepare" ? "这会使用你配置的 AI。" : ""}`)) return;
    await api.post("/api/workflow/control/tasks", { action: kind, due: scheduled, limit: Number($("control-limit").value), confirmed: true });
    await loadTasks(); toast("任务已加入执行列表", "ok");
  })));
  // ---- 审核与发送：够分 → 写稿 → 待审 → 发送，四步在这一页都能看见 ----
  const tile = (label, value, hint = "", tone = "") =>
    `<div class="wf-tile${tone ? " " + tone : ""}"><span class="wf-tile-k">${e(label)}</span><span class="wf-tile-v">${value}</span>${hint ? `<span class="wf-tile-h">${e(hint)}</span>` : ""}</div>`;
  function sendCount() { return (review?.rows || []).filter(r => chosen.has(r.job_id)).length; }
  function updateSendLabel() {
    const n = sendCount();
    $("control-send").textContent = `发送所选（${n}）`;
    $("control-send").disabled = !n;
  }
  /** 当前简历名字（用于标出"这条分数是旧简历打的"）。 */
  function currentResumeTitle() {
    const id = context?.active_resume_id;
    return (context?.resumes || []).find(r => r.resume_id === id)?.title || "";
  }
  /** 分数来自别的简历时，在卡片标题后面标一下——不隐藏，也不当作无效。 */
  function oldScoreChip(row) {
    const by = String(row?.scored_by || "");
    if (!by || !context || by === currentResumeTitle()) return "";
    return ` <span class="chip" title="这条分数是用《${e(by)}》打的，照样有效">${e(by)} 打分</span>`;
  }
  /** 把"版本号"换成人能读的名字：当前资料版本 → 简历标题。 */
  function prettyDetail(text) {
    let s = String(text || "");
    const ver = context?.profile?.version;
    const title = currentResumeTitle();
    if (ver && title) s = s.split(ver).join(`${title}（当前）`);
    return s;
  }
  function renderReview() {
    const d = review || {};
    const ok = d.ok === true;
    const cap = Number(d.daily_cap || 0), sent = Number(d.sent_today || 0);
    const ready = Number(d.ready || 0);
    const pending = Number(d.pending_total ?? d.eligible_not_ready ?? 0);
    $("control-review-stamp").textContent = d.at
      ? `数据 ${String(d.at).slice(9, 11)}:${String(d.at).slice(11, 13)}` : "";
    $("control-funnel").innerHTML = ok ? [
      tile("今日已发", `${sent}<small> / ${cap}</small>`, d.reserved ? `另占额度 ${d.reserved}` : "平台上限是预算，不是目标"),
      tile("够分岗位", String(d.eligible ?? 0), `≥ ${d.min_score} 分，筛选通过`),
      tile("待写稿 / 待处理", String(pending), pending ? "待生成、旧稿失效或校验未通过" : "没有欠稿", pending ? "warn" : ""),
      tile("可审核发送", String(ready), ready ? "已过程序校验，可查看正文" : "写完才进这里", ready ? "ok" : ""),
    ].join("") : "";
    if (!ok) {
      $("control-review-hint").innerHTML = `<div class="banner bad">${e(d.error || "读不到待发清单，请刷新重试")}</div>`;
      $("control-pending-count").textContent = "0";
      $("control-pending-list").innerHTML = "";
      $("control-ready-count").textContent = "0";
      $("control-drafts").innerHTML = "";
      $("control-coverage").innerHTML = "";
      $("control-write").disabled = true;
      updateSendLabel();
      return;
    }
    $("control-review-hint").innerHTML = "";
    $("control-coverage").innerHTML = coverageStrip(d);
    // ① 够分未写：和发送记录同一套卡片
    const pend = d.pending || [];
    const shown = pendingOpen ? pend : pend.slice(0, 5);
    $("control-pending-count").textContent = String(pending);
    // 欠稿按原因归类：先看"为什么没稿"，再决定是补写还是查资料
    const kindLabel = {
      new: "还没写", stale_profile: "换资料要重写", stale_score: "分数变了要重写",
      stale_jd: "JD 变了要重写", stale_prefs: "口径变了要重写",
      stale_selection: "选材规则变了", stale_policy: "写作规则变了",
      rejected: "上次没过校验", write_failed: "上次写稿失败",
      needs_review: "事实待核对",
      stale: "其他过期", no_text: "台账缺正文", unknown: "待复核", other: "其他",
    };
    const counts = { ...(d.pending_reason_counts || {}) };
    if (!d.pending_reason_counts) for (const p of pend) counts[p.reason_kind || "other"] = (counts[p.reason_kind || "other"] || 0) + 1;
    $("control-pending-reasons").innerHTML = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => `<span class="chip">${e(kindLabel[k] || k)} ${n}</span>`).join(" ");
    $("control-pending-list").innerHTML = pend.length
      ? shown.map(p => `<article class="card lift mb-16 match-card wf-queue-card" data-open="${e(p.job_id)}" role="button" tabindex="0" title="查看这条招呼语的输入与输出">
          <div class="match-row">
            ${scoreRing(p.score)}
            <div class="match-main">
              <h4 class="match-title">${e(p.title || "岗位未记录")} <span class="muted" style="font-weight:400;font-size:13px">@ ${e(p.company || "公司未记录")}</span>${oldScoreChip(p)}</h4>
              <div class="wf-queue-reason">${e(p.reason || "还没写招呼语")}</div>
              ${(p.reason_detail || p.reason_since) ? `<div class="wf-queue-detail">${e(prettyDetail(p.reason_detail))}${p.reason_since ? `　·　写于 ${e(String(p.reason_since).slice(5))}` : ""}</div>` : ""}
              ${p.review_draft ? `<p class="muted">以下草稿未进入待发送，请核对其中承诺的事实：</p><div class="wf-sent-text">${e(p.review_draft)}</div>` : ''}
              <div class="wf-hist-meta"><span class="muted wf-hist-open">查看写稿依据与正文 ›</span></div>
            </div>
            <button class="btn sm" data-write="${e(p.job_id)}" title="只写这一条招呼语">写这一条</button>
          </div>
        </article>`).join("")
        + (pend.length > 5
          ? `<button class="btn sm ghost mt-8" id="control-pending-more">${pendingOpen ? "收起" : `展开本次读到的 ${pend.length} 条`}</button>`
          : "")
        + (pending > pend.length ? `<p class="muted">还有 ${pending - pend.length} 条没列出（列表最多 40 条）。</p>` : "")
      : `<p class="muted">没有欠稿的岗位。</p>`;
    // ② 已写好待审：同一套卡片 + 勾选框
    const rows = d.rows || [];
    for (const id of [...chosen]) if (!rows.some(r => r.job_id === id)) chosen.delete(id);
    $("control-ready-count").textContent = String(ready);
    $("control-drafts").innerHTML = rows.length ? rows.map((r, i) => `<article class="card lift mb-16 match-card wf-draft-card" data-open="${e(r.job_id)}" role="button" tabindex="0" title="查看这条招呼语的输入与输出">
        <div class="match-row">
          ${scoreRing(r.score)}
          <div class="match-main">
            <h4 class="match-title">${e(r.title || "岗位未记录")} <span class="muted" style="font-weight:400;font-size:13px">@ ${e(r.company || "公司未记录")}</span>${oldScoreChip(r)}</h4>
            <div class="wf-sent-text">${e(r.text || "正文缺失，请刷新后重试")}</div>
            <div class="wf-hist-meta"><span class="muted">${e(String(r.chars || 0))} 字 · 已过程序校验</span><span class="muted wf-hist-open">查看写稿依据与正文 ›</span></div>
          </div>
          <label class="wf-pick" title="勾选这条发送"><input type="checkbox" data-draft="${i}" data-draft-box="${e(r.job_id)}"${chosen.has(r.job_id) ? " checked" : ""}><span>选这条</span></label>
        </div>
      </article>`).join("")
      : `<div class="banner info">${pending
          ? `还没有写好的招呼语。${pending} 个够分岗位仍待处理，请切换到“待写稿与问题”查看原因，再让 AI 补齐写稿。`
          : "还没有写好的招呼语。先在“匹配评分”核对岗位分数，再与你的 AI 确认写稿规则。"}</div>`;
    $("wf-select-all").checked = rows.length > 0 && rows.every(r => chosen.has(r.job_id));
    $("control-write").disabled = pending === 0;
    updateSendLabel();
  }
  /** 从「库里有多少」到「现在能发几条」的整条漏斗。 */
  function coverageStrip(d) {
    const num = v => Number(v || 0).toLocaleString("zh-CN");
    const cov = d.coverage || {};
    const current = Number(cov.scored_current ?? 0);
    const other = Number(cov.scored_other_only || 0);
    const any = Number(cov.scored_any || current + other);
    const todo = Number(d.unscored_todo || 0);
    const steps = [
      ["库里岗位", d.db_total],
      ["有 JD", d.with_jd],
      ["已打分", any],
      ["够分", d.eligible],
      ["有稿可发", d.ready],
    ];
    return `<div class="wf-cov-line">${steps.map(([k, v]) => `<span class="wf-cov-step">${e(k)} <b>${num(v)}</b></span>`).join('<span class="wf-cov-arrow">→</span>')}</div>${scoringQueueHtml(d)}
      ${todo ? `<div class="wf-cov-actions"><button class="btn sm" id="control-score-more">补评待评分岗位（最多80个）</button></div>` : ""}`;
  }
  async function loadReview(force = false) {
    if (disposed) return;
    if (!review) $("control-drafts").innerHTML = '<p class="muted">读取中…</p>';
    try {
      const [data, delivery] = await Promise.all([
        api.get("/api/workflow/sendable" + (force ? "?force=true" : "")),
        api.get('/api/deliver/status')]);
      if (disposed) return;
      $("control-delivery-state").innerHTML = delivery.read_control?.paused
        ? `<p class="banner warn">后台发送已暂停：${e(delivery.read_control.reason)}。请先处理 BOSS 提示，再让 AI 恢复；反复提交任务不会继续发送。</p>`
        : delivery.read_control?.verification_paused
          ? '<p class="muted">可选的岗位详情刷新暂缓；正常发送复核已采集资料，存在信息冲突的岗位仍不发送。</p>' : '';
      review = data;
      lastReviewAt = Date.now();
      // 数据没变就不重画：10 秒一次轮询不该打断正在看的人
      const sig = JSON.stringify([data.ok, data.analyzed, data.with_jd, data.missing_desc, data.unscored_todo, data.unscored_blocked, data.coverage, data.ready, data.pending_total, data.eligible,
        data.sent_today, data.pending_reason_counts, data.pending,
        (data.rows || []).map(r => `${r.job_id}:${r.chars}:${r.review_hash || r.text || ''}`)]);
      if (sig !== lastReviewSig) {
        lastReviewSig = sig;
        renderReview();
      } else {
        $("control-review-stamp").textContent = data.at
          ? `数据 ${String(data.at).slice(9, 11)}:${String(data.at).slice(11, 13)}` : "";
      }
    } catch (err) {
      if (disposed) return;
      if (!review) $("control-drafts").innerHTML = "";
      error(err);
    }
  }
  /** 资料/设置/口径改过：旧指纹作废，必须刷新后重新勾选（发送端也会拒）。 */
  function clearDrafts() {
    chosen.clear();
    updateSendLabel();
    if (review) {
      $("control-review-hint").innerHTML = '<div class="banner warn">资料或设置已更新：点「刷新」重新核对稿件，旧勾选已清空。</div>';
    }
  }
  bind("control-load-drafts", async () => {
    await loadReview(true);
    await loadHistory();
  });
  $("control-drafts").addEventListener("change", ev => {
    const box = ev.target.closest("[data-draft-box]");
    if (!box) return;
    if (box.checked) chosen.add(box.dataset.draftBox); else chosen.delete(box.dataset.draftBox);
    $("wf-select-all").checked = (review?.rows || []).length > 0 && (review.rows).every(r => chosen.has(r.job_id));
    updateSendLabel();
  });
  $("wf-select-all").addEventListener("change", ev => {
    for (const r of review?.rows || []) {
      if (ev.target.checked) chosen.add(r.job_id); else chosen.delete(r.job_id);
    }
    host.querySelectorAll("[data-draft-box]").forEach(box => { box.checked = ev.target.checked; });
    updateSendLabel();
  });
  $("control-pending-list").addEventListener("click", ev => {
    if (ev.target.closest("#control-pending-more")) {
      pendingOpen = !pendingOpen;
      renderReview();
      return;
    }
    const btn = ev.target.closest("[data-write]");
    if (btn) {
      ev.stopPropagation();
      action(btn, async () => {
        const jobId = btn.dataset.write;
        if (!window.confirm("只为这一条岗位写招呼语？只生成草稿，不会发送。")) return;
        await api.post("/api/workflow/control/tasks", { action: "prepare", only: [jobId], limit: 1, confirmed: true });
        await loadTasks();
        toast("已加入写稿队列（只写这一条）", "ok");
      });
      return;
    }
    const card = ev.target.closest("[data-open]");
    if (card) openQueueDetail(card.dataset.open);
  });
  $("control-drafts").addEventListener("click", ev => {
    if (ev.target.closest(".wf-pick")) return;   // 勾选框自己处理
    const card = ev.target.closest("[data-open]");
    if (card) openQueueDetail(card.dataset.open);
  });
  // 键盘也能打开（和发送记录卡片一致）
  for (const id of ["control-pending-list", "control-drafts"]) {
    $(id).addEventListener("keydown", ev => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const card = ev.target.closest("[data-open]");
      if (card) { ev.preventDefault(); openQueueDetail(card.dataset.open); }
    });
  }
  $("control-coverage").addEventListener("click", ev => {
    const btn = ev.target.closest("#control-score-more");
    if (!btn) return;
    if (!window.confirm("补一批评分（最多 80 个岗位）？已有分数不动。")) return;
    action(btn, async () => {
      await api.post("/api/workflow/control/tasks", { action: "match", limit: 80, confirmed: true });
      await loadTasks();
      toast("补打分任务已加入队列，跑完这里会自动刷新", "ok");
    });
  });
  bind("control-write", async () => {
    const limit = Math.max(1, Math.min(30, Number($("control-write-limit").value) || 6));
    const pending = Number(review?.pending_total ?? review?.eligible_not_ready ?? 0);
    if (!window.confirm(`为最多 ${limit} 个岗位写招呼语？只生成草稿，不会发送。${pending ? `（当前欠稿 ${pending} 条）` : ""}`)) return;
    await api.post("/api/workflow/control/tasks", { action: "prepare", limit, confirmed: true });
    await loadTasks();
    toast("写稿任务已加入队列，完成后待发清单会自动刷新", "ok");
  });
  bind("control-send", async () => {
    const rows = (review?.rows || []).filter(r => chosen.has(r.job_id));
    if (!rows.length) throw new Error("请先勾选要发送的招呼语");
    const approved = {};
    for (const r of rows) if (r.review_hash) approved[r.job_id] = r.review_hash;
    const count = Object.keys(approved).length;
    if (count !== rows.length) throw new Error("有勾选的稿件缺少审核指纹，请点「刷新」重新核对");
    const scheduled = due();
    if (!window.confirm(`确认${scheduled ? "于 " + when(scheduled) : "现在"}发送这 ${count} 条招呼语？发送后不能撤回。`)) return;
    await api.post("/api/workflow/control/tasks", { action: "send", approved, due: scheduled, confirmed: true });
    chosen.clear();
    updateSendLabel();
    await loadTasks();
    toast("后台发送已提交，可关闭页面或结束 AI 对话；保持程序服务运行，稍后查看发送记录", "ok");
  });
  /** 今天的安排：谁在跑、几点该跑、今天跑没跑、谁在触发。分块给人数，不写说明文。 */
  const ROUND_STATE = {
    done: ['ok', '已跑'], missed: ['warn', '没跑'], upcoming: ['static', '待跑'],
    weekend: ['static', '周末不排'], manual: ['static', '按需'],
  };
  function runBrief(b) {
    if (!b) return '<span class="muted">—</span>';
    const time = e(String(b.time || '').slice(5, 16));
    const tail = b.mode === 'send' ? `发出 ${e(String(b.ok ?? 0))} 条` : `备 ${e(String(b.prepared ?? 0))} 条`;
    const bad = Number(b.failed || 0) > 0 ? ` · 失败 ${e(String(b.failed))}` : '';
    return `${time}<br><span class="muted">${tail}${bad}</span>`;
  }
  async function loadSchedule() {
    const [d, ai] = await Promise.all([
      api.get('/api/workflow/control/schedule'),
      api.get('/api/ai/context').catch(() => null),
    ]);
    if (disposed) return;
    const c = d.collector || {};
    const os = (d.triggers || {}).os || [];
    const nowLines = [];
    if (ai && ai.ok) {
      const plan = (ai.active || {}).plan || {};
      const cand = (ai.active || {}).candidate || {};
      const missing = ai.missing || [];
      const warn = missing.length
        ? `<div class="wf-sched-line warn"><b>方案缺项</b> ${missing.map(e).join('、')}——这些阶段不会跑</div>`
        : '';
      nowLines.push(`<div class="wf-sched-line"><b>有效方案</b> ${e(plan.label || '未命名')} <span class="muted">rev ${e(String(plan.revision ?? '—'))} · ${e(plan.id || '—')}</span> · 资料 <span class="muted">${e(cand.id || '—')}</span></div>${warn}`);
    } else if (ai && ai.ok === false) {
      nowLines.push(`<div class="wf-sched-line warn"><b>有效方案</b> ${e(ai.message || '读取失败')}</div>`);
    }
    nowLines.push(c.running
      ? `<div class="wf-sched-line"><b>采集器</b> ${c.capped ? '<span class="pill warn">已到今日上限</span>' : '运行中'} · 第 ${e(String(c.cycle ?? '—'))} 轮 · ${c.capped ? '今天不再翻页，仍在补 JD / 探活跃' : `正在抓「${e(c.query || '—')}」`} · 今日 ${e(String(c.pages ?? 0))}/${e(String(c.page_cap ?? '—'))} 页 · 下一轮约 ${e(c.next_at || '—')}</div>`
      : `<div class="wf-sched-line"><b>采集器</b> <span class="pill warn">已停止</span>${c.at ? ` · 最后一次心跳 ${e(String(c.at).slice(5, 16))}` : ''}</div>`);
    const m = d.match || {};
    if (['running', 'queued', 'starting'].includes(m.status)) {
      nowLines.push(`<div class="wf-sched-line"><b>匹配</b> ${e(String(m.done ?? 0))} / ${e(String(m.total ?? 0))} · ${e(m.current_job || '')}</div>`);
    }
    const running = (d.queued || []).length;
    const lt = d.last_task || null;
    const ltText = lt
      ? `最近一次 ${e(when(lt.due || lt.created))} ${e(labels[lt.action] || lt.action)}（${e(statuses[lt.status] || lt.status)}）`
      : '还没排过任务';
    nowLines.push(`<div class="wf-sched-line"><b>面板任务</b> ${running ? `排队 ${e(String(running))} 个 · ` : ''}${ltText}</div>`);

    const rows = (d.rounds || []).map(r => {
      const [tone, label] = ROUND_STATE[r.state] || ['static', r.state];
      const todayText = r.state === 'weekend' ? '今天是周末，不排'
        : r.state === 'manual' ? '按需' : `<span class="pill ${tone}">${label}</span>`;
      return `<tr><td>${e(r.name === 'manual' ? '手动' : r.name)}</td><td>${e(r.at || '不限时')}</td><td>${todayText}</td><td>${runBrief(r.last)}</td></tr>`;
    }).join('');
    const lastAny = d.last_any
      ? `上一次任意一轮：${e(String(d.last_any.time || '').slice(5, 16))} · 发出 ${e(String(d.last_any.ok ?? 0))} 条`
      : '还没有跑过任何一轮';
    const verdict = os.length
      ? `<div class="wf-sched-line"><b>触发</b> 系统计划任务 ${e(String(os.length))} 条：${os.map(t => e(t.name)).join('、')}</div>`
      : `<div class="wf-sched-line warn"><b>触发</b> 系统计划任务里没有本项目的条目（${e((d.triggers || {}).checked_at || '')} 查过，0 条）——表里这些时间点不会自己跑</div>`;
    repaint($('control-schedule'), `
      ${nowLines.join('')}
      <div class="wf-sched-block">
        <div class="wf-sched-title">轮次</div>
        <div class="data-table-wrap"><table class="data-table"><thead><tr><th>轮次</th><th>计划</th><th>今天</th><th>上次实际</th></tr></thead><tbody>${rows}</tbody></table></div>
        ${verdict}
        <div class="wf-sched-line muted">${lastAny}</div>
      </div>`);
    $('control-schedule-stamp').textContent = `${d.today} ${['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'][d.weekday] || ''} · ${String(d.now || '').slice(11, 16)}`;
    const statusEl = $('rc-status');
    if (statusEl) {
      statusEl.textContent = c.running
        ? `运行中 · 第 ${c.cycle ?? '—'} 轮 · 今日 ${c.pages ?? 0}/${c.page_cap ?? '—'} 页 · 下一轮约 ${c.next_at || '—'}`
        : (c.at ? `已停止 · 最后心跳 ${String(c.at).slice(5, 16)}` : '未运行');
    }
  }
  /** 采集/打分/发送按什么条件跑，以及哪些指标可调（值都能直接改）。 */
  let recipe = null;
  async function loadRecipe() {
    recipe = await api.get('/api/workflow/control/recipe');
    if (disposed) return;
    const run = recipe.run || {};
    $('rr-score').value = run.min_score ?? '';
    $('rr-cap').value = run.daily_cap ?? '';
    $('rr-chunk').value = run.chunk ?? '';
    $('rr-gap').value = run.chunk_gap ?? '';
    $('rr-batch').value = run.batch_size ?? '';
    const gate = run.salary_gate || {}, pricing = run.pricing || {};
    $('rr-rules').innerHTML = [
      ['日薪门槛', `${gate.daily_min ?? '—'}–${gate.daily_max ?? '—'} 元/天`],
      ['高峰不跑匹配', (pricing.peak_windows || []).join(' / ') || '—'],
      ['招呼语上限', '200 字 + 事实校验'],
      ['发送重试', `${run.max_delivery_attempts ?? '—'} 次`],
      ['旧简历分数', '照样有效'],
    ].map(([k, v]) => `<span class="wf-rule"><b>${e(k)}</b> ${e(String(v))}</span>`).join('');
  }
  bind('rr-save', async () => {
    if (!recipe) throw new Error('参数还没读出来，稍后再保存');
    await api.put('/api/workflow/control/settings', {
      expected_version: recipe.run.version,
      min_score: Number($('rr-score').value),
      daily_cap: Number($('rr-cap').value),
      chunk: Number($('rr-chunk').value),
      chunk_gap: Number($('rr-gap').value),
      batch_size: Number($('rr-batch').value),
    });
    await loadRecipe();
    clearDrafts();
    toast('执行参数已保存', 'ok');
  });
  async function loadTasks() {
    const { items } = await api.get("/api/workflow/control/tasks");
    if (disposed) return;
    repaint($("control-tasks"), items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>任务</th><th>计划时间</th><th>状态</th><th>结果与操作</th></tr></thead><tbody>${items.map(t => `<tr><td>${e(labels[t.action] || t.action)}</td><td>${e(when(t.due))}</td><td>${e(t.cancel_requested && t.status === "running" ? "等待当前操作结束后停止" : statuses[t.status] || t.status)}</td><td>${["queued", "running"].includes(t.status) ? `<button class="btn sm" data-cancel="${e(t.id)}">${t.status === "queued" ? "取消计划" : "停止后续操作"}</button>` : ""}<details data-k="task:${e(t.id)}"><summary>查看执行结果</summary><pre class="wf-result">${e(t.result || "尚无结果。运行中任务若遇到程序异常退出，需核对台账；系统不会自动重试发送。")}</pre></details></td></tr>`).join("")}</tbody></table></div>` : `<p class="muted">还没有面板任务。</p>`);
    host.querySelectorAll("[data-cancel]").forEach(button => button.addEventListener("click", () => action(button, async () => {
      await api.post("/api/workflow/control/tasks/" + button.dataset.cancel + "/cancel"); await loadTasks();
    })));
    for (const task of items.filter(t => t.status === "needs_review")) {
      const button = document.createElement("button");
      button.className = "btn";
      button.textContent = `已核对中断任务 ${labels[task.action]}，解除暂停`;
      button.addEventListener("click", () => action(button, async () => {
        if (!window.confirm("请先核对运行日志、发送台账和平台实际结果。确认后只解除队列暂停，不会重试这个任务。确定已核对？")) return;
        await api.post("/api/workflow/control/tasks/" + task.id + "/acknowledge", { confirmed: true }); await loadTasks();
      }));
      $("control-tasks").append(button);
    }
  }
  async function loadHistory() {
    const data = await api.get('/api/workflow/control/deliveries');
    if (disposed) return;
    historyIndex.clear();
    for (const row of data.items || []) historyIndex.set(`${row.job_id || ''}:${row.attempt_id ?? ''}`, row);
    repaint($('control-history'), renderHistory(data) + renderGenerated(data.drafts || []));
  }
  /** 状态 → pill 文案与配色。 */
  const historyPill = {
    sending: ['发送中', 'warn'], sent: ['已发送', 'accent'], failed: ['发送失败', 'bad'],
    unknown: ['结果未知', 'warn'], skipped: ['已跳过', 'static'],
  };
  const dayLabel = (day) => {
    const now = new Date();
    const stamp = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const today = stamp(now);
    const yesterday = stamp(new Date(now.getTime() - 86400000));
    if (day === today) return '今天';
    if (day === yesterday) return '昨天';
    return day.slice(5).replace('-', ' 月 ') + ' 日';
  };
  const chips = (values) => (Array.isArray(values) ? values : []).filter(v => String(v).trim())
    .map(v => `<span class="chip">${e(String(v))}</span>`).join(' ');
  const gaps = (values) => (Array.isArray(values) ? values : []).filter(v => String(v).trim())
    .map(v => `<span class="pill static bad">${e(String(v))}</span>`).join(' ');
  const lines = (values) => (Array.isArray(values) ? values : []).filter(v => String(v).trim())
    .map(v => `<div class="muted" style="font-size:12.5px">${e(String(v))}</div>`).join('');

  /** 单条发送记录：卡片只放摘要，点开看全部（和匹配结果一样）。 */
  function historyCard(r) {
    const a = r.analysis || null;
    const key = `${r.job_id || ''}:${r.attempt_id ?? ''}`;
    const score = a ? Number(a.score ?? a.match_score ?? 0) : null;
    const [name, tone] = historyPill[r.status] || [r.status || '未记录', 'static'];
    const advice = a ? String(a.verdict || '').trim() : '';
    const body = r.text || '历史记录缺少当时正文，无法还原；请到平台核对。';
    const skills = a ? (a.matched_skills || []).filter(s => String(s).trim()).slice(0, 4) : [];
    return `
      <article class="card lift mb-16 match-card wf-history-card" data-hk="${e(key)}" role="button" tabindex="0" title="点击查看全部信息">
        <div class="match-row">
          ${score === null ? `<div class="score-ring low" style="--p:0">—</div>` : scoreRing(score)}
          <div class="match-main">
            <h4 class="match-title">${e(r.title || '岗位未记录')} <span class="muted" style="font-weight:400;font-size:13px">@ ${e(r.company || '公司未记录')}</span></h4>
            <div class="muted" style="font-size:12px">${e(String(r.at || '').slice(5))} · ${e(r.channel || '')}${r.model ? ` · ${e(r.model)}` : ''}</div>
            <div class="pills mt-8"><span class="pill static ${tone}">${e(name)}</span></div>
            ${advice ? `<div class="muted" style="font-size:12.5px;margin-top:6px">${e(advice)}</div>` : ''}
            <div class="wf-sent-text">${e(body)}</div>
            <div class="wf-hist-meta">
              <span class="muted">${e(String(body.length))} 字</span>
              ${skills.length ? `<span class="chips">${skills.map(s => `<span class="chip">${e(String(s))}</span>`).join(' ')}</span>` : ''}
              <span class="muted wf-hist-open">查看全部 ›</span>
            </div>
          </div>
        </div>
      </article>`;
  }

  /** 单条发送记录的全部信息（弹窗）。 */
  function openHistoryDetail(key) {
    const r = historyIndex.get(key);
    if (!r) return;
    const a = r.analysis || null;
    const score = a ? Number(a.score ?? a.match_score ?? 0) : null;
    const [name, tone] = historyPill[r.status] || [r.status || '未记录', 'static'];
    const verdict = a ? String(a.verdict || '').trim() : '';
    const matched = a ? chips(a.matched_skills) : '';
    const missing = a ? gaps(a.missing_skills) : '';
    const pros = a ? lines(a.pros) : '';
    const cons = a ? lines(a.cons) : '';
    const hard = a ? lines(a.hard_constraints) : '';
    const body = r.text || '历史记录缺少当时正文，无法还原；请到平台核对。';
    const dlg = modal({
      wide: true,
      title: `${r.title || '岗位未记录'} @ ${r.company || '公司未记录'}`,
      body: `
        <div class="flex between gap-12 mb-16 wrap" style="align-items:flex-start">
          <div class="flex center gap-12">
            ${score === null ? `<div class="score-ring low" style="--p:0">—</div>` : scoreRing(score)}
            <div>
              <div class="pills"><span class="pill static ${tone}">${e(name)}</span></div>
              <div class="muted mt-8" style="font-size:12.5px">${e(String(r.at || ''))} · ${e(r.channel || '')}</div>
              <div class="muted" style="font-size:12px">写稿模型：${e(r.model || '历史未记录')}</div>
            </div>
          </div>
          <div class="muted" style="font-size:12px;text-align:right">岗位 ${e(r.job_id || '—')}<br>任务 ${e(r.task_id || '未记录')}</div>
        </div>

        <div class="card-sub mb-8">匹配分析</div>
        ${a ? `
          ${verdict ? `<div class="muted mb-8" style="font-size:12.5px"><span class="muted" style="font-size:11px">结论</span> ${e(verdict)}</div>` : ''}
          ${matched ? `<div class="card-sub mb-8 mt-12">匹配技能</div><div class="job-meta">${matched}</div>` : ''}
          ${missing ? `<div class="card-sub mb-8 mt-12">欠缺</div><div class="pills">${missing}</div>` : ''}
          ${pros ? `<div class="card-sub mb-8 mt-12">优势</div>${pros}` : ''}
          ${cons ? `<div class="card-sub mb-8 mt-12">不足</div>${cons}` : ''}
          ${hard ? `<div class="card-sub mb-8 mt-12">硬条件</div>${hard}` : ''}
          <div class="muted mt-8" style="font-size:11.5px">${e(r.analysis_source || '')} · 分析编号 ${e(r.analysis_id || (a && a.analysis_id) || '未记录')}</div>
        ` : `<div class="muted mb-8" style="font-size:12.5px">当时的分析没有记录（或已删除），不能用现在的分析替代。</div>`}

        <div class="card-sub mb-8 mt-16">职位信息 · JD 全文</div>
        <div class="wf-jd"><span class="muted">读取 JD…</span></div>

        <div class="card-sub mb-8 mt-16">实际提交发送的正文</div>
        <p class="wf-detail-text">${e(body)}</p>

        <button type="button" class="btn sm" data-history-inputs>对照本条写稿输入与输出</button>
        <div class="card-sub mb-8 mt-16">本次发送前的生成稿</div>
        <p class="wf-detail-text muted">${e(r.generated_text || '历史未记录')}</p>

        <div class="card-sub mb-8 mt-16">任务与结果</div>
        <p class="muted" style="font-size:12.5px">批次 ${e(r.batch || '未记录')} · 第 ${e(String(r.attempt_id ?? '—'))} 次尝试 · 资料版本 ${e(r.profile_version || '未记录')}</p>
        <p class="muted" style="font-size:12.5px">${e(r.reason || '无附加说明')}</p>
      `,
    });
    dlg.box.querySelector('[data-history-inputs]').onclick=()=>openGreetingReview({...r,text:r.generated_text || r.text || ''});
    const host = dlg.box.querySelector('.wf-jd');
    if (!host) return;
    if (!r.job_id) { host.innerHTML = '<span class="muted">没有岗位编号，取不到 JD。</span>'; return; }
    api.get('/api/jobs/' + encodeURIComponent(r.job_id)).then((j) => {
      const meta = [j.brand_name, j.location, j.salary_desc, j.job_experience, j.job_degree, j.brand_scale_name]
        .filter(Boolean).map((v) => e(String(v))).join(' · ');
      const desc = String(j.job_desc || '');
      host.innerHTML = `${meta ? `<div class="muted" style="font-size:12px">${meta}</div>` : ''}
        ${desc
          ? `<details class="acc mt-8"><summary>展开 JD 全文（${e(String(desc.length))} 字）</summary><div class="acc-body" style="white-space:pre-wrap">${e(desc)}</div></details>`
          : '<p class="muted">这份岗位还没抓到 JD 全文（库里 job_desc 为空），可以去「职位」页补抓。</p>'}`;
    }).catch((err) => {
      host.innerHTML = `<span class="muted">JD 读取失败：${e(err.message || err)}</span>`;
    });
  }

  /** 待写 / 待发卡片点开：匹配分析 + JD 全文 + 正文 + 为什么卡在这一步（和发送记录同一套信息量）。 */
  function openQueueDetail(jobId) {
    const row = (review?.rows || []).find(r => r.job_id === jobId)
      || (review?.pending || []).find(p => p.job_id === jobId);
    if (!row) return;
    openGreetingReview(row);
  }

  /**
   * 重画一个容器，但保住用户已经展开/收起的折叠块。
   * 5 秒轮询重画时，正在看的内容不能自己收回去。
   */
  function repaint(node, html) {
    const keep = new Map();
    node.querySelectorAll('details[data-k]').forEach(d => keep.set(d.dataset.k, d.open));
    const scrolled = node.scrollTop;
    node.innerHTML = html;
    node.querySelectorAll('details[data-k]').forEach(d => {
      if (keep.has(d.dataset.k)) d.open = keep.get(d.dataset.k);
    });
    node.scrollTop = scrolled;
  }

  /** 按天分组：今天 / 昨天 / 具体日期；最新一天默认展开，其余收起。 */
  function renderHistory(data) {
    const head = data.errors ? `<div class="banner bad">台账有 ${e(String(data.errors))} 行无法读取，以下记录可能不完整。</div>` : '';
    const items = data.items || [];
    if (!items.length) return head + '<div class="empty"><div class="empty-icon">◌</div><p>还没有发送记录</p></div>';
    const groups = [];
    for (const row of items) {
      const key = String(row.at || '').slice(0, 10);
      const last = groups[groups.length - 1];
      if (!last || last.day !== key) groups.push({ day: key, rows: [row] });
      else last.rows.push(row);
    }
    return head + groups.map((g, i) => `<details class="wf-day-group" data-k="day:${e(g.day)}"${i === 0 ? ' open' : ''}>
        <summary class="wf-day">${e(dayLabel(g.day))}<span class="muted">${g.rows.length} 条</span></summary>
        ${g.rows.map(historyCard).join('')}
      </details>`).join('');
  }

  /** 生成历史：含未发送稿件，默认收起。 */
  function renderGenerated(drafts) {
    if (!drafts.length) return '';
    const rows = drafts.map(r => `<div class="wf-gen-row">
        <span class="muted">${e(String(r.at || '').slice(5, 16))}</span>
        <b>${e(r.title || '岗位未记录')}</b>
        <span class="muted">@ ${e(r.company || '公司未记录')}</span>
        <span class="muted">${e(String((r.text || '').length))} 字</span>
      </div>`).join('');
    return `<details class="mt-8" data-k="generated"><summary>生成历史（含未发送，最近 ${drafts.length} 条）</summary>${rows}</details>`;
  }
  async function loadMatch() {
    const state = await api.get('/api/match/analyze/status');
    if (disposed) return;
    matchState = state || {};
    const progress = state.progress || {};
    $('control-live-match').textContent = ['running','queued','starting'].includes(state.status)
      ? `正在匹配：${state.done ?? progress.done ?? 0} / ${state.total ?? progress.total ?? 0} · ${state.current_job || ''}` : '';
  }
  bind('control-refresh-history', loadHistory);
  // 点卡片看全部信息（用事件委托，重画后不用重新绑）
  $('control-history').addEventListener('click', (ev) => {
    const card = ev.target.closest('[data-hk]');
    if (card) openHistoryDetail(card.dataset.hk);
  });
  $('control-history').addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    const card = ev.target.closest('[data-hk]');
    if (card) { ev.preventDefault(); openHistoryDetail(card.dataset.hk); }
  });
  bind("control-refresh-tasks", loadTasks);
  // 首次进入必须先画一次清单和历史；之后只有「正在发送 / 刚发完」才重画。
  let sendWasBusy = true, reviewBusy = false;
  try {
    await loadContext();
    if (mode === 'review') await loadReview();
    await refreshPage();
  } catch (err) { error(err); }
  async function refreshPage() {
    if (mode === 'review') {
      let busy = false;
      try {
        const { items } = await api.get('/api/workflow/control/tasks');
        busy = (items || []).some(t => ['queued', 'running'].includes(t.status));
      } catch { /* 读不到任务状态也照样把清单画出来 */ }
      $("control-review-busy").textContent = busy ? "任务执行中…" : "";
      // 从「执行中」回到空闲 = 写稿或发送跑完了，重新核对一次待发清单
      if (reviewBusy && !busy) await loadReview(true);
      reviewBusy = busy;
      // 没人开弹窗时每 10 秒自己更新一次；数据没变不会重画，不靠用户点「刷新」
      const modalOpen = (document.querySelector('#modal-root')?.childElementCount || 0) > 0;
      if (!busy && !modalOpen && Date.now() - lastReviewAt > 10000) await loadReview();
      if (busy || sendWasBusy) await loadHistory();
      sendWasBusy = busy;
    }
    if (mode === 'tasks') await Promise.all([loadMatch(), loadSchedule(), loadRecipe(), loadTasks()]);
  }
  const timer = ['review','tasks'].includes(mode) ? setInterval(() => refreshPage().catch(error), 5000) : null;
  // In-page anchors must not replace the application's hash router.
  host.querySelectorAll('a[href^="#wf-"]').forEach(link => link.addEventListener("click", event => {
    event.preventDefault();
    const target = host.querySelector(link.getAttribute('href'));
    const panel = target?.closest('[data-section-panel]');
    if (panel && showReviewSection) showReviewSection(panel.dataset.sectionPanel);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  return () => { disposed = true; clearInterval(timer); };
}
