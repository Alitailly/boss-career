/** 只读的评分进展：未评分总量不等于可执行积压。 */
import { api } from '../api.js';
import { escapeHtml as e } from '../ui.js';
import { readScoreIndex, summarizeScores } from './score_data.js';

const count = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : null;
const display = value => value === null ? '—' : value.toLocaleString('zh-CN');
const activeStates = ['running', 'queued', 'starting'];

export function scoringModel(report, task = {}, now = Date.now() / 1000) {
  const ok = report?.ok === true;
  const todo = ok ? count(report.unscored_todo) : null;
  const blocked = ok ? count(report.unscored_blocked) : null;
  const analyzed = ok ? count(report.analyzed) : null;
  const missing = ok ? count(report.missing_desc) : null;
  const running = activeStates.includes(task?.status);
  const done = count(task?.done ?? task?.progress?.done);
  const total = count(task?.total ?? task?.progress?.total);
  const began = count(task?.created_at), ended = count(task?.ended_at);
  const seconds = began !== null && began > 0 && (running || ended > 0) ? Math.max(0, (running ? now : ended) - began) : null;
  let message = '评分队列暂未核实，请刷新或让 AI 检查。';
  if (todo === 0 && blocked > 0) message = '当前没有可评分积压；未评分岗位被现有硬条件预筛排除，不会进入评分队列。';
  else if (todo === 0) message = '当前没有可评分积压，后续符合条件的新岗位会进入队列。';
  else if (todo > 0) message = running ? '正在消化评分队列，可观察完成数是否持续增加。' : '有岗位等待评分；空闲可能在等半价窗口、下一轮采集，或需要检查任务状态。';
  if (running && todo === 0) message = '当前批次仍在收尾；队列为0不代表这批已经完成，以实时任务状态为准。';
  return { ok, todo, blocked, analyzed, missing, running, done, total, seconds, message, status: task?.status || '' };
}

export function scoringQueueHtml(report, task = {}, includeNote = true) {
  const m = scoringModel(report, task);
  return `<div class="ms-counts">
    <div class="ms-threshold"><span>进入候选池的分数线</span><strong>${report?.ok === true && count(report.min_score) !== null ? `≥ ${display(count(report.min_score))}` : '—'}</strong></div>
    <div><span>已有分析记录</span><strong>${display(m.analyzed)}</strong></div>
    <div class="${m.todo > 0 ? 'ms-queued' : ''}"><span>真正待评分</span><strong>${display(m.todo)}</strong></div>
    <div><span>硬条件预筛排除</span><strong>${display(m.blocked)}</strong></div>
  </div><p class="ms-explanation">${e(m.message)}</p>
  ${includeNote ? `<p class="ms-note">${m.missing === null ? '待补全文数量未读到' : `另有 ${display(m.missing)} 个岗位待补全文`}。已有分析含未达标、待核实与历史初筛结果，不等于全部可投；硬条件排除不计入评分积压。</p>` : ''}`;
}

export function scoringOverviewHtml(report, index) {
  const threshold = report?.ok === true ? count(report.min_score) : null;
  const s = summarizeScores(index, threshold);
  return `<div class="ms-result-flow" aria-label="评分结果概览">
    <div><span>已分析岗位</span><strong>${display(s.analyzed)}<small> 个</small></strong><p>同一岗位只计最近一次有效分数</p></div>
    <span class="ms-flow-arrow" aria-hidden="true">→</span>
    <div class="ms-result-threshold"><span>按当前候选池分数线查看</span><strong>${threshold === null ? '—' : `≥ ${display(threshold)}`}<small> 分</small></strong><p>${threshold === null ? '分数线尚未读到' : `含 ${display(threshold)} 分`}</p></div>
    <span class="ms-flow-arrow" aria-hidden="true">→</span>
    <div class="ms-result-high"><span>分数达标岗位</span><strong>${display(s.high)}<small> 个</small></strong><p>${s.low === null ? '分数线未读到，暂不划分' : `另有 ${display(s.low)} 个低于分数线`}${s.review > 0 ? `；达标中 ${display(s.review)} 个待核实` : ''}</p></div>
  </div><p class="ms-note">${s.ungraded > 0 ? `另有 ${display(s.ungraded)} 条无有效分数的记录（如失败或未调模型），未计入已分析数。` : ''}统计覆盖全库，包含历史标准的结果；这里只按当前分数线归类，未重新评分。分数达标不等于已通过发送检查。</p>`;
}

// 多处面板共享短缓存和在途请求，避免同时反复计算同一份准入报告。
let cached = null, cachedAt = 0, inflight = null;
export async function readScoringReport(force = false) {
  if (inflight) return inflight;
  if (!force && cached && Date.now() - cachedAt < 15000) return cached;
  inflight = api.get('/api/workflow/sendable' + (force ? '?force=true' : ''));
  try { const result = await inflight; cached = result; cachedAt = Date.now(); return result; }
  finally { inflight = null; }
}

export async function mountScoringProgress(host) {
  let disposed = false, busy = false, report = null, index = null, task = {}, failures = [];
  host.classList.add('ms-panel', 'card', 'mb-16');
  host.innerHTML = `<div class="card-head"><h3 class="card-title">评分结果概览</h3><button type="button" class="btn sm ghost" data-ms-refresh>刷新</button></div><div data-ms-body><p class="muted">读取已分析岗位和达标数量…</p></div>`;
  function render() {
    const m = scoringModel(report, task);
    const labels = {done:'已完成',error:'失败',cancelled:'已取消',running:'进行中',queued:'排队中',starting:'启动中',idle:'空闲'};
    const duration = m.seconds === null ? '' : ` · ${m.running ? '已运行' : '本批用时'} ${Math.round(m.seconds)} 秒`;
    const progress = m.total === null ? '' : ` ${display(m.done)} / ${display(m.total)} 个`;
    const stamp = String(report?.at || '').replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/, '$1-$2-$3 $4:$5:$6');
    const current = failures.includes('实时任务') ? '实时匹配任务读取失败，状态未知' : task?.task_id ? `${m.running ? '当前批次' : '最近批次'}：${e(labels[m.status] || m.status)}${progress}${duration}` : '当前没有匹配批次记录';
    const detailOpen = host.querySelector('.ms-details')?.open;
    host.querySelector('[data-ms-body]').innerHTML = scoringOverviewHtml(report, index) + `<div class="ms-queue-line"><span>等待评分 <b>${display(m.todo)}</b></span><span>评分前硬条件排除 <b>${display(m.blocked)}</b></span><span>待补岗位全文 <b>${display(m.missing)}</b></span></div><details class="ms-details"${detailOpen ? ' open' : ''}><summary>${m.running ? `当前评分批次进行中${progress}` : '查看任务状态与后续衔接'}</summary><div class="ms-current" role="status">${current}</div><p class="ms-note">${e(m.message)}</p><p class="ms-note">当前可继续写稿或审核的候选：${report?.ok === true ? display(count(report.eligible)) : '—'} 个（另经硬条件、已发送与冷却等检查，不能与上方达标总数混算）。<a href="#/review">查看招呼语与发送 →</a></p><p class="ms-note">${stamp ? `队列数据：${e(stamp)}。` : ''}评分索引缓存最多30秒，任务状态每5秒核对。${failures.length ? `读取不完整：${e(failures.join('、'))}` : ''}</p></details>`;
  }
  async function refresh(force = false) {
    if (disposed || busy) return;
    busy = true;
    try {
      const results = await Promise.allSettled([readScoringReport(force), api.get('/api/match/analyze/status'), readScoreIndex(force)]);
      if (disposed) return;
      report = results[0].status === 'fulfilled' ? results[0].value : null;
      task = results[1].status === 'fulfilled' ? results[1].value : {};
      index = results[2].status === 'fulfilled' ? results[2].value : null;
      failures = results.flatMap((r, i) => r.status === 'rejected' ? [['评分队列','实时任务','评分结果'][i]] : []);
      render();
    } finally { busy = false; }
  }
  host.querySelector('[data-ms-refresh]').addEventListener('click', () => refresh(true));
  await refresh();
  const timer = setInterval(() => refresh(), 5000);
  return {refresh,dispose(){disposed=true;clearInterval(timer);}};
}
