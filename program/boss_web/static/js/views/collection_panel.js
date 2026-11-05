/** 人读目标和新增变化；执行记录交给用户自己的 AI。此面板只读，不启动任务。 */
import { api } from '../api.js';
import { escapeHtml as e, toast } from '../ui.js';

const knownEmployment = { internship: '实习', campus: '校招', campus_recruitment: '校招', full_time: '全职', fulltime: '全职', part_time: '兼职' };
// Read-only labels from the program's basic filter table; loaded platform names win.
const referenceFilters = {
  degree: { '0': '不限', '209': '初中及以下', '208': '中专/中技', '206': '高中', '202': '大专', '203': '本科', '204': '硕士', '205': '博士' },
  experience: { '0': '不限', '108': '在校生', '102': '应届生', '101': '经验不限', '103': '1年以内', '104': '1-3年', '105': '3-5年', '106': '5-10年', '107': '10年以上' }
};
const list = value => Array.isArray(value) ? value : value?.mode === 'list' ? value.values || [] : [];
const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
const shown = value => value === null || value === undefined || value === '' ? '未记录' : String(value);
const platformLabels = {city:'城市',position:'职位类别',multiBusinessDistrict:'区／商圈',multiSubway:'地铁线路／站点',jobType:'平台岗位类型',salary:'平台薪资',industry:'行业',scale:'公司规模',degree:'岗位学历',experience:'岗位经验',stage:'融资阶段',payType:'结算方式',partTime:'兼职时段'};

function choices(raw, out = new Map()) {
  if (Array.isArray(raw)) raw.forEach(item => choices(item, out));
  else if (raw && typeof raw === 'object') {
    if (raw.code !== undefined && raw.name) out.set(String(raw.code), String(raw.name));
    Object.values(raw).filter(value => value && typeof value === 'object').forEach(value => choices(value, out));
  }
  return out;
}

// Preserve catalog hierarchy: cities by province, industries by broad category.
function catalogHtml(raw) {
  const items = Array.isArray(raw) ? raw : raw && typeof raw === 'object' ? Object.values(raw).filter(x => x && typeof x === 'object') : [];
  const children = x => x.children || x.options || [];
  const groups = items.filter(x => Array.isArray(children(x)) && children(x).length);
  const leaves = items.filter(x => x?.name && !children(x).length);
  return `${leaves.length ? `<div class="wc-option-grid">${leaves.map(x=>`<span>${e(x.name)}</span>`).join('')}</div>` : ''}${groups.length ? `<div class="wc-option-groups">${groups.map(x=>`<details class="wc-option-group" data-wc-open="group-${e(x.code || x.name)}"><summary>${e(x.name)} <small>${children(x).length} 项</small></summary>${catalogHtml(children(x))}</details>`).join('')}</div>` : ''}`;
}

function condition(value, dictionary = new Map()) {
  if (value?.mode === 'unknown') return '未设置';
  if (value?.mode === 'any') return '不限';
  const values = list(value);
  return values.length ? values.map(x => dictionary.get(String(x)) || (/^\d+$/.test(String(x)) ? `名称待加载（代码${x}）` : String(x))).join('、') : '未设置';
}

function salaryText(salary) {
  if (!salary || (salary.min == null && salary.max == null)) return '未设置';
  const bounds = salary.min != null && salary.max != null ? `${salary.min}–${salary.max}`
    : salary.min != null ? `${salary.min}起` : `${salary.max}以内`;
  return `${bounds}元/${({ day: '天', month: '月', year: '年', hour: '小时' })[salary.period] || salary.period || '口径未设置'}${salary.compare === 'overlap' ? '（区间有重叠即可）' : ''}`;
}

function dailyModel(data) {
  const m = data?.metrics || {}, c = m.collector || {};
  const days = Array.isArray(m.by_day) ? m.by_day.slice(-7) : [];
  const today = days.find(day => day.date === m.today);
  const todayJD = number(today?.jd_first);
  const todayNew = number(today?.new_jobs);
  const nonzeroDays = days.filter(day => number(day.new_jobs) > 0).length;
  const pending = m.new_jobs != null && m.new_with_jd != null ? Math.max(0, Number(m.new_jobs) - Number(m.new_with_jd)) : null;
  let title = '正在读取采集记录', text = '', tone = 'info';
  if (data?.ok === false || !data?.metrics) {
    title = '采集记录暂时读取不到'; text = data?.error || '请刷新或把读取问题交给 AI 检查；此时不能按新增0条解释。';
  } else if (!c.last_at) {
    title = '还没有可靠的采集状态记录'; text = '先核实任务是否启动；新增少不一定是搜索范围的问题。';
  } else if (c.running !== true) {
    title = '采集器当前未运行'; text = '下方保留已有新增记录。先检查任务状态，不能只凭新增为0就判断没有岗位。';
  } else if (todayNew === null) {
    title = '今天新增了多少个不重复岗位，尚未记录'; text = '目前不能按新增0个解释，先让 AI 核对采集记录。';
  } else if (todayNew === 0) {
    title = todayJD > 0 ? '今天在补齐已有岗位的全文，尚无新岗位入库' : '采集还在运行，今天还没有新增不重复岗位';
    text = '先看最近几天是否一直没有新岗位，再让 AI 检查是否在反复搜索相同范围。'; tone = 'warn';
  } else if (pending > 0 && Number(m.new_with_jd) === 0) {
    title = `今天新增了${todayNew}个不重复岗位，近期新岗位的全文还需补齐`;
    text = `过去${m.window_hours || 24}小时的新岗位已有入库记录，但尚未拿到详细职责和要求；可让 AI 检查全文获取进度。`; tone = 'warn';
  } else if (nonzeroDays <= 1) {
    title = '已有新增，先看看岗位是否符合你的方向'; text = '目前只有一天记录到新增，先积累趋势，不据此保证后续一直有同样产出。';
  } else {
    const last = days.filter(day => day.date < m.today).slice(-3).map(day => number(day.new_jobs));
    const decreasing = last.length === 3 && last.every(v => v !== null) && last[0] > last[1] && last[1] > last[2];
    title = decreasing ? '最近几天新增的不重复岗位正在减少' : '今天已有新的不重复岗位入库';
    text = decreasing ? '这是需要留意的变化，先检查覆盖和执行记录，不直接认定搜索范围已耗尽。' : '可结合岗位内容判断是否继续调整；执行节奏与覆盖交给 AI 检查。';
    tone = decreasing ? 'warn' : 'info';
  }
  return { m, c, days, todayJD, todayNew, pending, title, text, tone };
}

export async function mountCollectionPanel(host) {
  let disposed = false, busy = false, digest = null, context = null, metadata = {}, recipe = null, platform = null;
  let selectedDay = '', requestText = '', proposedChanges = '', readErrors = [];
  host.classList.add('wf-collection');
  host.innerHTML = `
    <div class="wc-grid">
      <section class="card wc-intent">
        <div class="card-head"><h3 class="card-title">当前采集输入</h3><span class="pill static">已采用条件</span></div>
        <div data-wc-conditions><p class="muted">读取当前生效条件…</p></div>
      </section>
      <section class="card wc-results">
        <div class="card-head"><h3 class="card-title">还在找到新岗位吗？</h3><button class="btn sm ghost" data-wc-refresh>刷新</button></div>
        <div data-wc-results><p class="muted">读取新增记录…</p></div>
        <div class="wc-actions"><button class="btn sm" data-wc-feedback>查看与复制采集记录</button><a class="btn sm" href="#/jobs">查看岗位</a></div>
      </section>
    </div>
    <section class="card wc-feedback" data-wc-feedback-box hidden>
      <div class="card-head"><h3 class="card-title">当前采集记录</h3><button class="btn sm ghost" data-wc-close>收起</button></div>
      <label class="field">我的观察（可留空）<textarea data-wc-question-text rows="3" placeholder="填写你看到的情况或具体岗位"></textarea></label>
      <p class="muted wc-help" data-wc-request-note>自动附上当前条件、检索词、趋势和已有执行记录；缺失数据会标明。</p>
      <details data-wc-open="summary"><summary>查看将附给 AI 的记录</summary><pre class="wc-summary" data-wc-summary></pre></details>
      <div class="wc-actions"><button class="btn primary" data-wc-copy>复制问题与采集记录</button><span class="muted wc-help" data-wc-copy-status role="status">复制后粘贴到你常用的 AI 对话。</span></div>
    </section>
    <details class="card wc-diagnostics" data-wc-open="diagnostics"><summary>AI执行详情与记录 · 可查看，无需自己调参数</summary><div data-wc-diagnostics></div></details>`;
  const $ = selector => host.querySelector(selector);

  const names = key => new Map([...Object.entries(referenceFilters[key] || {}), ...choices(metadata[key])]);
  function entryFields() {
    const base = context?.effective?.search?.base || {};
    const partTime = String(base.jobType) === '1903' || names('jobType').get(String(base.jobType)) === '兼职';
    const profile = partTime ? 'part_time' : 'regular';
    return metadata.selection?.profiles?.[profile]?.fields ||
      ['city','position','multiBusinessDistrict','multiSubway','jobType','industry','scale','degree','experience',...(partTime ? ['payType','partTime'] : ['salary','stage'])];
  }
  const missingOptions = () => entryFields().filter(key => !['multiBusinessDistrict','multiSubway'].includes(key)).some(key => !choices(metadata[key]).size);

  function platformConditions() {
    const effective = context?.effective || {}, search = effective.search || {}, target = effective.target || {};
    const bases = search.bases?.length ? search.bases.map(base => ({...search.base,...base})) : [search.base || {}];
    const receipt = new Map((search.audit?.rows || []).map(row => [row.field,row]));
    const enabled = entryFields();
    const aliases = {city:'cities',jobType:'employment_types',industry:'industries',scale:'company_scales'};
    return Object.keys(platformLabels).map(field => {
      const row = receipt.get(field);
      const codes = [...new Set(bases.flatMap(base => Array.isArray(base[field]) ? base[field] : [base[field]])
        .filter(value => value !== undefined && value !== null && value !== '').map(String))];
      const state = row?.state || (codes.length ? codes.every(code => code === '0') ? '已明确不限' : '已配置平台条件'
        : target[aliases[field]]?.mode === 'any' ? '已明确不限' : '未限制，尚无明确选择记录');
      const labels = codes.map(code => names(field).get(code) ||
        row?.names?.[row.codes?.map(String).indexOf(code)] || (/^\d+$/.test(code) ? `名称待加载（代码${code}）` : code));
      let value = ['multiBusinessDistrict','multiSubway'].includes(field) && row?.names?.length ? row.names.join('；') : labels.join('、'), badge = state, tone = 'set';
      if (!enabled.includes(field)) {
        if (!codes.length) return null;
        badge = '与当前入口不符，需核对'; tone = 'warn';
      } else if (state === '已明确不限') {
        value = codes.length ? '不限' : '未传参数，采用平台默认范围';
        badge = codes.length ? '已明确不限' : '用户已选不限'; tone = 'neutral';
      } else if (state.startsWith('采集后判断')) {
        value = field === 'salary' ? '未设置平台薪资档位' : '平台无对应单选'; badge = '采集后判断'; tone = 'neutral';
      } else if (!codes.length) {
        value = '平台采用默认范围'; badge = '未设置'; tone = 'unset';
      } else {
        badge = '平台已设置';
      }
      return {field,label:platformLabels[field],codes,value,badge,tone,state};
    }).filter(Boolean);
  }

  function currentConditions() {
    const effective = context?.effective || {}, target = effective.target || {};
    const cities = condition(target.cities, names('city'));
    const employment = condition(target.employment_types, new Map(Object.entries(knownEmployment)));
    return [
      ['期望城市', cities],
      ['招聘范围', employment], ['薪资偏好', salaryText(effective.salary)],
    ];
  }
  function keywords() { return list(context?.effective?.search?.keywords); }
  function summaryText() {
    const { m } = dailyModel(digest);
    return [
      '【当前采集记录】', requestText ? '我的观察：' + requestText : '',
      proposedChanges ? '【我提出的修改要求，尚未生效】\n' + proposedChanges : '',
      '【个人求职偏好】\n' + (context ? currentConditions().map(([k, v]) => `${k}：${v}`).join('\n') : '当前条件读取失败，尚不可核实'),
      '【实际采集条件（当前方案）】\n' + (context ? platformConditions().map(row => `${row.label}：${row.value}；${row.badge}`).join('\n') : '尚不可核实'),
      '这些条件来自当前方案的编译结果，不代表在运行的旧任务已切换版本；旧任务按启动快照执行。未设置不等于用户明确不限。',
      '启用检索词：' + (keywords().join('、') || '未记录'),
      '【统计范围】全库；不是单个城市、关键词或方案的独立效果。\n数据截至：' + shown(m.as_of),
      '【主要看新增不重复岗位】数据库原来没有、当天首次入库的岗位；已有岗位再次采到不算新增。\n' + ((m.by_day || []).map(d => `${d.date}：新增不重复岗位${shown(d.new_jobs)}个；首次拿到全文${shown(d.jd_first)}个`).join('\n') || '按日新增未记录'),
      '首次拿到全文可能发生在岗位入库几天之后，包含之前已入库的岗位；补齐全文不会增加岗位数量。两项不能相减来计算待补全文数。',
      '【已有执行记录，供AI检查】\n' + JSON.stringify({
        active_plan: context?.active?.plan || null, effective: context?.effective || null,
        metrics: digest?.metrics || null, coverage: digest?.coverage || null,
        execution_reference: recipe?.collector || null, events: (digest?.events || []).slice(0, 5),
        platform_control: platform,
        missing_sources: readErrors, keyword_yield: '当前接口未记录；不能由总量反推',
        last_new_jd_at: '当前摘要未提供，不用心跳时间代替',
        running_duration: '当前摘要未提供，不从心跳推算'
      }, null, 2)
    ].filter(Boolean).join('\n\n');
  }
  function updateSummary() {
    $('[data-wc-summary]').textContent = summaryText();
    $('[data-wc-request-note]').textContent = proposedChanges
      ? '已附上你的修改要求，当前实际条件尚未改变。让 AI 完成调整后，刷新查看生效结果。'
      : '自动附上当前条件、检索词、趋势和已有执行记录；缺失数据会标明。';
  }
  function openFeedback(text) {
    if (text) requestText = text;
    $('[data-wc-feedback-box]').hidden = false;
    $('[data-wc-question-text]').value = requestText;
    updateSummary();
    $('[data-wc-feedback-box]').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  function renderConditions() {
    const el = $('[data-wc-conditions]');
    if (!context) { el.innerHTML = '<div class="banner info">当前条件暂未读到。不会用旧配置冒充当前生效方案；可刷新或让 AI 检查。</div>'; return; }
    const plan = context.active?.plan;
    const words = keywords();
    const cov = digest?.coverage || {};
    const recorded = (cov.keywords || []).filter(k => k.executed).length;
    el.innerHTML = `
      <p class="muted wc-help">${plan ? `${e(plan.label || '当前方案')} · 版本 ${e(shown(plan.revision))}` : '尚未启用个人方案'}</p>
      ${missingOptions() ? '<p class="muted wc-help">部分平台选项尚未加载，学历和经验先显示程序参考名称。可先告诉 AI 你的要求；完整选项加载后再核对，不影响已保存条件。</p>' : ''}
      <dl class="wc-condition-list">${currentConditions().map(([k, v]) => `<dt>${e(k)}</dt><dd>${v === '未设置' ? '<span class="wc-unconfirmed">尚未确认</span>' : e(v)}</dd>`).join('')}</dl>
      ${currentConditions().some(([, v]) => v === '未设置') ? '<p class="wc-condition-note">标记为“尚未确认”的条件，不能当作你已选择“不限”。可交给 AI 一起核对。</p>' : ''}
      <div class="wc-platform-head"><h4>实际采集条件</h4><span class="muted">来自当前方案</span></div>
      <div class="wc-platform-grid">${platformConditions().map(row => `<div class="wc-platform-item"><span class="wc-platform-label">${e(row.label)}<small class="wc-filter-state wc-filter-${row.tone}">${e(row.badge)}</small></span><strong>${e(row.value)}</strong></div>`).join('')}</div>
      <p class="wc-employment-note"><strong>“实习”在哪里？</strong> 招聘范围中的“实习”是程序保存的个人求职要求。${[...names('jobType').values()].some(name => ['实习','实习生'].includes(name)) ? '当前平台目录也提供实习类型，可查看下方平台岗位类型选项；是否已启用，以上方实际采集条件为准。' : '当前本地电脑网页目录的岗位类型只有'+e([...names('jobType').values()].join('、') || '尚未加载的选项')+'，未提供实习专选；不能据此声称平台已只搜实习。“在校生”是经验选项，不等于实习类型。实习性质仍需结合关键词、岗位全文及程序个人条件判断。'}</p>
      <p class="wc-condition-note">未设置 ≠ 已确认不限。个人薪资偏好与平台薪资档位分开设置。</p>
      <details data-wc-open="filter-receipt"><summary>查看生效范围与平台选项来源</summary><p class="muted wc-help">空字段不会传给平台。日薪/月薪不自动换算；“日结”是结算方式，不是日薪金额。这是当前方案编译出的条件；新任务与采集器下一轮使用，运行中的旧任务按启动快照执行。${Number(context.effective?.search?.audit?.city_batches) > 1 ? `当前按${Number(context.effective.search.audit.city_batches)}个城市分别检索。` : ''} 修改条件不会重新筛选已有库中的岗位。</p><p class="muted wc-help">${e(metadata.catalog_note || context.effective?.search?.audit?.catalog_note || '选项来自程序本地目录；查看面板不刷新平台选项。')}</p></details>
      <div class="wc-key-head"><h4>搜索关键词</h4><span class="muted">已启用 ${words.length} 个</span></div>
      <div class="wc-chips">${words.slice(0, 24).map(k => `<span class="chip">${e(k)}</span>`).join('') || '<span class="muted">尚未设置检索词</span>'}</div>
      ${words.length > 24 ? `<details data-wc-open="more-words"><summary>还有 ${words.length - 24} 个关键词</summary><div class="wc-chips">${words.slice(24).map(k => `<span class="chip">${e(k)}</span>`).join('')}</div></details>` : ''}
      <details data-wc-open="keywords"><summary>平台支持的筛选条件（参考）</summary>
        <p class="muted wc-help">候选词库未单独记录；可以告诉 AI 希望探索的相邻方向。${recorded ? `覆盖记录中有${recorded}个词留下执行记录，详情供AI核实。` : ''}</p>
        ${entryFields().map(k => { const map = choices(metadata[k]); return map.size ? `<details data-wc-open="catalog-${e(k)}"><summary>${e(platformLabels[k])}选项</summary><div class="wc-catalog-body">${catalogHtml(metadata[k])}</div></details>` : ''; }).join('')}
        <p class="muted wc-help">职位类别与关键词是两个筛选维度，类别选得过窄可能遗漏相关岗位。区／商圈、地铁选项按城市保存；查看页面不会向平台发请求。</p>
        ${(metadata.locationNotes || []).map(note => `<p class="muted wc-help">${e(note)}</p>`).join('')}
      </details><p class="muted wc-help">个人条件由你决定；搜索深度、间隔和批次交给 AI 检查。</p>`;
  }
  function renderResults() {
    const d = dailyModel(digest), { m, c, days, todayJD, todayNew } = d;
    const paused = platform?.account_paused ? ['账号范围的平台操作'] : ['search','details'].filter(scope => platform?.paused_scopes?.[scope]).map(scope => ({search:'岗位搜索',details:'岗位全文读取'})[scope]);
    if (paused.length) {
      d.title = `${paused.join('、')}已暂停`;
      d.text = '采集进程运行不等于平台请求仍在执行。先让 AI 核对暂停与已有执行记录，再决定后续操作；刷新不会解除暂停。';
      d.tone = 'warn';
    }
    const max = Math.max(1, ...days.flatMap(day => [number(day.jd_first) || 0, number(day.new_jobs) || 0]));
    if (!days.some(day => day.date === selectedDay)) selectedDay = days[days.length - 1]?.date || '';
    $('[data-wc-results]').innerHTML = `
      <div class="wc-daily-stats">
        <div class="wc-stat"><strong>${todayNew === null ? '—' : todayNew}</strong><div>今日新增不重复岗位<small>数据库原来没有，今天首次存入；已有岗位再次采到不计入</small></div></div>
        <div class="wc-stat wc-stat-completion"><strong>${todayJD === null ? '—' : todayJD}</strong><div>今日首次拿到全文<small>首次拿到详细职责和要求；其中可能有之前已入库的岗位</small></div></div>
      </div>
      <div class="banner ${d.tone} wc-signal"><strong>${e(d.title)}</strong><p>${e(d.text)}</p></div>
      <div class="wc-chart-head"><span>最近7天 · 新岗位与全文获取（个）</span><span>所有采集任务汇总 · 今天尚未结束</span></div>
      <div class="wc-chart-legend"><span><i class="wc-legend-new"></i>新增不重复岗位</span><span><i class="wc-legend-jd"></i>首次拿到全文</span></div>
      <div class="wc-trend" role="group" aria-label="最近7天新增不重复岗位与首次拿到全文的数量">${days.map(day => { const n = number(day.jd_first), discovered = number(day.new_jobs); return `<button class="wc-day" type="button" data-wc-day="${e(day.date)}" aria-pressed="${day.date === selectedDay}" aria-label="${e(day.date)}，新增不重复岗位 ${discovered === null ? '未记录' : discovered}个，首次拿到全文 ${n === null ? '未记录' : n}个"><span class="wc-bar-values"><span>${discovered === null ? '—' : discovered}</span><span>${n === null ? '—' : n}</span></span><span class="wc-bar-area"><span class="wc-bar wc-bar-new" style="height:${discovered === null ? 0 : discovered / max * 100}%"></span><span class="wc-bar" style="height:${n === null ? 0 : n / max * 100}%"></span></span><span class="muted">${e(String(day.date).slice(5).replace('-', '/'))}</span></button>`; }).join('') || '<p class="muted">暂无按日记录</p>'}</div>
      <p class="muted wc-help" data-wc-day-detail></p>
      <p class="muted wc-help">数据截至 ${e(shown(m.as_of))} · ${c.running === true ? '采集进程运行中' : c.last_at ? '采集进程当前未运行' : '运行状态待核实'}${paused.length ? ' · 平台采集受暂停限制' : platform ? '' : ' · 平台控制状态未读到'}</p>
      <details data-wc-open="definition"><summary>怎样区分“新增岗位”和“拿到全文”？</summary>
        <p class="muted wc-help"><strong>新增岗位：数据库里多了一个原来没有的岗位。</strong>已经存过的岗位再次采到，不算新增。</p>
        <p class="muted wc-help"><strong>拿到全文：第一次取得某个岗位的详细职责、任职要求。</strong>这可能是今天新增的岗位，也可能是之前已入库、今天才补齐全文的岗位。</p>
        <p class="muted wc-help">例如：昨天已存入一个岗位，今天才拿到它的全文。今天“新增不重复岗位”不增加，“首次拿到全文”增加1个；数据库里仍然只有这一个岗位。</p>
        <p class="muted wc-help">重点看新增不重复岗位有多少。全文获取是另一项进度，不能用这两个数字相减来计算还缺多少份全文。</p>
      </details>
      <details data-wc-open="scope"><summary>统计范围和日期说明</summary>
        <p class="muted wc-help">范围：汇总所有采集任务，不是只统计当前条件中的城市或某个关键词的结果。</p>
        <p class="muted wc-help">日期：新增岗位记在首次入库那天，全文记在首次拿到那天。同一岗位可能在不同日期分别被计入两项，不代表重复入库。</p>
        <p class="muted wc-help">未记录：显示“—”或“未记录”时，表示缺少数据；即使某天新增为0，也要先核实那天是否运行过采集。</p>
        <p class="muted wc-help">今天还没结束，不能直接用今天半天的数量判断是否比昨天全天变差。</p>
      </details>`;
    renderSelectedDay();
  }
  function renderSelectedDay() {
    const day = (digest?.metrics?.by_day || []).find(d => d.date === selectedDay);
    $('[data-wc-day-detail]').textContent = day ? `${day.date}：新增不重复岗位${shown(day.new_jobs)}个 · 首次拿到全文${shown(day.jd_first)}个（含之前入库的岗位）` : '无按日记录。';
    host.querySelectorAll('[data-wc-day]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.wcDay === selectedDay)));
  }
  function renderDiagnostics() {
    const m = digest?.metrics || {}, cov = digest?.coverage || {};
    const params = recipe?.collector?.params || [];
    $('[data-wc-diagnostics]').innerHTML = `
      <p class="muted wc-help">过去${e(shown(m.window_hours))}小时：搜索结果中出现岗位${e(shown(m.raw))}次，重复出现${e(shown(m.duplicates))}次；实际新增不重复岗位${e(shown(m.new_jobs))}个，其中${e(shown(m.new_with_jd))}个已拿到全文。重复出现次数不等于数据库里多存了这么多条；这个时间范围也不等于今天。</p>
      <p class="muted wc-help">最后状态更新：${e(shown(m.collector?.last_at))}。这是心跳时间，不是上次新增时间；实际运行时长未记录。</p>
      <p class="muted wc-help">${e(cov.city_limit || '')} ${e(cov.note || '')}</p>
      ${params.length ? `<details data-wc-open="params"><summary>执行参数（只读记录）</summary><div class="data-table-wrap"><table class="data-table"><thead><tr><th>设置</th><th>兼容配置参考值</th><th>作用</th></tr></thead><tbody>${params.map(p => `<tr><td>${e(p.label || p.key)}</td><td>${e(shown(p.value))} ${e(p.unit || '')}</td><td>${e(p.hint || '')}</td></tr>`).join('')}</tbody></table></div><p class="muted wc-help">兼容配置不等于当前任务的完整执行快照。参考间隔不能保证新增效果或规避风控。</p></details>` : ''}
      ${(cov.keywords || []).length ? `<details data-wc-open="coverage"><summary>已记录的搜索覆盖</summary><div class="data-table-wrap"><table class="data-table"><thead><tr><th>关键词</th><th>启用</th><th>深度记录</th></tr></thead><tbody>${cov.keywords.map(k => `<tr><td>${e(k.keyword)}</td><td>${k.enabled ? '是' : '历史词'}</td><td>${e(shown(k.depth))}页${k.exhausted ? ' · 当前范围已翻完' : ''}</td></tr>`).join('')}</tbody></table></div><p class="muted wc-help">搜索位置以已有记录为准；逐词新增没有记录，不能把全库新增分摊到各词。</p></details>` : ''}
      ${(digest?.events || []).length ? `<details data-wc-open="events"><summary>最近诊断事件</summary><pre class="wc-summary">${e(JSON.stringify(digest.events.slice(0, 5), null, 2))}</pre></details>` : ''}
      ${readErrors.length ? `<p class="muted wc-help">读取不完整：${e(readErrors.join('；'))}</p>` : ''}`;
  }
  async function refresh() {
    if (disposed || busy) return;
    busy = true;
    try {
      const results = await Promise.allSettled([api.get('/api/collection/digest?hours=24'), api.get('/api/ai/context'), api.get('/api/filters/conditions'), api.get('/api/workflow/control/recipe'), api.get('/api/platform/control')]);
      if (disposed) return;
      const names = ['采集记录', '当前生效条件', '筛选选项', '执行参考配置', '平台操作状态'];
      readErrors = results.flatMap((r, i) => r.status === 'rejected' ? [`${names[i]}：${r.reason?.message || '读取失败'}`] : []);
      digest = results[0].status === 'fulfilled' ? results[0].value : { ok: false, error: results[0].reason?.message };
      context = results[1].status === 'fulfilled' ? results[1].value : null;
      metadata = results[2].status === 'fulfilled' ? results[2].value : {};
      const search = context?.effective?.search || {};
      const cities = [...new Set((search.bases?.length ? search.bases : [search.base || {}]).map(b => b.city).filter(c => c && c !== '100010000'))];
      const localLocations = await Promise.allSettled(cities.map(city => api.get(`/api/filters/locations?city=${encodeURIComponent(city)}`)));
      if (disposed) return;
      metadata.locationNotes = [];
      for (const key of ['multiBusinessDistrict','multiSubway']) {
        metadata[key] = localLocations.flatMap((r, i) => r.status === 'fulfilled' && r.value[key]?.length
          ? [{code: cities[i], name: r.value.city_name || choices(metadata.city).get(cities[i]) || cities[i], children: r.value[key]}] : []);
      }
      localLocations.forEach((r,i) => {
        if (r.status === 'rejected' || r.value.source === 'missing') metadata.locationNotes.push(`${choices(metadata.city).get(cities[i]) || cities[i]}：精细位置目录未读到，可由用户 AI 显式维护一次后使用。`);
      });
      recipe = results[3].status === 'fulfilled' ? results[3].value : null;
      platform = results[4].status === 'fulfilled' ? results[4].value : null;
      const opened = [...host.querySelectorAll('details[data-wc-open][open]')].map(el => el.dataset.wcOpen);
      renderConditions(); renderResults(); renderDiagnostics(); updateSummary();
      opened.forEach(key => { const element = host.querySelector(`details[data-wc-open="${key}"]`); if (element) element.open = true; });
    } finally { busy = false; }
  }
  $('[data-wc-refresh]').addEventListener('click', () => refresh().catch(err => toast(err.message || '刷新失败', 'bad')));
  $('[data-wc-feedback]').addEventListener('click', () => openFeedback());
  $('[data-wc-close]').addEventListener('click', () => { $('[data-wc-feedback-box]').hidden = true; });
  $('[data-wc-question-text]').addEventListener('input', event => { requestText = event.target.value; updateSummary(); });
  host.addEventListener('click', event => { const button = event.target.closest('[data-wc-day]'); if (button) { selectedDay = button.dataset.wcDay; renderSelectedDay(); } });
  $('[data-wc-copy]').addEventListener('click', async () => {
    const text = summaryText();
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text); $('[data-wc-copy-status]').textContent = '已复制，粘贴给你的AI即可。';
    } catch {
      const area = document.createElement('textarea'); area.value = text; area.className = 'wc-copy-fallback'; area.setAttribute('aria-label', '请手动复制采集记录');
      $('[data-wc-feedback-box]').appendChild(area); area.focus(); area.select();
      let copied = false; try { copied = document.execCommand('copy'); } catch { /* 保留已选文本 */ }
      if (copied) area.remove();
      $('[data-wc-copy-status]').textContent = copied ? '已复制，粘贴给你的AI即可。' : '自动复制不可用，内容已选中，请手动复制。';
    }
  });
  await refresh();
  const timer = setInterval(() => refresh().catch(() => {}), 30000);
  return { refresh, dispose() { disposed = true; clearInterval(timer); } };
}
