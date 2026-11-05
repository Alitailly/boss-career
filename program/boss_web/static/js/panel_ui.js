/** 人读结果，AI 接收反馈。这里只复制本地文字，不启动任务、不修改配置。 */
import { escapeHtml as e, modal, toast } from './ui.js';

export function pageHeader(step, title, description, actions = '') {
  return `<div class="page-head"><div class="page-head-text"><span class="page-eyebrow">${e(step)}</span><h1 class="hero-title">${e(title)}</h1><p class="hero-sub">${e(description)}</p></div>${actions ? `<div class="btn-row">${actions}</div>` : ''}</div>`;
}

export function feedbackButton(topic, label = '和 AI 调整') {
  return `<button class="btn sm" type="button" data-ai-feedback="${e(topic)}">${e(label)} <span aria-hidden="true">↗</span></button>`;
}

const topics = {
  "home": {
    "title": "求职流程"
  },
  "resume": {
    "title": "简历与资料"
  },
  "tasks": {
    "title": "岗位采集"
  },
  "match": {
    "title": "匹配评分"
  },
  "review": {
    "title": "招呼语与发送"
  },
  "feedback": {
    "title": "每日复盘"
  },
  "interview": {
    "title": "面试准备"
  },
  "settings": {
    "title": "AI 与设置"
  },
  "login": {
    "title": "平台账号"
  },
  "jobs": {
    "title": "本地岗位库"
  },
  "activity": {
    "title": "任务记录"
  }
};

function currentTopic() {
  const route = location.hash.replace(/^#\//, '').split('?')[0];
  return ({ candidate:'resume', execution:'settings', llm:'settings', workflow:'home' })[route] || (topics[route] ? route : 'home');
}

/** 原生按钮支持键盘，切换仅改变可见区域，保留勾选与已加载数据。 */
export function wireSections(root, { buttons = '[data-section-tab]', panels = '[data-section-panel]', initial, onChange } = {}) {
  const tabs = [...root.querySelectorAll(buttons)];
  const panes = [...root.querySelectorAll(panels)];
  function show(name, focus = false) {
    if (!tabs.some(tab => tab.dataset.sectionTab === name)) return;
    tabs.forEach(tab => {
      const selected = tab.dataset.sectionTab === name;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.classList.toggle('active', selected);
      if (selected && focus) tab.focus();
    });
    panes.forEach(pane => { pane.hidden = pane.dataset.sectionPanel !== name; });
    onChange?.(name);
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => show(tab.dataset.sectionTab));
    tab.addEventListener('keydown', event => {
      const next = { ArrowRight:(index + 1) % tabs.length, ArrowLeft:(index - 1 + tabs.length) % tabs.length, Home:0, End:tabs.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault(); show(tabs[next].dataset.sectionTab, true);
    });
  });
  show(initial || tabs[0]?.dataset.sectionTab);
  return show;
}

function visibleReadout(topic) {
  // 设置页不采集 DOM 文本，以免把 API Key、路径等配置带进剪贴板。
  if (topic === 'settings' || topic === 'login') return '';
  const selectors = '.wc-results, .wc-condition-list, .wc-platform-grid, .ms-counts, .ms-current, .wf-funnel, .rr-summary, .rr-update, [data-overview-metrics], .rs-current';
  return [...document.querySelectorAll(`#view :is(${selectors})`)]
    .filter(node => !node.closest('[hidden]') && node.getClientRects().length)
    .map(node => node.innerText.trim()).filter(Boolean).join('\n\n').slice(0, 6500);
}

export function installPanelFeedback() {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-ai-feedback]');
    if (!button) return;
    const key = button.dataset.aiFeedback === 'current' ? currentTopic() : button.dataset.aiFeedback;
    const topic = topics[key] || topics.home;
    const readout = visibleReadout(key);
    const body = document.createElement('div');
    body.className = 'panel-feedback';
    body.innerHTML = `<p class="muted">复制当前可见记录，可附上你自己的观察。</p><label class="field">我的观察（可留空）<textarea class="input" rows="4" data-feedback-question placeholder="填写具体岗位或你看到的情况"></textarea></label><details><summary>本次附带的面板记录</summary><pre class="wc-summary">${e(readout || '当前页面没有附带记录。')}</pre></details><div class="btn-row mt-16"><button class="btn primary" type="button" data-feedback-copy>复制记录</button><span class="muted" data-feedback-state role="status"></span></div>`;
    const dialog = modal({ title:`面板记录 · ${topic.title}`, body });
    const input = body.querySelector('[data-feedback-question]');
    input.value = button.dataset.aiQuestion || '';
    const buildText = () => `页面：${topic.title}\n面板位置：${location.hash || '#/'}\n观察时间：${new Date().toLocaleString('zh-CN')}\n我的观察：${input.value.trim() || '未填写'}\n\n当前可见记录（可能已变化）：\n${readout || '未附带'}`;
    const fullText = document.createElement('details');
    fullText.className = 'feedback-full-text';
    fullText.innerHTML = '<summary>查看完整记录 · 也可手动复制</summary><textarea class="input wc-copy-fallback" rows="8" readonly aria-label="完整面板记录"></textarea>';
    const preview = fullText.querySelector('textarea');
    body.appendChild(fullText);
    const updatePreview = () => { preview.value = buildText(); };
    updatePreview();
    input.addEventListener('input', updatePreview);
    body.querySelector('[data-feedback-copy]').onclick = async () => {
      updatePreview();
      const text = preview.value;
      try { await navigator.clipboard.writeText(text); body.querySelector('[data-feedback-state]').textContent = '已复制；也可展开完整文案手动复制。'; toast('记录已复制', 'ok'); }
      catch {
        fullText.open = true; preview.focus(); preview.select(); body.querySelector('[data-feedback-state]').textContent = '请在下方手动复制。';
      }
    };
    dialog.box.querySelector('textarea')?.focus();
  });
}
