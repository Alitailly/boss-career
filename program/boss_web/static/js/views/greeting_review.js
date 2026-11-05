/** 招呼语审查：展示阶段衔接及当次依据，无写稿或发送动作。 */
import { api } from '../api.js';
import { escapeHtml as e, modal } from '../ui.js';
import { scoringRuleHtml } from './stage_inputs.js';
import { promptReadingHtml, executionOriginalHtml } from './input_reading.js';

const list = values => Array.isArray(values) && values.length ? `<ul>${values.map(v=>`<li>${e(v)}</li>`).join('')}</ul>` : '<p class="io-note">未记录</p>';
export function greetingReviewHtml(d, row = {}) {
  const input=d.inputs || {}, job=input.job || {}, assessment=d.analysis || {};
  const output=row.text ?? d.output?.text ?? '';
  const same = !row.text || row.text === d.output?.text;
  const recorded = input.recorded && same;
  return `<div class="greeting-chain"><span>采集：岗位 JD</span><b>→</b><span>匹配：岗位摘要与匹配点</span><b>＋</b><span>个人事实与写作规则</span><b>→</b><span>招呼语正文</span></div>
    <div class="greeting-review-layout"><section class="greeting-input-column"><h3>输入 · 这条文案依据什么</h3><p class="io-note">${e(same ? input.note : '队列中的稿件已变化；未将新稿输入冒充当前这段正文的依据。请刷新队列核对。')}</p>
    <section class="greeting-evidence"><h4>① 采集的岗位与写稿看到的内容</h4><p><strong>${e(row.title || d.job?.title || '岗位未记录')}</strong> · ${e(row.company || d.job?.company || '公司未记录')}</p>
      ${recorded ? `<p class="io-note">实际写稿采用评分摘要与原文摘录，不是整份JD。</p><div class="io-prose">${e(job.jd_brief || '未提供摘要')}</div>${list(job.jd_evidence)}` : '<p class="io-note">未保存本条当时实际送入写稿的岗位摘要与摘录。</p>'}
      <details class="io-secondary"><summary>对照本地岗位全文</summary><p class="io-note">${e(d.job?.jd_source || '')}</p><div class="greeting-jd-copy">${e(d.job?.jd || '本地未保存全文')}</div></details></section>
    <section class="greeting-evidence"><h4>② 匹配评分传给写稿的依据</h4><p class="io-note">${e(d.analysis_note || '')}</p><p>${e(assessment.score ?? assessment.match_score ?? row.score ?? '—')} 分 · ${e(assessment.verdict || '未记录结论')}</p>
      ${recorded ? `<p class="io-note">实际传入的匹配点</p>${list(job.match_points)}` : list(assessment.pros)}
      ${assessment.candidate_material?.text ? `<details class="io-secondary"><summary>评分时使用的候选人材料</summary><div class="io-prose">${promptReadingHtml(assessment.candidate_material.text,{translateFields:false})}</div></details>` : ''}
      ${scoringRuleHtml(assessment.scoring_rule)}</section>
    <section class="greeting-evidence"><h4>③ 写稿可引用的个人事实</h4>${recorded ? `<p class="io-note">模型实际收到的事实；“已引用”是写稿模型返回的引用编号。</p><div class="greeting-facts">${(input.facts || []).map(f=>`<div><span>${e(f.id)}${(d.output?.used_fact_ids || []).includes(f.id) ? ' · 已引用' : ''}</span><p>${e(f.text)}</p></div>`).join('')}</div>` : '<p class="io-note">历史稿件未留存这部分输入，不以当前个人资料补填。</p>'}</section>
    <details class="io-secondary"><summary>④ ${recorded ? '本条实际采用的完整写作规则' : '当前写作规则 · 仅供后续生成参考'}</summary><p class="io-note">${e(recorded ? '写稿时保存的规则与输出格式。' : d.current_note)}</p><div class="io-prose">${promptReadingHtml(recorded ? input.rules : d.current_rules || '未读取到')}</div>${executionOriginalHtml(recorded ? input.rules : d.current_rules || "")}</details></section>
    <section class="greeting-output-column"><span class="io-kicker">输出 · 核对实际文案</span><h3>${output ? '本条招呼语正文' : '尚未生成招呼语'}</h3><div class="greeting-output-copy">${e(output || '已有评分与岗位资料保留在左侧，后续由用户 AI 发起写稿。')}</div><p class="io-note">${output ? `${output.length} 字符 · 对照岗位职责、个人事实与写作规则审查。` : '查看资料不会触发生成或发送。'}</p><div class="greeting-output-note">评分结论帮助选取相关内容；对外经历与能力只能依据个人事实，评分模型的推断不能当作新经历。</div></section></div>`;
}

export async function openGreetingReview(row) {
  const dialog=modal({wide:true,title:`${row.title || '岗位'} · 招呼语输入与输出`,body:'读取本地写稿依据…'});
  dialog.box.classList.add('greeting-review-modal');
  try {
    const query=new URLSearchParams();
    if(row.analysis_id)query.set('analysis_id',row.analysis_id);
    if(row.text) {
      const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(row.text));
      query.set('text_sha',[...new Uint8Array(digest)].map(b=>b.toString(16).padStart(2,'0')).join('').slice(0,16));
    }
    const d=await api.get('/api/ai/greeting-inputs/'+encodeURIComponent(row.job_id)+'?'+query);
    const body=dialog.box.querySelector('.modal-body');
    if (body) body.innerHTML=greetingReviewHtml(d,row);
  } catch(err) {
    const body=dialog.box.querySelector('.modal-body');
    if(body)body.innerHTML=`<p class="io-note">写稿依据读取失败：${e(err.message)}</p><h3>本条正文</h3><div class="io-prose">${e(row.text || '尚未写稿')}</div>`;
  }
  return dialog;
}
