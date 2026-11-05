import { api } from '../api.js';
import { escapeHtml as e } from '../ui.js';
import { mountControls } from './workflow_controls.js';
import { mountCollectionPanel } from './collection_panel.js';
import { mountReplyReview } from './reply_review.js';
import { pageHeader, feedbackButton, wireSections } from '../panel_ui.js';
import { renderCrawls } from './match_crawls.js';
import { mountStageInputs } from './stage_inputs.js';

function page(mode, title) {
  return async view => {
    view.innerHTML = pageHeader('资料与设置', title, '核对当前使用的资料与规则；需要定制时，和你的 AI 确认后再采用。') + `<div id="wf-controls"></div>`;
    return mountControls(view.querySelector('#wf-controls'), mode);
  };
}
export async function renderReview(view) {
  view.innerHTML = pageHeader('04 / CONTACT', '招呼语与发送', '对照当前写作规则、岗位资料与实际正文，查看待发送和已发送记录。') + '<div id="review-inputs"></div><div id="wf-controls"></div><div class="panel-footer-note"><span>回复记录与每日统计</span><a class="review-history-link" href="#/feedback">查看每日复盘 →</a></div>';
  const [disposeInputs, disposeControls] = await Promise.all([mountStageInputs(view.querySelector('#review-inputs'), 'greeting'), mountControls(view.querySelector('#wf-controls'), 'review')]);
  return () => { disposeInputs(); disposeControls(); };
}
export async function renderTasks(view) {
  view.innerHTML = pageHeader('02 / DISCOVER', '岗位采集', '核对实际采集条件与关键词，查看不重复岗位的新增和长期记录。') + `
    <div class="section-tabs" role="tablist" aria-label="采集查看方式"><button type="button" role="tab" id="collection-tab-overview" data-section-tab="overview" aria-controls="collection-overview">采集概览</button><button type="button" role="tab" id="collection-tab-history" data-section-tab="history" aria-controls="collection-history">长期抓取记录</button><a class="section-link" href="#/jobs">浏览本地岗位 ↗</a></div>
    <div id="collection-overview" role="tabpanel" aria-labelledby="collection-tab-overview" data-section-panel="overview"><div id="wf-collection"></div></div>
    <div class="card" id="collection-history" role="tabpanel" aria-labelledby="collection-tab-history" data-section-panel="history" hidden><div id="collection-crawls"></div></div>`;
  let disposed = false, historyLoaded = false;
  const collectionPromise = mountCollectionPanel(view.querySelector('#wf-collection'));
  wireSections(view, { initial:'overview', onChange(name) {
    if (name !== 'history' || historyLoaded || disposed) return;
    historyLoaded = true;
    renderCrawls(view.querySelector('#collection-crawls'), {}, {setMatchScope(taskId, jobCount) {
      location.hash = `#/match?crawl_task=${encodeURIComponent(taskId)}&count=${Number(jobCount) || 0}`;
    }}).catch(err => {
      historyLoaded = false;
      if (!disposed) view.querySelector('#collection-crawls').innerHTML = `<div class="banner warn">${e(err.message || '历史记录读取失败，请切换后重试。')}</div>`;
    });
  }});
  const collection = await collectionPromise;
  return () => { disposed = true; collection.dispose(); };
}
export async function renderFeedback(view) {
  view.innerHTML = pageHeader('05 / REFLECT', '每日复盘', '按原发送日期查看回复、等待时间，以及对应岗位和实际发送正文。') + '<div id="wf-reply-review"></div>';
  const reply = await mountReplyReview(view.querySelector('#wf-reply-review'));
  return () => reply.dispose();
}
export async function renderActivity(view) {
  view.innerHTML = pageHeader('WORKSPACE / ACTIVITY', '任务记录', '查看后台正在做什么、停在哪里，以及最近的执行结果。') + '<div id="activity-platform"></div><div id="wf-controls"></div>';
  const [disposePlatform, disposeControls] = await Promise.all([mountPlatformStatus(view.querySelector('#activity-platform')), mountControls(view.querySelector('#wf-controls'), 'tasks')]);
  return () => { disposePlatform(); disposeControls(); };
}
export const renderCandidate = page('candidate', '求职资料核对');
export const renderExecution = page('execution', '采集与执行设置');

async function mountPlatformStatus(host) {
  let disposed = false, busy = false;
  const scopes = {search:'岗位搜索',details:'岗位全文',send:'发送招呼语',chat:'回复读取',token:'平台会话',other:'其他平台请求'};
  host.innerHTML = '<section class="card mb-16"><h3 class="card-title">平台操作状态</h3><div data-platform-state class="mt-16">读取本地暂停与节奏记录…</div></section>';
  async function load() {
    if (disposed || busy) return;
    busy = true;
    try {
      const d = await api.get('/api/platform/control');
      if (disposed) return;
      const paused = Object.entries(d.paused_scopes || {}).filter(([,value]) => value).map(([key]) => scopes[key] || key);
      const pausedText = d.account_paused ? '账号范围的平台操作已暂停' : paused.length ? `${paused.join('、')}已暂停` : '本地未设置平台暂停';
      host.querySelector('[data-platform-state]').innerHTML = `<div class="banner ${d.account_paused || paused.length ? 'warn' : 'info'}"><strong>${e(pausedText)}</strong><br>${d.account_paused || paused.length ? '本地资料、评分与文案仍可查看。请先让 AI 核对异常和未决记录，再决定是否恢复。' : '这仅代表本地控制状态，不代表平台账号一定可正常使用。'}${d.paused_at ? `<br>最近暂停记录：${e(d.paused_at)}` : ''}</div><details class="strategy-versions"><summary>查看共享请求节奏（供 AI 核对）</summary><p>最近10分钟记录到 ${e(String(d.requests_last_ten_minutes ?? '—'))} 次请求；共享最短间隔 ${e(String(d.min_gap_seconds ?? '—'))} 秒；每分钟上限 ${e(String(d.max_per_minute ?? '—'))} 次，每10分钟上限 ${e(String(d.max_per_ten_minutes ?? '—'))} 次。多阶段共享预算，单个任务的等待不等于故障。</p><p>这些是当前程序约束，不保证平台不会限制账号。此页仅读取本地记录，不向平台探测，也不会解除暂停。</p></details>`;
    } catch (err) {
      if (!disposed) host.querySelector('[data-platform-state]').innerHTML = `<p class="banner warn">暂停状态未读到：${e(err.message)}。不能据此认定账号可以使用。</p>`;
    } finally { busy = false; }
  }
  await load();
  const timer = setInterval(load, 30000);
  return () => { disposed = true; clearInterval(timer); };
}

/** 工作流页顶部：只在真的有事在跑时出现，没有任务就不占位置。 */
export async function mountWorkflowStatus(host) {
  let disposed = false;
  const names = { collect: '采集', match: '匹配', prepare: '写稿', send: '发送', feedback: '同步反馈' };
  async function refresh() {
    const results = await Promise.allSettled([api.get('/api/workflow/control/tasks'), api.get('/api/match/analyze/status')]);
    if (disposed) return;
    const tasks = results[0].status === 'fulfilled' ? results[0].value.items : [];
    const match = results[1].status === 'fulfilled' ? results[1].value : {};
    const running = tasks.filter(t => ['queued', 'running', 'needs_review'].includes(t.status));
    const matching = ['running', 'queued', 'starting'].includes(match.status);
    const rows = running.map(t => `<div class="wf-running-row">${e(names[t.action] || t.action)} · ${e({ queued: '待执行', running: '执行中', needs_review: '异常中断，待核对' }[t.status])}</div>`).join('');
    const matchRow = matching ? `<div class="wf-running-row">匹配中 ${e(String(match.done ?? 0))} / ${e(String(match.total ?? 0))} · ${e(match.current_job || '')}</div>` : '';
    const broken = results.some(r => r.status === 'rejected') ? '<div class="banner warn">任务状态读取失败，刷新核对</div>' : '';
    host.innerHTML = (rows || matchRow) ? `<section class="card mb-16"><h3>正在做什么</h3>${rows}${matchRow}</section>` : broken;
  }
  await refresh();
  const timer = setInterval(() => refresh().catch(() => {}), 5000);
  return () => { disposed = true; clearInterval(timer); };
}
