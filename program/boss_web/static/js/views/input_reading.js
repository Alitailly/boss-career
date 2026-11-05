/** 本地阅读排版；不改执行文本，不调用模型，不推断缺失的个人偏好。 */
import { escapeHtml as e } from '../ui.js';

const labels = {
  conditions:'个人条件', mode:'设置状态', value:'取值', values:'选项',
  length:'篇幅要求', min_chars:'最少字符数', max_chars:'最多字符数', format:'结果格式',
  target:'求职目标', requirements:'具体要求', salary:'薪资要求', availability:'可工作安排',
  directions:'岗位方向', levels:'职级', employment_types:'岗位类型', cities:'城市', work_modes:'工作方式',
  accepts_relocation:'是否接受异地', industries:'行业', company_scales:'公司规模', degree_min:'最低学历',
  degree_block:'排除的学历要求', years_min:'工作年限底线', excluded_companies:'排除的公司',
  excluded_keywords:'排除的岗位内容', excluded_titles:'排除的岗位名称', hard_conflicts:'不能接受的条件',
  hard_conflict_phrases:'不能接受的要求', extra:'其他已保存条件',
  currency:'币种', period:'薪资周期', min:'下限', max:'上限', compare:'比较方式', unknown:'薪资不明时',
  days_per_month:'每月折算天数', months_per_year:'每年折算月数',
  start:'开始时间', end:'结束时间', days_per_week:'每周到岗天数', months:'持续月数', graduation:'毕业时间',
  school:'学校', degree:'学历', major:'专业', expected:'是否预计日期', org:'单位或项目', title:'名称或职位',
  kind:'经历类型', source:'资料来源', confidence:'确认状态', id:'编号', text:'内容',
  facts:'可引用的个人事实', education:'教育经历', experiences:'经历', skills:'技能', unknowns:'待核实事项',
  candidate_material:'评分用候选人材料', resume_brief:'候选人材料', resume_content:'简历内容',
  results:'逐岗评估结果', job_id:'岗位编号', encrypt_job_id:'岗位编号', score:'匹配分数', match_score:'匹配分数',
  decision:'岗位结论', verdict:'判断理由', pros:'主要匹配点', cons:'不足与待核实', advice:'补充说明',
  matched_skills:'匹配技能', missing_skills:'缺少的技能', jd_brief:'岗位摘要', hard_constraints:'岗位硬要求',
  greeting:'招呼语正文', reason:'选材依据', used_fact_ids:'引用的事实编号', ending_type:'结尾方式',
  title_for_message:'文案中的岗位称呼',
  blocked_reason:'无法生成的原因', status:'处理状态', jobs:'待处理岗位', company:'公司',
  jd_evidence:'岗位原文摘录', match_points:'匹配依据', draft:'待修改的文案', requirements_text:'写作要求',
  questions:'面试题目', question_text:'具体问题', question_type:'题型', difficulty:'难度',
  is_frequent:'是否为高频考点', tags:'考点标签', answer_hints:'回答提示', approach:'答题思路',
  key_points:'关键知识点', reference_answer:'参考回答',
  criteria:'个人评分标准', weights:'权重', weight:'权重', dimensions:'评分维度', name:'名称',
  instructions:'方法说明', prompt:'提示词要求', system:'执行要求', content:'内容',
  admit_min_score:'候选池分数线', stage2_min_score:'旧版详评门槛', confirmed:'是否已确认',
};
const values = {
  internship:'实习', full_time:'全职', part_time:'兼职', project:'项目', freelance:'自由职业',
  remote:'远程', onsite:'现场', hybrid:'混合办公', CNY:'人民币', day:'按天', month:'按月', year:'按年',
  overlap:'薪资区间有重叠即可', top:'岗位薪资上沿须在要求范围内',
  allow:'允许保留', block:'排除', reject:'未达标', match:'符合标准', uncertain:'待核实',
  unknown:'尚未确认', unset:'尚未设置', confirmed:'已确认', user:'用户提供', resume:'简历',
  draft:'草稿', blocked:'暂不能生成', succeeded:'已完成', needs_review:'待核实', failed:'失败',
  question:'提问式', statement:'陈述式', invite:'邀请式',
  custom_prompt:'个人定制方法', builtin:'基础模板', external_skill:'外部方法',
};
const variables = {
  resume_brief:'本次候选人材料', candidate_brief:'本次候选人材料', candidate_material:'本次确认的候选人材料',
  resume_content:'本次简历内容', resume_md:'本次简历原文', job_desc:'该岗位的正文', jobs_block:'本批岗位资料',
  job_name:'岗位名称', brand_name:'公司名称', job_id:'岗位编号', job_name_short:'岗位简称',
  conditions:'当前已确认的岗位条件', threshold:'当前候选池分数线', question_count:'本次题目数量',
  greeting_requirement:'本次写作要求', selected_sections:'选定的简历章节', optimization_prompt:'本次优化要求',
  brand_industry:'公司行业', brand_scale_name:'公司规模', location:'岗位地点', salary_desc:'岗位薪资',
  job_experience:'岗位经验要求', job_degree:'岗位学历要求', job_labels:'岗位标签', job_skills:'岗位技能要求',
  n:'本批岗位数量', count:'本批岗位数量', idx:'岗位序号', index:'岗位序号',
};

export function humanLabel(key) { return labels[key] || key; }
export function humanScalar(value) {
  if (value == null) return '未填写';
  if (typeof value === 'boolean') return value ? '是' : '否';
  return typeof value === 'string' ? (values[value] || value) : String(value);
}

/** 保留0、否、未知的区别；以中文标签阅读结构化值，不丢掉陌生字段。 */
export function structuredReadingHtml(value) {
  if (value == null || typeof value !== 'object') return `<span>${e(humanScalar(value))}</span>`;
  if (Array.isArray(value)) {
    if (!value.length) return '<span class="io-note">未列出项目</span>';
    return `<ul class="reading-list">${value.map(v=>`<li>${structuredReadingHtml(v)}</li>`).join('')}</ul>`;
  }
  // 有额外字段时照常展示，不能只取value而悄悄遗漏单位或其他约束。
  const keys=Object.keys(value);
  if (keys.every(k=>['mode','value','values'].includes(k)) && 'mode' in value) {
    const mode=value.mode;
    if (['unknown','unset'].includes(mode)) return `<span>${mode==='unknown' ? '尚未确认' : '尚未设置'}</span>`;
    if (['any','unlimited'].includes(mode)) return '<span>不限</span>';
    if (mode === 'value' && 'value' in value) return structuredReadingHtml(value.value);
    if (mode === 'list' && Array.isArray(value.values)) return structuredReadingHtml(value.values);
  }
  if (!keys.length) return '<span class="io-note">未设置</span>';
  if (keys.length === 1 && keys[0] === 'extra') return structuredReadingHtml(value.extra);
  return `<dl class="reading-fields">${Object.entries(value).map(([key,v])=>`<div><dt>${e(humanLabel(key))}</dt><dd>${structuredReadingHtml(v)}</dd></div>`).join('')}</dl>`;
}

function readingWords(value,translateFields=true) {
  let s=String(value);
  if (!translateFields) return s;
  s=s.replace(/\{\{?\s*([a-zA-Z_][\w.]*)\s*\}?\}/g,(whole,key)=>variables[key] ? `【${variables[key]}】` : whole);
  s=s.replace(/JSON\s*Schema/gi,'结果格式约定').replace(/JSON\s*对象/gi,'约定的结果内容').replace(/JSON/gi,'结构化结果');
  s=s.replace(/\b(true\s*\/\s*false)\b/g,'是或否');
  s=s.replace(/\bmatch\s*\/\s*reject\s*\/\s*uncertain\b/g,'符合标准／未达标／待核实');
  s=s.replace(/\bstatus\s*=\s*(draft|blocked)\b/g,(_,state)=>'处理状态为'+values[state]);
  // 自然语言里的普通英文词不按字段翻译；格式字段仅取明确的程序用词。
  s=s.replace(/\b([a-zA-Z_][\w]*)\b/g,(word)=>labels[word] &&
    (word.includes('_') || /^(pros|cons|advice|facts|jobs|draft|decision|verdict|unknowns|score|greeting|status|id)$/.test(word)) ? labels[word] : word);
  return s;
}

function proseHtml(value,translateFields=true) {
  return String(value).split(/\n\s*\n/).filter(s=>s.trim()).map(part=>{
    const lines=part.trim().split('\n');
    if (lines.every(line=>/^\s*[-*]\s+/.test(line))) {
      return `<ul class="reading-list">${lines.map(line=>`<li>${e(readingWords(line.replace(/^\s*[-*]\s+/,''),translateFields))}</li>`).join('')}</ul>`;
    }
    return `<p class="reading-paragraph">${e(readingWords(part.trim(),translateFields))}</p>`;
  }).join('');
}

function parseJson(value) {
  try { return {ok:true,value:JSON.parse(value)}; } catch {}
  // Python format模板双花括号；只用于阅读，不写回源文本。
  if (value.includes('{{')) {
    try { return {ok:true,value:JSON.parse(value.replace(/\{\{/g,'{').replace(/\}\}/g,'}'))}; } catch {}
  }
  return {ok:false};
}

function schemaReadingHtml(schema) {
  if (!schema || typeof schema !== 'object') return structuredReadingHtml(schema);
  if (schema.properties && typeof schema.properties === 'object') {
    const required=Array.isArray(schema.required) ? schema.required : [];
    return `<dl class="reading-fields">${Object.entries(schema.properties).map(([key,field])=>`<div><dt>${e(humanLabel(key))}<small class="reading-optional">${required.includes(key) ? '需提供' : '可选'}</small></dt><dd>${schemaReadingHtml(field)}</dd></div>`).join('')}</dl>`;
  }
  if (schema.type === 'array') return `<p class="reading-paragraph">逐条列出${schema.maxItems != null ? `，最多 ${e(schema.maxItems)} 项` : ''}。</p>${schema.items ? schemaReadingHtml(schema.items) : ''}`;
  const parts=[];
  if (schema.description) parts.push(e(schema.description));
  if (Array.isArray(schema.enum)) parts.push('可选：'+schema.enum.map(v=>e(humanScalar(v))).join('、'));
  else if (schema.type) parts.push(e(({string:'文字说明',integer:'整数',number:'数值',boolean:'是或否',object:'分项说明'})[schema.type] || schema.type));
  if (schema.minLength != null) parts.push(`至少 ${e(schema.minLength)} 字符`);
  if (schema.maxLength != null) parts.push(`最多 ${e(schema.maxLength)} 字符`);
  if (schema.minimum != null) parts.push(`不低于 ${e(schema.minimum)}`);
  if (schema.maximum != null) parts.push(`不高于 ${e(schema.maximum)}`);
  return parts.length ? `<p class="reading-paragraph">${parts.join('；')}</p>` : structuredReadingHtml(schema);
}

function formatBlockContent(body, isFormat) {
  const parsed=parseJson(body);
  const title=isFormat ? '程序约定的返回内容 · 这是格式示例，不是实际结果' : '已保存的内容';
  if (parsed.ok) {
    const schema=isFormat && parsed.value && typeof parsed.value === 'object' && parsed.value.type && (parsed.value.properties || parsed.value.items);
    return `<section class="reading-structure"><p class="reading-caption">${schema ? '程序约定的返回内容' : title}</p>${schema ? schemaReadingHtml(parsed.value) : structuredReadingHtml(parsed.value)}</section>`;
  }
  // 面试格式中含true/false、0–100等说明，并非合法JSON。保留每个字段和说明。
  const rows=[...body.matchAll(/"([^"\n]+)"\s*:\s*([^\n]*)/g)];
  if (rows.length && isFormat) {
    return `<section class="reading-structure"><p class="reading-caption">程序约定的返回内容</p><dl class="reading-fields">${rows.map(([,key,rhs])=>{
      const clean=rhs.replace(/[,\s]+$/,'');
      const parsedValue=parseJson(clean);
      const explanation=/^[\[{]\s*$/.test(clean) ? '包含以下信息' : clean.replace(/^"(.*)"$/,'$1');
      return `<div><dt>${e(humanLabel(key))}</dt><dd>${parsedValue.ok ? structuredReadingHtml(parsedValue.value) : proseHtml(explanation)}</dd></div>`;
    }).join('')}</dl></section>`;
  }
  return '<p class="reading-unconverted">此段使用了暂不能完整转换的程序格式，请在“执行原文”中核对；未将其省略为已知规则。</p>';
}

function formatBlockHtml(body, isFormat) {
  const content=formatBlockContent(body,isFormat);
  return isFormat ? `<details class="reading-format"><summary>程序返回哪些信息</summary>${content}</details>` : content;
}

function jsonEnd(source,start) {
  let depth=0, quoted=false, escaped=false;
  for(let i=start;i<source.length;i++) {
    const ch=source[i];
    if(quoted) { if(escaped)escaped=false; else if(ch==='\\')escaped=true; else if(ch==='"')quoted=false; continue; }
    if(ch==='"')quoted=true;
    else if(ch==='{' || ch==='[')depth++;
    else if(ch==='}' || ch===']') { if(--depth===0)return i+1; }
  }
  return -1;
}

/** 阅读版按原文排版并翻译格式字段；可随时对照另外保存的执行原文。 */
export function promptReadingHtml(value,{translateFields=true}={}) {
  if (typeof value !== 'string') return structuredReadingHtml(value);
  const source=value.replace(/\r\n/g,'\n');
  let out='', prose='', i=0, heading='';
  function flush() {
    const body=!translateFields && /待确认|待核实|尚未确认/.test(heading)
      ? prose.split('\n').map(line=>labels[line.trim()] || line).join('\n') : prose;
    out+=proseHtml(body,translateFields);prose='';
  }
  while(i<source.length) {
    if ((i===0 || source[i-1]==='\n') && /^#{1,4}\s/.test(source.slice(i))) {
      flush(); const end=source.indexOf('\n',i), next=end<0?source.length:end;
      heading=source.slice(i,next).replace(/^#{1,4}\s+/,'');
      out+=`<h5 class="reading-heading">${e(readingWords(heading,translateFields))}</h5>`;
      i=next+1;continue;
    }
    if (source.startsWith('```',i)) {
      const first=source.indexOf('\n',i), last=source.indexOf('```',first+1);
      if(first>=0 && last>=0) {
        flush(); const body=source.slice(first+1,last).trim();
        out+=formatBlockHtml(body,/json|输出|返回|格式|schema/i.test(source.slice(Math.max(0,i-100),first)));
        i=last+3;continue;
      }
    }
    const ch=source[i];
    if ((ch==='{' || ch==='[') && /^[\[{]\s*(?:[\[{"\d]|\]|\})/.test(source.slice(i))) {
      const end=jsonEnd(source,i);
      if(end>i) {
        const before=source.slice(Math.max(0,i-100),i);
        const body=source.slice(i,end);
        const isFormat=/输出|返回|格式|schema/i.test(before);
        flush();out+=formatBlockHtml(body,isFormat);i=end;continue;
      }
    }
    prose+=ch;i++;
  }
  flush();return out || '<p class="io-note">尚无内容</p>';
}

export function executionOriginalHtml(value, title='执行原文与程序格式') {
  const raw=typeof value==='string' ? value : JSON.stringify(value,null,2);
  return `<details class="io-secondary reading-original"><summary>${e(title)}</summary><p class="io-note">这里保留程序实际采用的原文，供用户 AI 或开发排查；阅读版不会改写这些内容。</p><pre class="io-text">${e(raw ?? '')}</pre></details>`;
}
