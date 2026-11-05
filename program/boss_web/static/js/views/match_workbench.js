/** 匹配工作台：简历选择 + 范围条 + 匹配按钮 + 双栏（岗位列表｜匹配结果）+ 详情弹窗 */

import { api } from "../api.js";
import { toast, modal, escapeHtml, scoreRing, taskPanel, renderEvents } from "../ui.js";

const PAGE_SIZE = 20;

/**
 * 渲染匹配工作台：简历选择、范围条、匹配按钮、岗位列表与匹配结果双栏。
 *
 * @param {HTMLElement} pane   内容容器
 * @param {Object}     state   页面级状态
 * @param {Object}     helpers 共享助手
 */
export function renderWorkbench(pane, state, helpers) {
  const {
    resumes, currentResumeId, jobs, totalJobs, page, selected,
    matches, analysisId, scope,
  } = state;
  const {
    setMatchButtons, paintMatch, renderMatches, renderJobs, updateSelCount,
    startMatch, openMatchDetail, deliverLabel, loadResumes, updateLlmBadge,
    matchPanelHost,
  } = helpers;

  pane.innerHTML = `
    <div class="match-toolbar">
      <div class="match-resume-field">
        <label class="muted" for="resume-select">当前评分简历</label>
        <select class="select" id="resume-select"></select>
      </div>
      <div id="scope-bar" class="flex center gap-8 wrap"></div>
    </div>
    <div id="match-task" hidden></div>
    <div class="match-work-grid">
      <details class="card match-source" id="match-source">
        <summary><span class="card-title">手动评分与岗位范围</span><span class="muted">可选，通常由 AI 发起</span></summary>
        <div class="match-source-body">
        <div class="btn-row match-actions"><button class="btn" id="btn-match-all">匹配当前范围</button><button class="btn" id="btn-match-sel">匹配选中</button></div>
        <div class="card-sub mb-8" id="jobs-sub">—</div>
        <div class="flex between center mb-12">
          <div class="btn-row">
            <button class="btn sm ghost" id="btn-select-page">全选本页</button>
            <button class="btn sm ghost" id="btn-clear-sel">清空</button>
          </div>
          <span class="pill accent" id="sel-count">已选 0</span>
        </div>
        <div id="job-list" class="scroll-y" role="region" aria-label="可选岗位列表" tabindex="0"></div>
        <div class="flex between center mt-12">
          <button class="btn sm ghost" id="btn-prev">上一页</button>
          <span class="muted mono" id="page-info" style="font-size:12px">—</span>
          <button class="btn sm ghost" id="btn-next">下一页</button>
        </div>
        </div>
      </details>
      <div class="card match-results" id="results-card">
        <div class="card-head">
          <h3 class="card-title">岗位评分与理由 <small class="muted">点击查看 JD 与完整分析</small></h3>
          <div class="flex center gap-8">
            <span class="card-sub" id="an-sub">尚未匹配</span>
            <button class="btn sm ghost" id="btn-clear-analysis" ${state.analysisId || state.matches.length ? "" : "hidden"}>清空</button>
          </div>
        </div>
        <div id="an-results" class="scroll-y" role="region" aria-label="岗位匹配结果" tabindex="0">
          <div class="empty"><div class="empty-icon">◌</div><p>启动匹配后，结果会按分数从高到低排在这里</p></div>
        </div>
      </div>
    </div>
  `;

  const $ = (id) => pane.querySelector("#" + id);

  $("match-source").open = false;
  if (matchPanelHost) {
    // Keep status and errors visible; the event stream is optional reading.
    if (!matchPanelHost.querySelector('.match-run-log')) {
      const log = matchPanelHost.querySelector('.task-log');
      if (log) {
        const details = document.createElement('details');
        details.className = 'match-run-log';
        const summary = document.createElement('summary');
        summary.textContent = '运行日志';
        details.append(summary, log);
        matchPanelHost.appendChild(details);
      }
    }
    $("match-task").appendChild(matchPanelHost);
    const status = matchPanelHost.querySelector('.task-status')?.textContent;
    $('match-task').hidden = status === '待命';
  }

  const sel = $("resume-select");
  sel.innerHTML =
    `<option value="">— 选择简历 —</option>` +
    resumes.map((it) => {
      const parsed = it.llm && it.llm.has_data ? " · 已解析" : " · 未解析";
      return `<option value="${escapeHtml(it.resume_id)}">${escapeHtml(it.title || it.resume_id)}${parsed}</option>`;
    }).join("");
  sel.value = currentResumeId || "";
  sel.addEventListener("change", () => {
    state.currentResumeId = sel.value;
  });

  renderScopeBar();

  renderJobs();

  renderMatches();

  $("btn-match-all").addEventListener("click", () => startMatch([]));
  $("btn-match-sel").addEventListener("click", () => {
    if (!selected.size) {
      $("match-source").open = true;
      return toast("先在岗位范围中勾选要匹配的岗位", "warn");
    }
    startMatch([...selected]);
  });
  $("btn-clear-analysis")?.addEventListener("click", () => helpers.clearAnalysis());
  $("btn-select-page").addEventListener("click", () => {
    jobs.forEach((j) => selected.add(j.encrypt_job_id));
    renderJobs();
  });
  $("btn-clear-sel").addEventListener("click", () => {
    selected.clear();
    renderJobs();
  });
  $("btn-prev").addEventListener("click", () => {
    if (state.page > 0) {
      state.page -= 1;
      helpers.loadJobs();
    }
  });
  $("btn-next").addEventListener("click", () => {
    if ((state.page + 1) * PAGE_SIZE < state.totalJobs) {
      state.page += 1;
      helpers.loadJobs();
    }
  });
  const prevBtn = $("btn-prev");
  const nextBtn = $("btn-next");
  if (prevBtn) prevBtn.disabled = state.page <= 0;
  if (nextBtn) nextBtn.disabled = (state.page + 1) * PAGE_SIZE >= state.totalJobs;

  /** 绘制范围条（全部 / 抓取任务 / 已选），支持清除范围。 */
  function renderScopeBar() {
    const bar = $("scope-bar");
    if (!bar) return;
    const sc = state.scope;
    if (!sc || sc.type === "all") {
      bar.innerHTML = `<span class="pill static">全部职位</span>`;
      return;
    }
    const label = sc.type === "crawl_task"
      ? `抓取任务 #${escapeHtml(String(sc.crawl_task_id || "").slice(-6))} · ${sc.job_count ?? "?"} 个职位`
      : `已选 ${sc.job_ids?.length || 0} 个职位`;
    bar.innerHTML = `
      <span class="pill accent">${label}</span>
      <button class="btn sm ghost" id="btn-clear-scope">清除范围</button>
    `;
    bar.querySelector("#btn-clear-scope")?.addEventListener("click", () => {
      state.scope = { type: "all" };
      helpers.loadJobs();
      renderScopeBar();
    });
  }

  helpers._renderScopeBar = renderScopeBar;
}
