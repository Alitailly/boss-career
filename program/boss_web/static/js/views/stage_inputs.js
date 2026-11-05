/** 当前输入只读展示：不生成建议、不改配置、不调用模型。 */
import { api } from '../api.js';
import { escapeHtml as e, modal, toast } from '../ui.js';
import { promptReadingHtml, structuredReadingHtml, executionOriginalHtml } from './input_reading.js';

const names = {matching:'评分', greeting:'招呼语', interview:'面试题库', resume_revision:'简历优化'};
const sources = {builtin:'基础模板', custom_prompt:'个人定制模板', external_skill:'外部方法'};
const text = value => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
function readableSections(d) {
  const sections = d.sections || [];
  if (d.mode === 'external') return sections;
  if (d.stage === 'matching') return [
    {...sections[0], title:'第一步：初筛采用的评分标准'},
    {...sections[1], title:'第二步：详评采用的评分标准'},
  ].filter(s => s.text);
  if (d.stage === 'greeting') return sections.filter(s => s.title === '当前合成写作规则').map(s => ({...s,title:'当前采用的招呼语写作要求'}));
  return sections.filter(s => !s.secondary);
}
function inputExplanation(d) {
  if (d.mode === 'external') return '当前使用外部方法，下面展示已保存的方法内容。';
  return ({
    matching:'评分模型每次收到：所选简历的结构化摘要 ＋ 下面的评分标准 ＋ 实际岗位全文。程序先批量初筛，达到详评分数线后再逐岗详评；这两步的标准都影响结果。你的个人偏好须写进实际采用的标准或条件，聊天内容不会自动传入评分模型。',
    greeting:'采集岗位 → 评分形成摘要与匹配点 → 写稿结合个人事实和写作规则生成文案。批量写稿采用岗位摘要与原文摘录；单条重生成使用JD。点开下方具体文案查看当次实际输入与输出。',
    interview:'基础出题使用所选岗位全文、简历和下面的要求。用户 AI 的模拟对话采用什么方法，以对话中的选择为准。',
    resume_revision:'简历优化使用所选简历、目标岗位或本次优化要求。这里查看生成要求，优化前后的文字在简历页面对照。',
  })[d.stage] || '';
}
function sectionHtml(s) {
  return `<section class="io-rule"><h4>${e(s.title)}</h4>${s.status === 'fallback' ? '<p class="io-note">模板读取异常，执行将回退基础模板。</p>' : ''}<div class="io-prose">${promptReadingHtml(s.text)}</div></section>`;
}

// 只按原文标题分段，不生成摘要，不从旧提示词推断用户偏好。
export function splitCriteria(value) {
  const sections = [];
  for (const line of String(value || '').split(/\r?\n/)) {
    const heading = line.match(/^#{1,4}\s+(.+?)\s*$/);
    if (heading) sections.push({title:heading[1], lines:[]});
    else {
      if (!sections.length) sections.push({title:'评分标准',lines:[]});
      sections[sections.length-1].lines.push(line);
    }
  }
  return sections.map(s=>({title:s.title,text:s.lines.join('\n').trim()})).filter(s=>s.text);
}

function legacyMatchingStandardHtml(d) {
  if (d.mode === 'external') return readableSections(d).map(sectionHtml).join('');
  const common = d.criteria?.shared && d.criteria.text;
  const rules = common ? `<p class="io-note">以下阅读版对应初筛和详评共同采用的标准，执行原文在下方保留。</p><div class="criteria-grid">${splitCriteria(common).map((s,i)=>`<section class="criteria-card"><span class="criteria-index">${String(i+1).padStart(2,'0')}</span><h4>${e(s.title)}</h4><div class="criteria-copy">${promptReadingHtml(s.text)}</div></section>`).join('')}</div>`
    : `<p class="io-note">当前标准仍按初筛、详评分开保存。下面按实际采用的两处文本排版，程序字段以中文说明；未合并或推断个人偏好。</p><div class="criteria-grid criteria-legacy">${(d.sections || []).slice(0,2).map((s,i)=>`<section class="criteria-card"><span class="criteria-index">${i === 0 ? '批量初筛' : '详细评分'}</span><h4>${i === 0 ? '决定哪些岗位值得继续细看' : '形成岗位分数与评估理由'}</h4>${s.status === 'fallback' ? '<p class="io-note">模板读取异常，执行将回退基础模板。</p>' : ''}<div class="criteria-copy">${promptReadingHtml(s.text)}</div></section>`).join('')}</div>`;
  const t=d.thresholds || {};
  return `<div class="criteria-intro"><p>查看提示词重视什么，再对照岗位内容和评分理由判断结果。分数用于参考优先程度，理由是模型给出的评估说明。</p></div>${rules}<details class="io-secondary" data-io-section="conditions"><summary>硬条件与分数线 · 候选池 ${t.admit_min_score == null ? '未设置' : e(t.admit_min_score)+'分起'}</summary>${conditionsHtml(d)}</details><details class="io-secondary" data-io-section="materials"><summary>本次评分使用哪些材料</summary><p class="io-note">${e(d.material_note || '程序使用候选人材料与岗位资料；简历事实、个人偏好和岗位要求分别参与判断。')}</p>${d.resume_id ? '<a href="#/resume">查看当前简历与资料 →</a>' : '<p class="io-note">当前未关联可定位的简历。</p>'}<p class="io-note">初筛与详评是两种执行方式；当前标准用于后续执行，历史岗位以当次记录为准。</p></details>`;
}
function candidateText(value) {
  return promptReadingHtml(value || '尚未整理评分用材料；可由自己的 AI 结合简历与补充事实整理。',{translateFields:false});
}
export function matchingStandardHtml(d) {
  if (d.mode === 'external') return readableSections(d).map(sectionHtml).join('');
  if (d.execution_mode !== 'single_pass') return '<p class="scoring-alert">当前服务仍是旧的两阶段评分。新界面已加载，后端需要重启后才能使用一次评分。</p>' + legacyMatchingStandardHtml(d);
  const material=d.candidate_material || {}, ready=material.ready === true;
  // 仅改阅读标题：旧两处规则仍原样参与同一次请求，不重新总结个人标准。
  const criteria = String(d.criteria?.text || '').replace(/^# 原初筛标准（兼容保留）$/gm,'### 方向与资格判断').replace(/^# 原详评标准（兼容保留）$/gm,'### 匹配程度与评价要求');
  const line = d.thresholds?.admit_min_score;
  return `<div class="score-input-intro"><p>模型会把<strong>候选人材料、个人评分标准、已保存的岗位条件和本批岗位 JD 一起考虑</strong>，每个岗位只评估一次。下面是这一整份输入的阅读版，不是几套单独执行的评分。</p><span class="material-status ${ready ? '' : 'material-pending'}">${ready ? '已确认 · 新评分采用' : material.state === 'stale' ? '材料已过期 · 暂不评分' : '待确认 · 暂不评分'}</span>${!ready ? `<p class="io-note">${e(material.note || '候选人材料尚未确认；下方可阅读草稿，暂不发起新评分。')}</p>` : ''}</div>
    <article class="score-input-document" aria-label="合并的评分输入阅读版">
      <header><span class="io-kicker">一份输入 · 同一次评估</span><p>请结合下列求职者资料与个人标准，逐一评估本批岗位的实际职责和任职要求，给出 0–100 分及具体理由。候选池分数线为 <strong>${line == null ? '尚未设置' : e(line)+' 分（含）'}</strong>；低于分数线保留简评，符合标准且达线时在同次结果中给出详细评价，关键情况不明确时标为待核实。</p></header>
      <div class="score-input-document-body">
        <section><h3>评分用候选人材料</h3><p class="io-note">以下材料${ready ? '已确认，后续评分采用' : '为待确认材料，当前仅供审查'}；不会再自动追加整份简历。<a href="#/resume">查看原始简历 →</a></p>${candidateText(material.text)}</section>
        <section><h3>个人评分标准</h3>${d.criteria?.legacy ? '<p class="io-note">旧版保存的两段个人标准一起参与这次评估；其中旧输出格式由程序现行格式替代，不会再分别打两轮分。</p>' : ''}${criteria ? promptReadingHtml(criteria) : '<p class="io-note">未读取到个人评分标准，不能据此确认执行依据。</p>'}</section>
        <section><h3>一起参与判断的岗位条件</h3><p>程序也会加入当前方案保存的岗位方向、岗位类型、城市、薪资和其他硬条件；并非只使用上面的评分文字。标为尚未设置的条件不会自动补成限制。</p><details class="score-input-conditions" data-io-section="conditions"><summary>展开核对这些条件的实际取值</summary>${structuredReadingHtml(d.conditions || {})}</details></section>
        <section class="score-input-jd"><h3>每批自动加入的岗位资料</h3><p>执行时会在这里加入该批岗位的名称、公司、行业、规模、地点、薪资、经验与学历要求、标签和 JD 正文。当前没有选择具体评分批次，因此这里不拿某个岗位冒充实际输入；在下方打开评分结果，可查看当次岗位全文和评分理由。</p></section>
      </div>
    </article><details class="io-secondary" data-io-section="material-original"><summary>候选人材料原文与版本 · 供用户 AI 核对</summary>${executionOriginalHtml(material.text || '', '候选人材料原文')}<p class="io-note">资料 ${e(material.candidate_id || '未关联')} · 简历 ${e(material.resume_id || '未关联')}</p></details><p class="io-note">以上是当前输入的连续阅读版，未修改实际提示词。新输入只影响后续评分，已有结果按当次记录展示。</p>`;
}
export function greetingStandardHtml(d) {
  const rule=(d.sections || []).find(s=>s.title === '当前合成写作规则')?.text || '';
  const blocks=splitCriteria(rule);
  return `<div class="greeting-chain"><span>采集：岗位 JD</span><b>→</b><span>匹配：摘要与匹配点</span><b>＋</b><span>个人事实与写作规则</span><b>→</b><span>实际招呼语</span></div><p class="io-note">批量写稿采用评分摘要与JD摘录。下方点开一条文案，可左右对照当次输入与输出；当前规则用于后续生成。</p>
    <details class="io-secondary" data-io-section="greeting-facts"><summary>当前保存的个人事实 · ${(d.candidate_facts || []).length} 条</summary><p class="io-note">${e(d.material_note || '')}</p><div class="greeting-current-facts">${(d.candidate_facts || []).map(f=>`<p><strong>${e(f.id)}</strong> ${e(f.text)}</p>`).join('')}</div></details>
    <section class="greeting-current-rules"><h3>当前采用的写作要求</h3><p class="io-note">${String(rule).length} 字符 · 阅读版按原文分组，格式字段以中文说明；执行原文保留在下方。</p>${blocks.map((b,i)=>`<details class="io-secondary" data-io-section="greeting-rule-${i}" ${i===0 ? 'open' : ''}><summary>${e(b.title === "评分标准" ? "基础写作要求" : b.title)}</summary><div class="greeting-rule-copy">${promptReadingHtml(b.text)}</div></details>`).join('')}</section>`;
}
function choice(value) {
  if (value == null) return '未设置';
  if (Array.isArray(value)) return value.map(choice).join('、') || '未设置';
  if (typeof value !== 'object') return String(value);
  if (['unknown','unset'].includes(value.mode)) return '未设置';
  if (['any','unlimited'].includes(value.mode)) return '不限';
  if ('values' in value) return choice(value.values);
  if ('value' in value) return choice(value.value);
  return text(value);
}
function conditionsHtml(d) {
  if (d.stage !== 'matching') return '';
  const t = d.thresholds || {}, c = d.conditions || {}, target = c.target || {}, salary = c.salary || {};
  const rows = [
    ...(d.execution_mode === 'single_pass' ? [] : [['进入详评分数线', t.stage2_min_score == null ? '未读到' : `${t.stage2_min_score} 分`]]),
    ['候选池分数线', t.admit_min_score == null ? '未设置' : `${t.admit_min_score} 分`],
    ['目标城市', choice(target.cities)], ['岗位方向', choice(target.directions)],
    ['薪资条件', salary.min == null && salary.max == null ? '未设金额范围' : `${salary.min ?? '不限'}—${salary.max ?? '不限'} ${salary.currency || 'CNY'}/${({day:'天',month:'月',year:'年'})[salary.period] || salary.period || '未设周期'}`],
    ['薪资比较方式', ({overlap:'区间有重叠即可',top:'岗位薪资上沿须在范围内'})[salary.compare] || '未设置'],
  ];
  return `<div class="io-conditions">${rows.map(([label,value]) => `<div><dt>${e(label)}</dt><dd>${e(value)}</dd></div>`).join('')}</div><details class="io-secondary"><summary>查看全部个人条件</summary>${structuredReadingHtml(c)}${executionOriginalHtml(c,"个人条件的程序原值")}<p class="io-note">硬条件在采集后、评分前及候选池准入时分别核对。通过分数线不等于已经允许发送。</p></details>`;
}
export function scoringRuleHtml(rule) {
  if (!rule?.system) return '<div class="io-history-note">这条记录未附带当次评分模板。上方当前模板不能作为它当时的评分依据。</div>';
  return `<details class="io-secondary"><summary>本次实际采用的${rule.stage === 'single' ? '一次评分' : rule.stage === 'screen' ? '历史初筛' : '历史详评'}提示词 <small>${e((rule.sha || '').slice(0,12))}</small></summary><p class="io-note">以下是当次要求的阅读版，格式字段以中文说明；执行原文单独保留。</p>${promptReadingHtml(rule.system)}${rule.stage === 'single' ? `<details><summary>当次候选池线与硬条件</summary>${structuredReadingHtml({admit_min_score:rule.admit_min_score,conditions:rule.conditions})}</details>` : ''}<details><summary>本次资料如何组合</summary>${promptReadingHtml(rule.user_template || '')}${rule.job_template ? promptReadingHtml(rule.job_template) : ''}</details>${executionOriginalHtml(rule,'执行原文与程序格式')}</details>`;
}
export async function mountStageInputs(host, stage) {
  if (!host) return () => {};
  host.innerHTML = `<section class="card io-panel mb-16"><details data-io-shell ${stage === 'greeting' ? 'open' : ''}><summary class="io-summary"><span class="io-kicker">当前输入</span><span class="io-heading">${stage === 'matching' ? '评分模型会一起收到什么' : stage === 'greeting' ? '写稿输入 · 个人事实与写作规则' : e(names[stage])+'采用的模板与条件'}</span><span class="io-preview" data-io-preview>读取实际模板…</span></summary><div class="io-body"><div class="io-toolbar"><span class="io-meta" data-io-meta></span><div class="btn-row"><button class="btn sm" data-io-copy type="button" disabled>${stage === 'matching' ? '复制评分输入' : '复制当前输入'}</button><button class="btn sm ghost" data-io-refresh type="button">刷新</button></div></div><p class="io-note" data-io-notice></p><div data-io-content></div><p class="io-note" data-io-stamp role="status"></p></div></details></section>`;
  let disposed = false, busy = false, current = null, previous = '';
  const $ = selector => host.querySelector(selector);
  async function load() {
    if (disposed || busy) return;
    busy = true;
    try {
      const d = await api.get(`/api/ai/stage-inputs/${stage}`);
      if (d.ok === false) throw new Error(d.error || '输入读取失败');
      if (stage === 'greeting' && d.mode !== 'external') {
        try {
          const batch = await api.get('/api/workflow/prompt-preview');
          d.sections.push({title:'批量写稿 · 完整执行指令', text:batch.ok ? batch.instruction : `未读到：${batch.error || '批量写稿指令不可用'}`});
        } catch (err) { d.sections.push({title:'批量写稿 · 完整执行指令', text:`未读到：${err.message}`}); }
      }
      if (disposed) return;
      current = d;
      const signature = JSON.stringify(d);
      if (signature !== previous) {
        const open = new Set([...host.querySelectorAll('[data-io-section][open]')].map(el => el.dataset.ioSection));
        $('[data-io-preview]').textContent = ({matching:`候选人材料 ＋ 个人标准 ＋ 已保存条件 ＋ 岗位JD · ${d.candidate_material?.ready ? '材料已确认' : '材料未就绪，暂不评分'}`,greeting:'个人事实 ＋ 评分摘要与JD摘录 ＋ 写作规则 → 文案',interview:'所选岗位 ＋ 简历 ＋ 出题要求 → 面试题',resume_revision:'原简历 ＋ 目标与优化要求 → 优化稿'})[stage] || '查看当前采用的输入';
        $('[data-io-meta]').textContent = `${sources[d.source] || d.source || '来源未读到'}${d.versions?.plan ? ` · 方案 ${d.versions.plan}` : ''}${d.resume_id ? ` · 简历 ${d.resume_id}` : ''}`;
        $('[data-io-notice]').textContent = stage === 'matching' && d.mode !== 'external' ? '' : d.notice || '';
        $('[data-io-content]').innerHTML = (stage === 'matching' ? matchingStandardHtml(d) : stage === 'greeting' && d.mode !== 'external' ? greetingStandardHtml(d) : `<p class="io-explanation">${e(inputExplanation(d))}</p>${stage === 'greeting' ? `<details class="io-secondary"><summary>当前保存的个人事实 · ${(d.candidate_facts || []).length} 条</summary><p class="io-note">${e(d.material_note || '')}</p>${(d.candidate_facts || []).map(f=>`<p><strong>${e(f.id)}</strong> ${e(f.text)}</p>`).join('')}</details>` : ''}${d.resume_id ? '<p class="io-note">所选简历：见上方简历标识；<a href="#/resume">查看简历内容</a>。具体结果采用哪份简历，以当次记录为准。</p>' : ''}` + conditionsHtml(d) + readableSections(d).map(sectionHtml).join('')) + `<details class="io-secondary" data-io-section="technical"><summary>程序执行细节 · 完整提示词与输入格式</summary><p class="io-note">下列资料填充模板、输出格式、批量指令是程序执行细节，并非每段都需要单独定制。上面的阅读版依据实际模板排版，便于人工审查；这里保留原始字段、格式与文本，供用户 AI 核对。</p>${(d.sections || []).map(s => `<details class="io-section"><summary>${e(s.title)}</summary><pre class="io-text">${e(text(s.text))}</pre></details>`).join('')}</details>`;
        for (const section of host.querySelectorAll('[data-io-section]')) { if (previous) section.open = open.has(section.dataset.ioSection); }
        previous = signature;
      }
      $('[data-io-copy]').disabled = false;
      $('[data-io-stamp]').textContent = `本地读取于 ${new Date().toLocaleTimeString('zh-CN')} · 当前模板用于后续执行，已有结果以当次记录为准。`;
    } catch (err) {
      if (disposed) return;
      if (!current) $('[data-io-preview]').textContent = '当前输入未读到 · 展开查看原因';
      $('[data-io-stamp]').textContent = `读取失败：${err.message}。${current ? '上方保留的是此前读到的内容，可能已变化。' : '尚不能确认当前标准。新接口需要程序服务加载本次更新。'}`;
    } finally { busy = false; }
  }
  $('[data-io-refresh]').onclick = load;
  $('[data-io-copy]').onclick = async () => {
    if (!current) return;
    const body = stage === 'matching' ? `评分用候选人材料（${current.candidate_material?.ready ? '已确认' : '未确认'}）\n${current.candidate_material?.text || '未读取到'}\n\n当前评分标准\n${current.criteria?.text || (current.sections || []).slice(0,2).map(s=>s.title+'\n'+text(s.text)).join('\n\n')}\n\n已保存的岗位条件（同次加入）\n${JSON.stringify(current.conditions || {},null,2)}\n\n候选池分数线：${current.thresholds?.admit_min_score ?? '未设置'}` : `当前${names[stage]}输入\n${JSON.stringify(current, null, 2)}`;
    try { await navigator.clipboard.writeText(body); toast('已复制当前输入', 'ok'); }
    catch { const input = document.createElement('textarea'); input.className='input io-copy'; input.readOnly=true; input.value=body; modal({title:'当前输入 · 可手动复制',body:input}); input.select(); }
  };
  await load();
  const timer = setInterval(load,30000);
  return () => { disposed=true; clearInterval(timer); };
}
