/** 跨批次评分审查：只读本地已保存结果，不重新评分或访问平台。 */
import { api } from '../api.js';
import { escapeHtml as e, fmtTime, modal } from '../ui.js';
import { scoringRuleHtml } from './stage_inputs.js';
import { promptReadingHtml, executionOriginalHtml } from './input_reading.js';
import { readScoreIndex, hasValidScore } from './score_data.js';

// 固定数量的页码，几百页时也可直达首尾和当前位置附近。
export function scorePageNumbers(page, pages) {
  const selected = new Set([0, pages - 1]);
  const start = Math.max(1, Math.min(page - 2, pages - 6));
  for (let p = start; p <= Math.min(pages - 2, start + 4); p++) selected.add(p);
  const result = [];
  for (const p of [...selected].sort((a,b) => a-b)) {
    if (result.length && p - result[result.length - 1] > 1) result.push('gap');
    result.push(p);
  }
  return result;
}

function paginationHtml(result, size) {
  const first = result.total ? result.page * size + 1 : 0;
  const last = Math.min((result.page + 1) * size, result.total);
  return `<span class="score-page-range">第 ${first}–${last} 条 / 共 ${result.total} 条 · 第 ${result.page+1} / ${result.pages} 页</span><div class="score-page-actions"><div class="score-page-numbers"><button type="button" class="btn sm ghost" data-score-go="${result.page-1}" ${result.page===0 ? 'disabled' : ''} aria-label="上一页">‹</button>${scorePageNumbers(result.page,result.pages).map(p => p === 'gap' ? '<span class="score-page-gap">…</span>' : `<button type="button" class="btn sm ghost" data-score-go="${p}" aria-label="第${p+1}页" ${p===result.page ? 'aria-current="page"' : ''}>${p+1}</button>`).join('')}<button type="button" class="btn sm ghost" data-score-go="${result.page+1}" ${result.page+1>=result.pages ? 'disabled' : ''} aria-label="下一页">›</button></div><form class="score-page-jump" data-score-jump><label>跳至 <input class="input" type="number" min="1" max="${result.pages}" step="1" value="${result.page+1}" aria-label="跳转页码" required> 页</label><button type="submit" class="btn sm ghost">前往</button></form></div>`;
}

export function selectScoreResults(rows, {query='',band='all',sort='high',page=0,size=20,threshold=null,status='all'}={}) {
  const q = query.trim().toLocaleLowerCase();
  const filtered = rows.filter(r => {
    const m = r.item || {}, score = Number(m.match_score);
    if (status !== 'all' && (status === 'legacy' ? m.evaluation_mode === 'single_pass' : m.evaluation_mode !== 'single_pass' || m.status !== status)) return false;
    const hasScore = hasValidScore(m);
    if (q && ![m.job_name,m.brand_name,m.location,m.salary_desc,m.verdict,m.advice].join(' ').toLocaleLowerCase().includes(q)) return false;
    if (band === 'high' && (threshold == null || !hasScore || score < threshold)) return false;
    if (band === 'low' && (threshold == null || !hasScore || score >= threshold)) return false;
    return true;
  });
  filtered.sort((a,b) => sort === 'new' ? Number(b.analysis_at || 0)-Number(a.analysis_at || 0)
    : ((Number(a.item.match_score ?? -1)-Number(b.item.match_score ?? -1)) * (sort === 'low' ? 1 : -1)) || Number(b.analysis_at || 0)-Number(a.analysis_at || 0));
  const pages = Math.max(1,Math.ceil(filtered.length/size));
  const current = Math.max(0,Math.min(page,pages-1));
  return {items:filtered.slice(current*size,(current+1)*size),total:filtered.length,page:current,pages};
}

export function scoreResultLabel(m) {
  if (m.evaluation_mode === 'single_pass') return ({succeeded:'符合标准 · 详评',rejected:'未达标 · 简评',needs_review:'待核实',hard_filtered:'硬条件排除 · 未调模型',failed:'评分失败'})[m.status] || '一次评分';
  return m.stage === 'screen' ? '历史初筛' : m.stage === 'full' ? '历史详评' : '历史记录';
}

export function scoreDetailHtml(r) {
  const m=r.item;
  const evidence=[['主要匹配点',m.pros],['不足与待核实',m.cons],['匹配技能',m.matched_skills],['缺少技能',m.missing_skills],['关键待核实事项',m.unknowns]]
    .filter(([,v])=>Array.isArray(v) && v.length)
    .map(([title,v])=>`<section class="score-evidence"><h4>${title}</h4><ul>${v.map(t=>`<li>${e(t)}</li>`).join('')}</ul></section>`).join('');
  return `<div class="score-detail-meta"><span class="score-detail-mark">${e(m.match_score ?? '—')}<small>分</small></span><div><strong>${e(m.brand_name || '公司未记录')}</strong><p>${e(m.location || '地点未记录')} · ${e(m.salary_desc || '薪资未记录')}</p><small>${e(scoreResultLabel(m))} · ${e(fmtTime(m.analyzed_at || r.analysis_at))} · 简历：${e(r.resume_title || r.resume_id || '未记录')}</small></div></div>
    <div class="score-detail-layout"><section class="score-jd-column"><h4>岗位全文</h4><p class="io-note">${m.input_jd != null ? '本次实际送入评分模型的 JD；保留截取标记。' : '本地当前保存的版本；历史评分时的JD可能不同。'}</p><div class="io-prose score-jd-copy" data-score-jd>读取中…</div></section><section class="score-assessment-column"><h4>为什么得到这个分数</h4><div class="score-verdict">${e(m.verdict || m.advice || m.error || '本条未保存评分理由')}</div>${evidence}${m.advice && m.advice !== m.verdict ? `<section class="score-evidence"><h4>补充说明</h4><p>${e(m.advice)}</p></section>` : ''}<p class="io-note">以上是模型当时保存的评估说明，供你对照岗位内容审查。</p></section></div><div class="score-detail-rule">${m.candidate_material?.text ? `<details class="io-secondary"><summary>本次实际使用的候选人材料 · ${e((m.candidate_material.sha || "").slice(0,12))}</summary><div class="io-prose">${promptReadingHtml(m.candidate_material.text,{translateFields:false})}</div>${executionOriginalHtml(m.candidate_material.text,"当次候选人材料原文")}<p class="io-note">当次保存的材料，不随当前个人资料变化。</p></details>` : '<p class="io-note">这条记录未保存当次候选人材料，不能用当前材料代替历史输入。</p>'}${scoringRuleHtml(m.scoring_rule)}</div>${m.greeting ? `<details class="io-secondary"><summary>本条保存的招呼语</summary><div class="io-prose">${e(m.greeting)}</div></details>` : ''}`;
}

export async function renderScoreArchive(pane, threshold) {
  pane.innerHTML = `<div class="score-archive"><h3 class="card-title">全部岗位评分</h3><p class="io-note">跨批次汇总，同一岗位展示最近保存的一次结果。新结果为一次评分，未达标保留简评，关键未知单独待核实；旧初筛与详评标为历史。旧结果不会按当前标准自动重算。</p>
    <div class="score-filters"><label>搜索岗位 / 公司<input class="input" data-score-query placeholder="也可搜索城市、薪资或评分理由"></label><label>分数范围<select class="select" data-score-band><option value="all">全部分数</option>${threshold == null ? '' : `<option value="high">${e(threshold)}分及以上</option><option value="low">低于${e(threshold)}分</option>`}</select></label><label>结果类型<select class="select" data-score-status aria-label="结果类型"><option value="all">全部结果</option><option value="succeeded">符合标准 · 详评</option><option value="rejected">未达标 · 简评</option><option value="needs_review">待核实</option><option value="hard_filtered">硬条件排除</option><option value="failed">评分失败</option><option value="legacy">历史两阶段结果</option></select></label><label>排序<select class="select" data-score-sort><option value="high">分数从高到低</option><option value="low">分数从低到高</option><option value="new">最近评分优先</option></select></label><button class="btn sm" data-score-refresh>刷新结果</button></div>
    <div class="score-view-options"><p class="io-note" data-score-count role="status">读取已保存的评分…</p><label>每页 <select class="select" data-score-size aria-label="每页条数"><option value="20">20条</option><option value="50">50条</option><option value="100">100条</option></select></label></div><nav class="score-pagination" data-score-pagination aria-label="评分列表上方分页"></nav><div data-score-list></div><nav class="score-pagination" data-score-pagination aria-label="评分列表下方分页"></nav></div>`;
  const $ = s => pane.querySelector(s);
  let rows=[], page=0, size=20, disposed=false, busy=false, dialog=null;
  function paint() {
    if (disposed) return;
    const result=selectScoreResults(rows,{query:$('[data-score-query]').value,band:$('[data-score-band]').value,sort:$('[data-score-sort]').value,page,size,threshold,status:$('[data-score-status]').value});
    page=result.page;
    $('[data-score-count]').textContent=`已记录 ${rows.length} 个不重复岗位 · 当前筛选 ${result.total} 个。${threshold == null ? '当前候选池分数线未读到，暂不划分高低分组。' : '分数范围按当前候选池线划分，不表示已通过全部硬条件。'}`;
    $('[data-score-list]').innerHTML=result.items.map(r=>{
      const m=r.item, score=m.match_score == null ? '—' : m.match_score;
      return `<button class="score-result" data-score-id="${e(m.encrypt_job_id)}"><strong class="score-number">${e(score)}<small>分</small></strong><span class="score-result-main"><strong>${e(m.job_name || '未记录岗位名')}</strong><span>${e(m.brand_name || '')} · ${e(m.location || '地点未记录')} · ${e(m.salary_desc || '薪资未记录')}</span><span class="score-reason">${e(m.verdict || m.advice || (m.error ? '评分失败：'+m.error : '未附评分理由'))}</span><small>${e(scoreResultLabel(m))} · ${e(fmtTime(m.analyzed_at || r.analysis_at))} · 简历：${e(r.resume_title || r.resume_id || '未记录')}</small></span><span class="score-open">查看 JD 与理由 →</span></button>`;
    }).join('') || '<div class="empty"><p>没有符合当前查看条件的评分记录</p></div>';
    for (const nav of pane.querySelectorAll('[data-score-pagination]')) {
      nav.innerHTML = paginationHtml(result,size);
    }
  }
  async function load(force = false) {
    if (busy || disposed) return;
    busy=true; $('[data-score-refresh]').disabled=true;
    try { const d=await readScoreIndex(force); if(disposed)return; rows=Object.values(d.items || {}); paint(); }
    catch(err) { if(!disposed)$('[data-score-count]').textContent=`读取失败：${err.message}${rows.length ? '；保留上次读到的记录。' : ''}`; }
    finally {busy=false; if(!disposed)$('[data-score-refresh]').disabled=false;}
  }
  async function detail(id) {
    const r=rows.find(r=>r.item.encrypt_job_id===id); if(!r)return;
    const m=r.item;
    dialog?.close();
    dialog=modal({title:e(m.job_name || '评分详情'),wide:true,body:scoreDetailHtml(r)});
    dialog.box.classList.add('score-detail-modal');
    const active=dialog;
    if (m.input_jd != null) { active.box.querySelector('[data-score-jd]').textContent=m.input_jd; return; }
    try { const job=await api.get('/api/jobs/'+encodeURIComponent(id)); const host=active.box.querySelector('[data-score-jd]'); if(host)host.textContent=job.job_desc || '本地尚未保存岗位全文。'; }
    catch(err) { const host=active.box.querySelector('[data-score-jd]'); if(host)host.textContent='全文读取失败：'+err.message; }
  }
  $('[data-score-query]').oninput=()=>{page=0;paint();};
  for(const key of ['band','sort','status'])$(`[data-score-${key}]`).onchange=()=>{page=0;paint();};
  function go(next) {
    if (!Number.isSafeInteger(next)) return;
    page=next; paint(); $('[data-score-count]').scrollIntoView({block:'start'});
  }
  for (const nav of pane.querySelectorAll('[data-score-pagination]')) {
    nav.onclick=event=>{
      const button=event.target.closest('[data-score-go]');
      if (button && !button.disabled) go(Number(button.dataset.scoreGo));
    };
    nav.onsubmit=event=>{
      const form=event.target.closest('[data-score-jump]');
      if (!form) return;
      event.preventDefault();
      if (form.reportValidity()) go(Number(form.querySelector('input').value)-1);
    };
  }
  $('[data-score-size]').onchange=()=>{
    const next=Number($('[data-score-size]').value);
    if (![20,50,100].includes(next)) return;
    const first=page*size; size=next; page=Math.floor(first/size); paint();
    $('[data-score-count]').scrollIntoView({block:'start'});
  };
  $('[data-score-refresh]').onclick=()=>load(true);
  $('[data-score-list]').onclick=event=>{const button=event.target.closest('[data-score-id]');if(button)detail(button.dataset.scoreId);};
  await load();
  return ()=>{disposed=true;dialog?.close();};
}
