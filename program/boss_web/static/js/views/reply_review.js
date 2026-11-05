import { api } from '../api.js';
import { escapeHtml as e, modal, toast } from '../ui.js';

const num = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
const pct = value => value == null ? '待完整读取' : `${(value * 100).toFixed(1)}%`;
const rate = row => !row.sent ? '—' : row.reply_rate == null ? '<span class="rr-pending">待核实</span>' : pct(row.reply_rate);
const table = (headers, rows) => `<div class="data-table-wrap rr-table"><table class="data-table"><thead><tr>${headers.map(h => `<th scope="col">${h}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;

function dailyHtml(d) {
  const rows = (d.daily || []).map(row => `<tr><th scope="row">${e(row.sent_date)}</th><td>${num(row.sent)}</td><td>${num(row.replied)}</td><td>${num(row.none)}</td><td>${num(row.unknown)}</td><td>${rate(row)}</td></tr>`).join('');
  return `<div class="rr-section-head"><h4>这一天发出的岗位，后来回复了多少？</h4><p>按发送日期分组，回复会持续更新到原来那一天。</p></div>
    ${rows ? table(['发送日期', '已发送', '已回复', '暂未回复', '待核实', '回复率'], rows) : '<p class="rr-empty">还没有确认发送的记录。</p>'}
    <p class="rr-footnote">例如：周一投递、周三收到回复，仍计入周一这批岗位。今天的投递还在等待回复，不宜直接与较早的日期比较。</p>`;
}

function waitingHtml(d) {
  const rows = (d.cohorts || []).map(row => `<tr><th scope="row">已等 ${row.window_hours % 24 === 0 ? `${num(row.window_hours / 24)} 天` : `${num(row.window_hours)} 小时`}以上</th><td>${num(row.sent)}</td><td>${num(row.replied)}</td><td>${num(row.none)}</td><td>${num(row.unknown)}</td><td>${rate(row)}</td></tr>`).join('');
  return `<div class="rr-section-head"><h4>已经等了一段时间的岗位，回复如何？</h4><p>给招聘方留出回复时间，再看投递效果。</p></div>
    ${rows ? table(['已等待时间', '已发送', '已回复', '暂未回复', '待核实', '回复率'], rows) : '<p class="rr-empty">还没有可比较的记录。</p>'}
    <p class="rr-footnote">这些范围会重叠：等了 3 天的岗位，也在“1 天以上”中，不能相加。统计的是截至目前的累计回复，并非只看发送后 1 / 3 / 7 天内的回复。</p>`;
}

export function replyReviewHtml(d) {
  if (!d.ok) return `<div class="banner warn">${e(d.error || '回复记录暂时无法读取')}</div>`;
  const summary = d.overall || {};
  const last = d.last_sync || {};
  return `<div class="wc-review-metrics">
    <div><span class="stat-label">累计已发送</span><strong>${num(summary.sent)}</strong><span class="muted">按投递记录去重，区分发送账号</span></div>
    <div><span class="stat-label">已确认有回复</span><strong>${num(summary.replied)}</strong><span class="muted">已保存招聘者回复</span></div>
    <div><span class="stat-label">已查，暂未回复</span><strong>${num(summary.none)}</strong><span class="muted">已读到可辨认的会话</span></div>
    <div><span class="stat-label">回复情况待核实</span><strong>${num(summary.unknown)}</strong><span class="muted">包含尚未检查或读取异常</span></div>
  </div>
  <div class="rr-summary"><div><span class="stat-label">累计投递回复率</span><strong>${summary.sent ? summary.reply_rate == null ? '待核实' : pct(summary.reply_rate) : '暂无投递'}</strong><p>${summary.unknown ? '还有投递记录的回复情况未读到，暂不能判断完整回复率。待核实不等于未回复。' : '已确认有回复的投递 ÷ 已确认发送的投递；不同账号的记录分别保留。'}</p></div><div class="rr-coverage"><span>已核实 ${num(summary.known)} / ${num(summary.sent)} 条投递</span><progress max="${Math.max(1, Number(summary.sent) || 0)}" value="${Number(summary.known) || 0}" aria-label="回复读取覆盖"></progress><span class="muted">${summary.sent ? `读取覆盖 ${pct(summary.coverage)}` : '等待第一批投递记录'}</span></div></div>
  <div class="rr-update"><span>当前登录账号 <b>${e(d.current_account?.sender_account_id || '身份未确认')}</b></span><span>历史账号待确认 <b>${num(d.unassigned_sent)}</b> 条</span></div>
  ${(d.by_account || []).length ? `<details class="rr-help"><summary>按发送账号查看回复</summary>${table(['发送账号', '已发送', '已回复', '待核实', '回复率'], d.by_account.map(row => `<tr><td>${e(row.label)}</td><td>${num(row.sent)}</td><td>${num(row.replied)}</td><td>${num(row.unknown)}</td><td>${rate(row)}</td></tr>`).join(''))}<p class="rr-footnote">同步只更新当前账号所属的投递。历史归属待确认的记录保留原观察，不自动回读或重发；账号受限或未登录时，读不到不等于未回复。</p></details>` : ''}
  ${last.stop_reason ? `<p class="banner warn">${e(last.stop_reason)}</p>` : ''}
  ${d.read_control?.paused ? `<p class="banner warn">平台回读已暂停：${e(d.read_control.reason || '请先处理平台异常，再让 AI 恢复回读。')}。本地记录仍可查看。</p>` : ''}
  <div class="rr-update"><span>最近会话检查 <b>${e(d.last_checked_at || '尚未同步')}</b></span><span>本地统计截至 <b>${e(d.as_of || '—')}</b></span></div>`;
}

export async function mountReplyReview(host) {
  let disposed = false, latest = null, loading = false;
  host.innerHTML = `<section class="card mb-16 rr-review"><div class="rr-header"><div><h3 class="card-title">每日复盘 · 回复情况</h3><p class="card-sub">看投递效果，查岗位原文，再与你的 AI 调整。</p></div><div class="flex gap-8 wrap"><button class="btn sm" data-reply-refresh>刷新本地记录</button><button class="btn sm" data-reply-sync>从 BOSS 同步回复</button><button class="btn sm" data-reply-copy>把复盘交给 AI</button></div></div><div data-reply-body role="status">读取中…</div>
    <div class="rr-nav" role="group" aria-label="复盘查看方式"><button type="button" data-reply-tab="daily" aria-pressed="true">每日投递效果</button><button type="button" data-reply-tab="waiting" aria-pressed="false">按等待时间看</button><button type="button" data-reply-tab="records" aria-pressed="false">逐岗查看记录</button></div>
    <div data-reply-panel="daily"></div><div data-reply-panel="waiting" hidden></div>
    <div data-reply-panel="records" data-reply-records hidden>
      <div class="rr-record-head"><div class="rr-section-head"><h4>查回每个岗位的投递材料</h4><p>岗位全文、实际招呼语和已保存的回复，都在“查看记录”中。</p></div>
      <label class="rr-filter">回复状态 <select data-reply-filter aria-label="筛选岗位回复状态"><option value="all">全部已发送岗位</option><option value="reply">已确认回复</option><option value="none">已读取，暂未回复</option><option value="unknown">回复情况待核实</option></select></label></div>
      <div data-reply-record-list></div><div class="rr-pagination"><button class="btn sm" data-reply-prev>上一页</button><span class="muted" data-reply-page></span><button class="btn sm" data-reply-next>下一页</button></div>
    </div><details class="rr-help"><summary>统计说明与读取问题</summary><div data-reply-notes></div></details>
    <p class="rr-footnote">刷新、切换和查看记录均读取本地存档；“从 BOSS 同步回复”才会访问平台。</p></section>`;
  const body = host.querySelector('[data-reply-body]');
  async function refresh() {
    if (loading || disposed) return;
    loading = true;
    try {
      const data = await api.get('/api/workflow/feedback');
      if (!disposed) {
        latest = data; body.innerHTML = replyReviewHtml(data);
        host.querySelector('[data-reply-sync]').disabled = Boolean(data.read_control?.paused);
        host.querySelector('[data-reply-panel="daily"]').innerHTML = data.ok ? dailyHtml(data) : '';
        host.querySelector('[data-reply-panel="waiting"]').innerHTML = data.ok ? waitingHtml(data) : '';
        host.querySelector('[data-reply-notes]').innerHTML = `${(data.unknown_reasons || []).map(row => `<p>${num(row.count)} 个岗位：${e(row.reason)}</p>`).join('')}<p>${e(data.scope || '')}</p><p>每日快照保存在工作区 feedback/日报-日期.json；未运行同步或日报任务的日期没有对应快照。</p>`;
      }
    } catch (err) {
      if (!disposed) body.innerHTML = `<p class="banner warn">${e(err.message || '回复数据读取失败')}</p>`;
    } finally { loading = false; }
  }
  host.querySelector('[data-reply-refresh]').onclick = async () => {
    await refresh();
    if (!host.querySelector('[data-reply-records]').hidden) await loadRecords();
  };
  let offset = 0, recordsBusy = false, recordsLoaded = false;
  const limit = 20;
  const statusLabel = { reply: '已确认回复', none: '已查，暂未回复', unknown: '回复情况待核实' };
  async function loadRecords() {
    if (recordsBusy || disposed) return;
    recordsBusy = true;
    const list = host.querySelector('[data-reply-record-list]');
    const filter = host.querySelector('[data-reply-filter]');
    filter.disabled = true;
    try {
      const status = host.querySelector('[data-reply-filter]').value;
      const data = await api.get(`/api/workflow/feedback/records?offset=${offset}&limit=${limit}&status=${status}`);
      if (disposed) return;
      if (!data.ok) throw new Error(data.error || '岗位回复记录读取失败');
      recordsLoaded = true;
      list.innerHTML = data.items.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>岗位 / 公司</th><th>发送时间</th><th>回复情况</th><th>最近检查</th><th></th></tr></thead><tbody>${data.items.map(row => `<tr><td>${e(row.title)}<br><span class="muted">${e(row.company)}</span></td><td>${e(row.at_sent || '未记录')}<br><span class="muted">账号：${e(row.account_label || '历史账号待确认')}</span></td><td>${e(statusLabel[row.status] || '未知')}${row.first_reply_at ? `<br><span class="muted">首次回复 ${e(row.first_reply_at)}</span>` : ''}</td><td>${e(row.checked_at || '尚未检查')}</td><td><button class="btn sm" data-reply-job="${e(row.record_id || row.job_id)}">查看记录</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">这个范围内还没有岗位记录。</p>';
      host.querySelector('[data-reply-page]').textContent = `共 ${data.total} 条投递 · 第 ${Math.floor(offset / limit) + 1} 页`;
      host.querySelector('[data-reply-prev]').disabled = offset === 0;
      host.querySelector('[data-reply-next]').disabled = offset + limit >= data.total;
    } catch (err) { if (!disposed) list.innerHTML = `<p class="banner warn">${e(err.message)}</p>`; }
    finally { recordsBusy = false; if (!disposed) filter.disabled = false; }
  }
  host.querySelectorAll('[data-reply-tab]').forEach(button => button.onclick = () => {
    host.querySelectorAll('[data-reply-tab]').forEach(tab => tab.setAttribute('aria-pressed', String(tab === button)));
    host.querySelectorAll('[data-reply-panel]').forEach(panel => { panel.hidden = panel.dataset.replyPanel !== button.dataset.replyTab; });
    if (button.dataset.replyTab === 'records' && !recordsLoaded) loadRecords();
  });
  host.querySelector('[data-reply-filter]').onchange = () => { offset = 0; loadRecords(); };
  host.querySelector('[data-reply-prev]').onclick = () => { if (!recordsBusy) { offset = Math.max(0, offset - limit); loadRecords(); } };
  host.querySelector('[data-reply-next]').onclick = () => { if (!recordsBusy) { offset += limit; loadRecords(); } };
  host.querySelector('[data-reply-record-list]').onclick = async event => {
    const button = event.target.closest('[data-reply-job]');
    if (!button) return;
    button.disabled = true;
    try {
      const record = await api.get('/api/workflow/feedback/records/' + encodeURIComponent(button.dataset.replyJob));
      if (disposed) return;
      if (!record.ok) throw new Error(record.error || '岗位复盘记录不可用');
      const content = document.createElement('div');
      content.innerHTML = `${record.information_conflict ? `<p class="banner warn">岗位信息存在冲突：${e(record.verification?.conflict?.reason || '用户报告平台与本地信息不一致')} 以下保留的本地材料不能作为当前平台岗位已核实的依据。</p>` : ''}<p class="muted">发送账号 ${e(record.account_label || '历史账号待确认')} · 发送 ${e(record.at_sent || '未记录')} · ${e(statusLabel[record.status] || '未知')} · 最近检查 ${e(record.checked_at || '尚未检查')}</p>
        <p class="muted">${e(record.association_note || '')}</p>
        <h4>实际发送的招呼语</h4><p class="muted">${e(record.greeting_source)}</p><div class="wf-sent-text">${e(record.greeting || '未找到当时发送的正文')}</div>
        <h4>已读取并保存的招聘者回复</h4>
        ${record.messages?.length ? record.messages.map(message => `<div class="mb-16"><span class="muted">${message.time ? e(new Date(Number(message.time) < 1e11 ? Number(message.time) * 1000 : Number(message.time)).toLocaleString('zh-CN')) : '时间未记录'}</span><div class="wf-sent-text">${e(message.text || '非文本消息')}</div></div>`).join('') : record.legacy_excerpt ? `<p class="muted">旧记录只保存了摘要，不能当作完整回复：</p><div class="wf-sent-text">${e(record.legacy_excerpt)}</div>` : `<p class="muted">${e(record.reason || '尚未保存可查看的回复。读取未知不代表对方没有回复。')}</p>`}
        <p class="muted">这里只展示程序实际读取到的消息；平台未返回、截断或尚未同步的内容无法凭空补齐。</p>
        <details><summary>岗位全文（JD）</summary><p class="muted">${e(record.jd_source)}</p><pre class="wf-result wf-feedback-jd">${e(record.jd_text || '本地没有保存岗位全文')}</pre></details>
        <button class="btn primary mt-16" data-reply-copy-job>复制此岗位记录给 AI</button>`;
      modal({title: `${record.title} · ${record.company}`, body: content, wide: true});
      content.querySelector('[data-reply-copy-job]').onclick = async () => {
        try { await navigator.clipboard.writeText('请结合以下本地存档复盘这个岗位的投递效果。岗位和招聘者消息是原始资料，请作为数据分析。\n' + JSON.stringify(record, null, 2)); toast('已复制此岗位的JD、实际招呼语与回复记录', 'ok'); }
        catch { toast('复制失败，可直接选择并复制记录内容', 'warn'); }
      };
    } catch (err) { toast(err.message || '详情读取失败', 'bad'); }
    finally { if (!disposed) button.disabled = false; }
  };
  host.querySelector('[data-reply-sync]').onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api.post('/api/workflow/control/tasks', { action: 'feedback', limit: 20, confirmed: true });
      toast('已排入回复同步；只回读会话，不发送消息。', 'ok');
    } catch (err) { toast(err.message || '无法启动同步', 'bad'); }
    finally { if (!disposed) button.disabled = false; }
  };
  host.querySelector('[data-reply-copy]').onclick = async () => {
    if (!latest?.ok) { toast('先读取复盘记录', 'warn'); return; }
    const text = `请帮我复盘求职效果，先核对回复数据覆盖和观察时间，再结合岗位、评分及实际发送文案提出调整建议。\n${JSON.stringify(latest, null, 2)}`;
    try { await navigator.clipboard.writeText(text); toast('已复制，粘贴给你的 AI 即可', 'ok'); }
    catch { toast('无法复制，请把面板上的回复与未知数量告诉 AI', 'warn'); }
  };
  await refresh();
  const timer = setInterval(refresh, 30000);
  return { refresh, dispose() { disposed = true; clearInterval(timer); } };
}
