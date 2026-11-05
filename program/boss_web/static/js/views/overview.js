/** 总览只汇总本地数据。进入页面不会采集、调用模型或发送。 */
import { api } from '../api.js';
import { escapeHtml as e } from '../ui.js';
import { pageHeader } from '../panel_ui.js';
import { readScoringReport } from './scoring_progress.js';

const number = value => value == null || value === '' || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('zh-CN');
const metric = (title, value, note, href) => `<a class="card overview-metric" href="${href}"><span>${e(title)}</span><strong>${e(number(value))}</strong><small>${e(note)}</small></a>`;
const step = (index, title, status, note, href) => `<a class="overview-step" href="${href}"><div class="overview-step-top"><span class="overview-step-index">${index}</span><h3>${e(title)}</h3><span aria-hidden="true">↗</span></div><p>${e(status)}</p><small>${e(note)}</small></a>`;
const notice = (title, text, href, label) => `<div class="overview-notice"><strong>${e(title)}</strong><p>${e(text)}</p>${href ? `<a href="${href}">${e(label)} →</a>` : ''}</div>`;

export async function renderOverview(root) {
  root.innerHTML = pageHeader('MY CAREER / OVERVIEW', '求职总览', '看清每一步的结果，再和你的 AI 一起调整。', '<button class="btn sm" type="button" data-overview-refresh>刷新本地数据</button>') + `
    <div class="overview-metrics" data-overview-metrics aria-label="求职关键数据"><div class="card">正在核对本地记录…</div></div>
    <div class="overview-columns"><section class="card"><div class="card-head"><h2 class="card-title">我的求职流程</h2><span class="card-sub">按阶段查看</span></div><div class="overview-flow" data-overview-flow></div></section>
    <aside><section class="card"><div class="card-head"><h2 class="card-title">现在值得留意</h2><span class="pill static">本地观察</span></div><div class="overview-attention" data-overview-attention></div></section>
    <div class="overview-companion"><h3>当前输入与运行结果</h3><p>各环节可查看实际采用的条件、模板及对应结果。与用户 AI 对齐后的新配置，在程序采用后显示。</p><a href="#/settings">查看当前个人方案 →</a></div></aside></div>
    <div class="panel-footer-note"><span>查看面板只读取本地记录；执行任务由你与 AI 确认后发起。</span><span data-overview-stamp>核对中</span></div>`;
  let disposed = false, busy = false;
  async function load() {
    if (disposed || busy) return;
    busy = true;
    try {
      const results = await Promise.allSettled([
        api.get('/api/workflow/overview'), readScoringReport(), api.get('/api/workflow/feedback'),
        api.get('/api/ai/context'), api.get('/api/resume/list'), api.get('/api/platform/control'),
      ]);
      if (disposed) return;
      const get = i => results[i].status === 'fulfilled' && results[i].value?.ok !== false ? results[i].value : null;
      const overview = get(0), score = get(1), feedback = get(2), context = get(3), resumes = get(4), platform = get(5);
      const corpus = overview?.corpus || {}, quota = overview?.quota || {}, overall = feedback?.overall || {};
      const current = (resumes?.items || []).find(item => item.resume_id === context?.effective?.candidate?.resume_id);
      const resumeStatus = current ? current.title || '已选定求职简历' : context?.effective?.candidate?.resume_id ? '已绑定简历，名称未读到' : resumes?.items?.length ? '已有简历，请核对采用版本' : resumes ? '还没有上传简历' : '简历状态未读到';
      const keywords = context?.effective?.search?.keywords || [];
      const threshold = context?.effective?.thresholds?.admit_min_score ?? score?.min_score;
      root.querySelector('[data-overview-metrics]').innerHTML = [
        metric('已保存的不重复岗位', corpus.total_jobs, corpus.with_jd == null ? '岗位全文情况未读到' : `${number(corpus.with_jd)} 个已有岗位全文`, '#/tasks'),
        metric('等待评分', score?.unscored_todo, '已排除硬条件不合格与待补全文', '#/match'),
        metric('待审核招呼语', score?.ready, '已生成并通过程序校验', '#/review'),
        metric('今日已发送', quota.confirmed, quota.daily_cap == null ? '按本地已确认发送记录统计' : `每日额度 ${number(quota.daily_cap)} 条`, '#/review'),
      ].join('');
      root.querySelector('[data-overview-flow]').innerHTML = [
        step('01', '简历与资料', resumeStatus, '核对原文与事实；优化与否由你选择。', '#/resume'),
        step('02', '岗位采集', keywords.length ? `${keywords.length} 个在用搜索词` : context ? '采集条件待对齐' : '采集方案未读到', '看城市、薪资与搜索范围，也看新增趋势。', '#/tasks'),
        step('03', '匹配评分', threshold == null ? '候选池门槛未读到' : `候选池准入 ≥ ${threshold} 分`, '看岗位样例与理由，校准你的偏好。', '#/match'),
        step('04', '招呼语与发送', score ? `${number(score.ready)} 条可审核 · ${number(score.eligible_not_ready)} 条待处理` : '写稿与发送情况未读到', '先确认文案，发送后保留实际记录。', '#/review'),
        step('05', '每日复盘', feedback ? `${number(overall.replied)} 条确认有回复 · ${number(overall.unknown)} 条待核实` : '回复记录未读到', '按原发送日期复盘，待核实不等于未回复。', '#/feedback'),
        step('06', '面试准备', '收到邀请后，再针对岗位练习', '找回 JD、简历与招呼语，选择练习方法。', '#/interview'),
      ].join('');
      const notes = [];
      const scopes = platform?.paused_scopes || {};
      const pausedScopes = Array.isArray(scopes) ? scopes : Object.entries(scopes).filter(([, value]) => value).map(([key]) => key);
      if (platform?.account_paused || pausedScopes.length) notes.push(notice('部分平台操作已暂停', '已有本地资料仍可查看。先把异常信息交给 AI 核对，再决定后续操作；刷新页面不会恢复任务。', '#/activity', '查看任务与暂停状态'));
      if (score?.eligible_not_ready > 0) notes.push(notice(`${number(score.eligible_not_ready)} 个合格岗位仍需处理文案`, '评分通过后还要写稿并完成校验，才会进入待审核发送。', '#/review?tab=pending', '查看待写稿与原因'));
      if (overall.unknown > 0) notes.push(notice('回复统计尚未完整核实', `还有 ${number(overall.unknown)} 条投递的回复情况待核实，目前不能当作未回复来计算完整效果。`, '#/feedback', '查看回复覆盖'));
      if (score?.unscored_todo > 0) notes.push(notice(`${number(score.unscored_todo)} 个岗位等待评分`, '先看当前批次是否在推进，再让 AI 判断是正常等待还是需要调整。', '#/match', '查看评分进展'));
      if (results.some(result => result.status === 'rejected') || results.some((_, i) => !get(i))) notes.push(notice('部分状态未读取完整', '未读到的数据用“—”表示。可刷新本地数据，或把问题交给 AI 检查，不能按 0 理解。'));
      if (!notes.length) notes.push(notice('从具体样例判断效果', '打开评分或招呼语，看几份真实岗位是否符合你的判断；想调整时，把例子交给 AI。', '#/match', '查看匹配结果'));
      root.querySelector('[data-overview-attention]').innerHTML = notes.join('');
      root.querySelector('[data-overview-stamp]').textContent = `本地核对 ${new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'})}`;
    } finally { busy = false; }
  }
  root.querySelector('[data-overview-refresh]').onclick = load;
  await load();
  const timer = setInterval(() => load().catch(() => {}), 30000);
  return () => { disposed = true; clearInterval(timer); };
}
