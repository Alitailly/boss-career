/** 系统配置：AI、采集参数、搜索条件、黑名单规则、招呼语提示词和版本更新。 */

import { api } from "../api.js";
import { toast, escapeHtml, modal, multiSelect, cascader } from "../ui.js";
import { applyUpdateStatus, peekUpdateStatus } from "../update.js";
import { renderLLM } from "./llm.js";
import { renderGreetingPrompt } from "./greeting_prompt.js";
import { renderSettingsUpdate } from "./settings_update.js";
import { renderWorkspace } from "./settings_workspace.js";
import { renderStrategySummary } from './strategy_summary.js';
import { pageHeader } from '../panel_ui.js';

/**
 * 系统配置页：左侧目录（AI / 采集 / 招呼语 / 版本更新）+ 右侧面板，返回卸载函数。
 *
 * @param {HTMLElement} root 挂载容器
 * @returns {Promise<() => void>} 卸载函数，移除监听并销毁当前面板
 */
export async function renderSettings(root) {
  root.innerHTML = `
    ${pageHeader('WORKSPACE / PERSONALIZE', 'AI 与设置', '查看当前采用的模板、条件与程序模型配置。')}
    <div class="settings-shell">
      <aside class="card settings-nav" aria-label="系统配置目录">
        <button class="settings-tab active" data-tab="overview" type="button">当前个人方案</button>
        <button class="settings-tab" data-tab="ai" type="button">模型与 API</button>
        <button class="settings-tab" data-tab="greeting" type="button">招呼语约束</button>
        <button class="settings-tab" data-tab="workspace" type="button">工作区</button>
        <button class="settings-tab" data-tab="collection" type="button">高级采集配置</button>
        <button class="settings-tab" data-tab="update" type="button">版本更新<span class="settings-tab-dot" hidden></span></button>
      </aside>
      <section class="card settings-pane" id="settings-pane"></section>
    </div>
  `;
  let pane = root.querySelector("#settings-pane");
  const tabs = [...root.querySelectorAll(".settings-tab")];
  let active = "overview";
  let dirty = false;
  let paneDispose = null;
  let paneVersion = 0;

  /**
   * 标/清「有未保存修改」，并切换副标题文案。
   *
   * @param {boolean} value 是否有未保存修改
   */
  function setDirty(value) {
    dirty = Boolean(value);
    const title = root.querySelector(".hero-sub");
    if (title) title.textContent = dirty
      ? "有未保存修改。切换目录前请先保存或确认放弃修改。"
      : "核对正在使用的个人方法。日常调整交给你的 AI，模型 API 与高级手动配置也可在这里管理。";
  }

  /**
   * 高亮当前目录页签。
   *
   * @param {string} name 页签名（`ai` / `collection` / `greeting` / `update`）
   */
  function setActive(name) {
    active = name;
    tabs.forEach((tab) => {
      const on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-current", on ? "page" : "false");
    });
  }

  /**
   * 切到某个目录页；有未保存修改会先确认。同一目录不重载。
   *
   * @param {string} name 页签名
   */
  async function show(name) {
    if (name === active && pane.childElementCount) return;
    if (dirty && !window.confirm("当前目录有未保存修改，确定放弃并切换吗？")) return;
    setDirty(false);
    setActive(name);
    if (typeof paneDispose === "function") {
      try { paneDispose(); } catch {}
    }
    paneDispose = null;
    const version = ++paneVersion;
    const nextPane = pane.cloneNode(false);
    pane.replaceWith(nextPane);
    pane = nextPane;
    pane.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>加载中…</p></div>`;
    if (name === 'overview') {
      const dispose = await renderStrategySummary(pane);
      if (version !== paneVersion) { if (typeof dispose === 'function') dispose(); }
      else paneDispose = dispose;
      return;
    }
    if (name === "workspace") {
      await renderWorkspace(pane);
      return;
    }
    if (name === "ai") {
      await renderLLM(pane, setDirty);
      return;
    }
    if (name === "greeting") {
      await renderGreetingPrompt(pane, setDirty);
      return;
    }
    if (name === "update") {
      const dispose = await renderSettingsUpdate(pane);
      if (version !== paneVersion) { if (typeof dispose === 'function') dispose(); }
      else paneDispose = dispose;
      return;
    }
    await renderCollection(pane, setDirty);
  }

  /**
   * 有新版且没被忽略才在「版本更新」页签上点小圆点；不弹窗（弹窗归首屏/update.js）。
   *
   * 后端 `should_prompt = has_update && !suppressed`——`ignored` 是忽略水位线，
   * 新版比水位高时照亮，所以判断只看 `should_prompt`，不能再拿 `!ignored` 卡一道。
   *
   * 圆点要一直挂着：关掉弹窗 ≠ 忽略，只要还有没被忽略的新版就不灭。因此进页面
   * 得先按缓存画上，不能只等 60s 一轮的 `boss:update`——关完弹窗进设置页那一分钟
   * 里圆点会是灭的，看着就像「关掉就没提示了」。
   *
   * @param {Event} e `boss:update` 事件，`detail` 为检测结果
   */
  function onUpdateDot(e) {
    const dot = root.querySelector(".settings-tab-dot");
    if (!dot) return;
    const d = (e && e.detail) || {};
    dot.hidden = !Boolean(d.has_update && d.should_prompt);
  }
  window.addEventListener("boss:update", onUpdateDot);
  const cached = peekUpdateStatus();
  if (cached) {
    onUpdateDot({ detail: cached });
  } else {
    api
      .get("/api/system/update")
      .then((s) => {
        if (s) applyUpdateStatus(s);
      })
      .catch(() => {
      });
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => show(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        show(tab.dataset.tab);
      }
    });
  });
  const requestedTab = new URLSearchParams(location.hash.split("?")[1] || "").get("tab");
  await show(["overview", "workspace", "ai", "collection", "greeting", "update"].includes(requestedTab) ? requestedTab : "overview");
  return () => {
    paneVersion += 1;
    window.removeEventListener("boss:update", onUpdateDot);
    if (typeof paneDispose === "function") {
      try { paneDispose(); } catch {}
    }
  };
}

/**
 * 「采集配置」面板：搜索条件、采集参数、来源切换和黑名单规则。
 *
 * @param {HTMLElement}  root      挂载容器
 * @param {(v: boolean) => void} markDirty 标/清未保存修改
 */
async function renderCollection(root, markDirty) {
  root.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div><h2 class="card-title">手动采集配置</h2><div class="card-sub">供「职位」页采集一次使用；AI 批量采集以活动求职方案为准，完整条件见「任务与采集」。</div></div>
      <div class="btn-row"><button class="btn" id="collection-reset">重置</button><button class="btn primary" id="collection-save">保存全部</button></div>
    </div>
    <div class="settings-scroll">
      <div class="row-4">
        <div class="field"><label>最大页数</label><input class="input" id="c-max-pages" type="number" min="1" max="10"></div>
        <div class="field"><label>起始页</label><input class="input" id="c-start-page" type="number" min="1"></div>
        <div class="field"><label>页间隔（秒）</label><input class="input" id="c-page-interval" type="number" min="0" max="30" step="0.1"></div>
        <div class="field"><label>详情间隔（秒）</label><input class="input" id="c-detail-interval" type="number" min="0" max="30" step="0.1"></div>
      </div>
      <div class="row-3">
        <label class="pill on"><input type="checkbox" id="c-fetch-details"> 抓取职位详情 / JD</label>
        <label class="pill on"><input type="checkbox" id="c-store-blacklisted"> 入库并标记黑名单职位</label>
      </div>
      <div class="divider"></div>
      <div class="card-sub mb-8">采集来源 <span class="muted" style="font-size:12px">（二选一：这次抓取走哪条检索流）</span></div>
      <div class="row-2" id="c-source-row">
        <label class="pill on"><input type="radio" name="c-source" value="search"> 关键词搜索（search/joblist）</label>
        <label class="pill"><input type="radio" name="c-source" value="expect"> 求职期望（recommend/job/list）</label>
      </div>
      <div class="row-2">
        <div class="field" id="c-expect-field">
          <label id="c-expect-label">期望</label>
          <select class="select" id="c-expect"></select>
          <div class="muted" id="c-expect-hint" style="font-size:12px"></div>
        </div>
      </div>
      <!-- 临时期望：对应站点「快速添加」的临时检索，主参数是 expectInfo JSON（不是 encryptExpectId）。
           键名必须是 expectType/position/location/lowSalary/highSalary——前端 getFormData()
           的 temp-expect 分支就是这么序列化的（location 是城市 code，不是显示名）。 -->
      <div class="row-2 hidden" id="c-temp-fields">
        <div class="field"><label>职位</label><input class="input" id="c-tmp-position" autocomplete="off"></div>
        <div class="field"><label>城市</label><div id="c-tmp-location"></div></div>
        <div class="field"><label>期望类型</label><input class="input" id="c-tmp-type" autocomplete="off"></div>
        <div class="field"><label>薪资下限（K）</label><input class="input" id="c-tmp-low" type="number" min="0"></div>
        <div class="field"><label>薪资上限（K）</label><input class="input" id="c-tmp-high" type="number" min="0"></div>
      </div>
      <div class="muted mb-8" id="expect-source"></div>
      <div class="divider"></div>
      <div class="card-sub mb-8">BOSS 搜索条件 <span class="muted" style="font-size:12px">（从程序提供的平台选项中选择，不同岗位类型可用的条件不同）</span></div>
      <div id="search-source" class="muted mb-8"></div>
      <div class="row-2">
        <div class="field" id="f-query"><label>关键词</label><input class="input" id="s-query" autocomplete="off"></div>
        <div class="field"><label>每页条数</label><input class="input" id="s-pageSize" type="number" min="1" max="50"></div>
        <div class="field"><label>城市</label><div id="s-city"></div></div>
        <div class="field" id="f-position"><label>职位类别</label><div id="s-position"></div></div>
        <div class="field" id="f-district"><label>区／商圈</label><div id="s-district"></div></div>
        <div class="field" id="f-subway"><label>地铁线路／站点</label><div id="s-subway"></div></div>
        <div class="field" id="f-jobType"><label>职位类型</label><select class="select" id="s-jobType"></select></div>
        <div class="field" id="f-salary"><label>薪资</label><select class="select" id="s-salary"></select></div>
        <div class="field"><label>经验</label><div id="s-experience"></div></div>
        <div class="field"><label>学历</label><div id="s-degree"></div></div>
        <div class="field"><label>行业</label><div id="s-industry"></div></div>
        <div class="field"><label>规模</label><div id="s-scale"></div></div>
        <div class="field" id="f-payType"><label>结算方式</label><div id="s-payType"></div></div>
        <div class="field" id="f-partTime"><label>兼职时段</label><div id="s-partTime"></div></div>
        <div class="field" id="f-stage"><label>融资阶段</label><div id="s-stage"></div></div>
      </div>
      <p class="muted" id="s-location-note">精细位置按城市读取本地目录，由 AI 可进一步配置；查看选项不访问平台。</p>
      <div class="divider"></div>
      <div class="flex between center wrap gap-8 mb-8"><div class="card-sub">黑名单规则</div><div class="btn-row"><button class="btn sm" id="rule-add-company">新增公司规则</button><button class="btn sm" id="rule-add-jd">新增 JD 规则</button><button class="btn sm ghost" id="rules-recheck">重新检查已入库职位</button></div></div>
      <div id="recheck-status" class="muted mb-8" role="status"></div>
      <div id="rules-list"></div>
    </div>
  `;
  const $ = (id) => root.querySelector("#" + id);
  const fields = ["c-max-pages", "c-start-page", "c-page-interval", "c-detail-interval", "s-query", "s-pageSize",
                  "c-tmp-position", "c-tmp-type", "c-tmp-low", "c-tmp-high"];
  fields.forEach((id) => $(id).addEventListener("input", () => markDirty(true)));
  $("c-max-pages").addEventListener("blur", () => {
    const val = Number($("c-max-pages").value);
    if (val > 10) $("c-max-pages").value = 10;
    if (val < 1) $("c-max-pages").value = 1;
  });
  ["c-fetch-details", "c-store-blacklisted"].forEach((id) => $(id).addEventListener("change", () => markDirty(true)));
  ["s-jobType", "s-salary", "c-expect"].forEach((id) => $(id).addEventListener("change", () => { updateSourceUI(); markDirty(true); }));
  root.querySelectorAll('input[name="c-source"]').forEach((el) => {
    el.addEventListener("change", () => {
      fillExpectSelect(expectOptions, $("c-expect").value);
      updateSourceUI();
      markDirty(true);
    });
  });

  const cityPicker = cascader({ onChange: () => { markDirty(true); districtPicker.setValue([]); subwayPicker.setValue([]); loadLocations().catch(err => toast(err.message, 'warn')); } });
  cityPicker.el.id = "s-city";
  $("s-city").replaceWith(cityPicker.el);
  const tmpLocationPicker = cascader({ onChange: () => markDirty(true) });
  tmpLocationPicker.el.id = "c-tmp-location";
  $("c-tmp-location").replaceWith(tmpLocationPicker.el);
  const industryPicker = cascader({ multiple: true, onChange: () => markDirty(true) });
  industryPicker.el.id = "s-industry";
  $("s-industry").replaceWith(industryPicker.el);
  const positionPicker = cascader({multiple:true, onChange:() => markDirty(true)});
  positionPicker.el.id = 's-position'; $('s-position').replaceWith(positionPicker.el);
  const districtPicker = cascader({multiple:true, onChange:() => markDirty(true)});
  districtPicker.el.id = 's-district'; $('s-district').replaceWith(districtPicker.el);
  const subwayPicker = cascader({multiple:true, onChange:() => markDirty(true)});
  subwayPicker.el.id = 's-subway'; $('s-subway').replaceWith(subwayPicker.el);
  let locationRequest = 0;
  function locationNodes(rows, allLabel) {
    return (rows || []).map(row => ({code:`group-${row.code}`,name:row.name,
      children:[{code:row.code,name:`${row.name}（${allLabel}）`},...(row.children || []).map(child => ({...child,name:`${row.name} / ${child.name}`}))]}));
  }
  function unpackLocations(value) {
    return (value || '').split(',').filter(Boolean).flatMap(token => {
      const [parent,children] = token.split(':');
      return children ? children.split('_').map(child => `${parent}:${child}`) : [parent];
    });
  }
  async function loadLocations() {
    const current = ++locationRequest, city = cityPicker.getValue();
    districtPicker.setNodes([]); subwayPicker.setNodes([]);
    if (!city || city === '100010000') { $('s-location-note').textContent = '先选择具体城市，再查看区／商圈和地铁选项。'; return; }
    const doc = await api.get(`/api/filters/locations?city=${encodeURIComponent(city)}`);
    if (current !== locationRequest || city !== cityPicker.getValue()) return;
    districtPicker.setNodes(locationNodes(doc.multiBusinessDistrict,'整区'));
    subwayPicker.setNodes(locationNodes(doc.multiSubway,'全线'));
    $('s-location-note').textContent = doc.source === 'missing' ? '该城市精细位置目录尚未保存，可由用户 AI 显式维护一次后使用。当前未限制精细位置。'
      : `${doc.city_name || '当前城市'}：区／商圈与地铁可分别选择；选项来自本地目录，查看不访问平台。`;
  }

  const multiIds = ["s-experience", "s-degree", "s-scale", "s-payType", "s-partTime", "s-stage"];
  const multis = {};
  multiIds.forEach((id) => {
    const ms = multiSelect({ onChange: () => markDirty(true) });
    ms.el.id = id;
    $(id).replaceWith(ms.el);
    multis[id] = ms;
  });

  /**
   * 用 code/name 列表填原生 select，保留当前选中值。
   *
   * @param {HTMLSelectElement} select 目标下拉
   * @param {Array<{code: string, name?: string}>} options 选项列表
   * @param {{blankLabel?: string}} [opts] 空项文案，缺省「不限」
   */
  function fillSelect(select, options, { blankLabel = "不限" } = {}) {
    const current = select.value;
    select.innerHTML = `<option value="">${blankLabel}</option>`;
    options.forEach((opt) => {
      const el = document.createElement("option");
      el.value = opt.code;
      el.textContent = opt.name || opt.code;
      select.appendChild(el);
    });
    if (current && [...select.options].some((o) => o.value === current)) select.value = current;
  }

  /** 行业 DTO 是分组 + options，转成级联的两层树；只有叶子是提交值 */
  function industryNodes(groups) {
    return (groups || []).map((g) => ({
      code: `g:${g.name}`,
      name: g.name,
      children: (g.options || []).map((o) => ({ code: String(o.code), name: o.name || String(o.code) })),
    }));
  }

  /** 期望选项：value 是「来源/期望」组合键，encryptId 用于写回 payload。 */
  let expectOptions = [];

  /** 当前采集来源：`search` 或 `expect`。 */
  function sourceValue() {
    return root.querySelector('input[name="c-source"]:checked')?.value || "search";
  }

  /**
   * 选中采集来源单选，并同步 pill 高亮与表单可见性。
   *
   * @param {string} value `search` 或 `expect`
   */
  function setSource(value) {
    const target = root.querySelector(`input[name="c-source"][value="${value}"]`);
    if (target) target.checked = true;
    root.querySelectorAll("#c-source-row .pill").forEach((pill) => {
      pill.classList.toggle("on", !!pill.querySelector("input")?.checked);
    });
    updateSourceUI();
  }

  /**
   * 重建期望下拉。搜索流加「不带期望」空项；期望流末尾挂「临时期望」。
   * 已保存但在线列表没有的值留占位，别静默丢掉。
   *
   * @param {Array<{value: string, label: string, kind?: string, encryptId?: string, mixType?: string}>} options 期望选项
   * @param {string} [selected] 要选中的 value
   */
  function fillExpectSelect(options, selected) {
    const select = $("c-expect");
    const source = sourceValue();
    select.innerHTML = "";
    if (source === "search") {
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "不带期望（纯关键词）";
      select.appendChild(blank);
    }
    options.forEach((opt) => {
      const el = document.createElement("option");
      el.value = opt.value;
      el.textContent = opt.label;
      select.appendChild(el);
    });
    if (source === "expect") {
      const temp = document.createElement("option");
      temp.value = "temp";
      temp.textContent = "临时期望（快速添加）";
      select.appendChild(temp);
    }
    if (selected && ![...select.options].some((o) => o.value === selected)) {
      const stale = document.createElement("option");
      stale.value = selected;
      stale.textContent = `（已保存但列表里没有）${selected}`;
      select.appendChild(stale);
    }
    if (selected && [...select.options].some((o) => o.value === selected)) select.value = selected;
    else if (source === "expect" && select.options.length) select.value = select.options[0].value;
  }

  /** 把存下来的 collection 反查成下拉键：recommend / part_time / expect:<id> / temp。 */
  function expectSelectValue(c) {
    const kind = (c.expect_kind || "").trim();
    const id = (c.encrypt_expect_id || "").trim();
    if (kind === "recommend") return "recommend";
    if (kind === "part_time") return "part_time";
    if (kind === "temp_expect") return "temp";
    if (kind === "expect" && id) return `expect:${id}`;
    if (id) {
      const hit = expectOptions.find((o) => o.encryptId === id);
      return hit ? hit.value : `expect:${id}`;
    }
    return "";
  }

  /** 临时期望表单 → expectInfo JSON 串（键名对齐前端 getFormData() 的 temp-expect 分支）。 */
  function tempExpectInfo() {
    return JSON.stringify({
      expectType: $("c-tmp-type").value.trim(),
      position: $("c-tmp-position").value.trim(),
      location: tmpLocationPicker.getValue() || "",
      lowSalary: $("c-tmp-low").value.trim(),
      highSalary: $("c-tmp-high").value.trim(),
    });
  }

  /** 临时期望的人读名（只进 expect_name 展示，不进请求参数）。 */
  function tempExpectName() {
    const position = $("c-tmp-position").value.trim();
    return position ? `临时期望：${position}` : "临时期望";
  }

  /** expectInfo JSON 串 → 临时期望表单（读回已存配置用）。 */
  function fillTempExpectInfo(raw) {
    let info = {};
    try { info = raw ? JSON.parse(raw) : {}; } catch { info = {}; }
    if (!info || typeof info !== "object") info = {};
    $("c-tmp-type").value = info.expectType ?? "";
    $("c-tmp-position").value = info.position ?? "";
    $("c-tmp-low").value = info.lowSalary ?? "";
    $("c-tmp-high").value = info.highSalary ?? "";
    tmpLocationPicker.setValue(info.location ?? "");
  }

  /**
   * 下拉当前项 → 要写回配置的期望三件套。
   *
   * @returns {{expect_kind: string, encrypt_expect_id: string, mix_expect_type: string, expect_name: string, expect_info: string}} 写回字段
   */
  function expectSelection() {
    const source = sourceValue();
    const raw = $("c-expect").value;
    const hit = expectOptions.find((o) => o.value === raw);
    if (source === "expect") {
      if (raw === "temp") {
        return {
          expect_kind: "temp_expect",
          encrypt_expect_id: "",
          mix_expect_type: "",
          expect_name: tempExpectName(),
          expect_info: tempExpectInfo(),
        };
      }
      let kind = hit?.kind || "";
      if (!kind) {
        if (raw === "recommend") kind = "recommend";
        else if (raw === "part_time") kind = "part_time";
        else if (raw.startsWith("expect:")) kind = "expect";
      }
      const id = hit?.encryptId || (raw.startsWith("expect:") ? raw.slice("expect:".length) : "");
      return {
        expect_kind: kind,
        encrypt_expect_id: id,
        mix_expect_type: hit?.mixType || "",
        expect_name: hit ? hit.label.replace(/^[^：]*：/, "") : (kind || ""),
        expect_info: "",
      };
    }
    const id = hit?.encryptId || (raw.startsWith("expect:") ? raw.slice("expect:".length) : "");
    return {
      expect_kind: "",
      encrypt_expect_id: id,
      mix_expect_type: "",
      expect_name: hit ? hit.label.replace(/^[^：]*：/, "") : "",
      expect_info: "",
    };
  }

  /**
   * 从账号在线拉期望列表并重建下拉；拉失败只提示，不挡其它配置。
   *
   * @param {string} [selected] 要选中的下拉键
   */
  async function loadExpects(selected) {
    const hint = $("c-expect-hint");
    try {
      const data = await api.get("/api/collection/expects");
      const options = [];
      if (data.mix) {
        options.push({
          value: "recommend",
          label: `推荐（混合）：${data.mix.label}`,
          kind: "recommend",
          encryptId: data.mix.encryptExpectId,
          mixType: data.mix.mixExpectType || "",
        });
      }
      (data.partTime || []).forEach((item) => {
        options.push({
          value: "part_time",
          label: `兼职：${item.label}`,
          kind: "part_time",
          encryptId: item.encryptExpectId,
          mixType: "",
        });
      });
      (data.expects || []).forEach((item) => {
        options.push({
          value: `expect:${item.encryptExpectId}`,
          label: `我的期望：${item.label}`,
          kind: "expect",
          encryptId: item.encryptExpectId,
          mixType: "",
        });
      });
      expectOptions = options;
      fillExpectSelect(options, selected);
      const src = $("expect-source");
      if (src) src.textContent = options.length
        ? `期望来源：账号在线拉取（${options.length} 条）`
        : "期望来源：账号在线拉取（一条都没有，先去站点配好求职期望）";
      if (hint) hint.textContent = sourceValue() === "expect"
        ? "期望流只带筛选维度，不带关键词（站点点期望 tab 时会删掉 query）。"
        : "选了期望就带上 encryptExpectId，对应站点「期望 tab + 输入关键词」。";
    } catch (err) {
      expectOptions = [];
      fillExpectSelect([], selected);
      const src = $("expect-source");
      if (src) src.textContent = `期望列表拉取失败：${err.message || err}`;
      if (hint) hint.textContent = "期望列表读不到时只能显式指定 expect:<id>。";
    }
    updateSourceUI();
  }

  /**
   * 采集来源切了之后：关键词/职位类型/薪资 vs 兼职维度跟着变。
   * 期望流不带关键词、自己写 jobType；兼职态用 payType/partTime 顶替 salary。
   */
  function updateSourceUI() {
    const source = sourceValue();
    const expectMode = source === "expect";
    const select = $("c-expect");
    root.querySelectorAll("#c-source-row .pill").forEach((pill) => {
      pill.classList.toggle("on", !!pill.querySelector("input")?.checked);
    });
    if (expectMode && !select.value && select.options.length) select.value = select.options[0].value;
    const selected = expectOptions.find((o) => o.value === select.value);
    const partTime = expectMode ? (selected?.kind === "part_time" || select.value === "part_time")
      : $("s-jobType")?.value === "1903";
    const temp = expectMode && select.value === "temp";

    const hideQuery = expectMode;
    const queryField = $("f-query");
    if (queryField) queryField.classList.toggle("hidden", hideQuery);
    if ($("s-query")) $("s-query").disabled = hideQuery;

    const jobTypeField = $("f-jobType");
    if (jobTypeField) jobTypeField.classList.toggle("hidden", expectMode);
    if ($("s-jobType")) $("s-jobType").disabled = expectMode;
    for (const id of ['f-position','f-district','f-subway','s-location-note']) $(id)?.classList.toggle('hidden',expectMode);

    const salaryField = $("f-salary");
    const payTypeField = $("f-payType");
    const partTimeField = $("f-partTime");
    if (salaryField) salaryField.classList.toggle("hidden", partTime);
    if (payTypeField) payTypeField.classList.toggle("hidden", !partTime);
    if (partTimeField) partTimeField.classList.toggle("hidden", !partTime);
    if ($("f-stage")) $("f-stage").classList.toggle("hidden", expectMode || partTime);
    if ($("s-salary")) $("s-salary").disabled = partTime;

    const tempFields = $("c-temp-fields");
    if (tempFields) tempFields.classList.toggle("hidden", !temp);

    const expectField = $("c-expect-field");
    if (expectField) expectField.classList.remove("hidden");
    const label = $("c-expect-label");
    if (label) label.textContent = expectMode ? "期望（必选）" : "期望上下文（可选）";
    const hint = $("c-expect-hint");
    if (hint) {
      if (temp) hint.textContent = "临时期望发 expectInfo（JSON），不发 encryptExpectId；下面几项就是它的键。";
      else if (expectMode) hint.textContent = "期望流只带筛选维度，不带关键词（站点点期望 tab 时会删掉 query）。";
      else hint.textContent = "选了期望就带上 encryptExpectId，对应站点「期望 tab + 输入关键词」。";
    }
  }

  /**
   * 拉筛选条件并填满城市/行业/各维度控件。
   *
   * @returns {Promise<Object>} 条件接口原样返回
   */
  async function loadConditions() {
    const cond = await api.get("/api/filters/conditions");
    const sourceEl = $("search-source");
    if (sourceEl) {
      const degraded = cond.degraded && Object.keys(cond.degraded).length
        ? `（部分降级：${Object.entries(cond.degraded).map(([k, v]) => `${k}=${v}`).join(", ")}）`
        : "";
      sourceEl.textContent = `筛选选项来源：${cond.source || "api"}${degraded}`;
    }
    cityPicker.setNodes(cond.city?.length ? cond.city : (cond.hotCity || []));
    tmpLocationPicker.setNodes(cond.city?.length ? cond.city : (cond.hotCity || []));
    industryPicker.setNodes(industryNodes(cond.industry || []));
    positionPicker.setNodes(cond.position || []);
    fillSelect($("s-jobType"), cond.jobType || []);
    fillSelect($("s-salary"), cond.salary || []);
    multis["s-experience"].setOptions(cond.experience || []);
    multis["s-degree"].setOptions(cond.degree || []);
    multis["s-scale"].setOptions(cond.scale || []);
    multis["s-payType"].setOptions(cond.payType || []);
    multis["s-partTime"].setOptions(cond.partTime || []);
    multis["s-stage"].setOptions(cond.stage || []);
    return cond;
  }

  /**
   * 读某个多选维度的当前值。
   *
   * @param {string} id 控件 id
   * @returns {Array<string>} 选中的 code 列表
   */
  function multiValues(id) {
    return multis[id].getValues();
  }

  /**
   * 把已存配置回填到整页控件。
   *
   * @param {Object} data 配置接口载荷（`collection` + `search`）
   */
  async function fill(data) {
    const c = data.collection || {};
    $("c-max-pages").value = c.max_pages ?? 5;
    $("c-start-page").value = c.start_page ?? 1;
    $("c-page-interval").value = c.page_interval ?? 1;
    $("c-detail-interval").value = c.detail_interval ?? 1;
    $("c-fetch-details").checked = c.fetch_details !== false;
    $("c-store-blacklisted").checked = c.store_blacklisted !== false;
    const kind = (c.expect_kind || "").trim();
    setSource(kind ? "expect" : "search");
    fillExpectSelect(expectOptions, expectSelectValue(c));
    fillTempExpectInfo(c.expect_info || "");
    updateSourceUI();
    const s = data.search || {};
    $("s-query").value = s.query || "";
    $("s-pageSize").value = s.pageSize ?? 15;
    cityPicker.setValue(s.city || "");
    industryPicker.setValue(s.industry || []);
    positionPicker.setValue(s.position || []);
    districtPicker.setValue(unpackLocations(s.multiBusinessDistrict));
    subwayPicker.setValue(unpackLocations(s.multiSubway));
    await loadLocations();
    if ($("s-jobType")) $("s-jobType").value = s.jobType || "";
    if ($("s-salary")) $("s-salary").value = s.salary || "";
    const multiMap = {
      "s-experience": s.experience || [],
      "s-degree": s.degree || [],
      "s-scale": s.scale || [],
      "s-payType": s.payType || [],
      "s-partTime": s.partTime || [],
      "s-stage": s.stage || [],
    };
    Object.entries(multiMap).forEach(([id, values]) => {
      multis[id].setValues(values || []);
    });
    updateSourceUI();
  }

  /**
   * 收集整页表单 → 保存接口的请求体。
   *
   * @returns {Object} 配置载荷
   */
  function payload() {
    const source = sourceValue();
    const picked = expectSelection();
    const partTime = source === "expect" ? picked.expect_kind === "part_time"
      : $("s-jobType").value === "1903";
    return {
      query: $("s-query").value.trim(),
      city: cityPicker.getValue(),
      jobType: $("s-jobType").value,
      salary: partTime ? "" : $("s-salary").value,
      experience: multiValues("s-experience"),
      degree: multiValues("s-degree"),
      industry: industryPicker.getValue(),
      position: source === 'search' ? positionPicker.getValue() : [],
      multiBusinessDistrict: source === 'search' ? districtPicker.getValue().join(',') : '',
      multiSubway: source === 'search' ? subwayPicker.getValue().join(',') : '',
      scale: multiValues("s-scale"),
      payType: partTime ? multiValues("s-payType") : [],
      partTime: partTime ? multiValues("s-partTime") : [],
      stage: source === "search" && !partTime ? multiValues("s-stage") : [],
      pageSize: Number($("s-pageSize").value) || 15,
      max_pages: Number($("c-max-pages").value) || 5, start_page: Number($("c-start-page").value) || 1, page_interval: Number($("c-page-interval").value) || 0,
      use_search: true,
      expect_kind: picked.expect_kind,
      encrypt_expect_id: picked.encrypt_expect_id,
      mix_expect_type: picked.mix_expect_type,
      expect_name: picked.expect_name,
      expect_info: picked.expect_info || "",
      fetch_details: $("c-fetch-details").checked, detail_interval: Number($("c-detail-interval").value) || 0, store_blacklisted: $("c-store-blacklisted").checked,
    };
  }

  /** 先定来源再拉期望列表，最后回填配置与黑名单规则。 */
  async function load() {
    try {
      await loadConditions();
      const data = await api.get("/api/collection/config");
      const c = data.collection || {};
      setSource((c.expect_kind || "").trim() ? "expect" : "search");
      await loadExpects(expectSelectValue(c));
      await fill(data);
      await loadRules();
      markDirty(false);
    } catch (err) { toast(err.message || "读取采集配置失败", "bad"); }
  }

  /** 拉黑名单规则列表并绑定测试/命中/编辑/停用/删除。 */
  async function loadRules() {
    const data = await api.get("/api/blacklist/rules");
    $("rules-list").innerHTML = (data.items || []).map((r) => `
      <div class="settings-rule flex between center gap-8 wrap" data-rule="${escapeHtml(r.rule_id)}">
        <div class="flex center gap-8 wrap" style="min-width:0"><i class="rule-dot" data-state="${r.enabled ? "on" : "off"}" title="${r.enabled ? "已启用" : "已停用"}"></i><div style="min-width:0"><strong>${escapeHtml(r.pattern)}</strong> <span class="pill static">${escapeHtml(r.rule_type)} / ${escapeHtml(r.match_mode)}</span>
          <div class="muted" style="font-size:12px">${escapeHtml(r.description || "无描述")} · 命中 ${r.hit_count || 0} 次${r.last_error ? ` · 规则错误：${escapeHtml(r.last_error)}` : ""}</div></div></div>
        <div class="btn-row"><button class="btn sm ghost rule-test">测试</button><button class="btn sm ghost rule-hits">命中详情</button><button class="btn sm ghost rule-edit">编辑</button><button class="btn sm ghost rule-toggle">${r.enabled ? "停用" : "启用"}</button><button class="btn sm danger rule-delete">删除</button></div>
      </div>`).join("") || `<div class="muted">还没有黑名单规则</div>`;
    $("rules-list").querySelectorAll(".settings-rule").forEach((row) => {
      const id = row.dataset.rule;
      const rule = (data.items || []).find((item) => item.rule_id === id);
      row.querySelector(".rule-toggle").addEventListener("click", async () => { try { await api.post(`/api/blacklist/rules/${id}/toggle`, { enabled: !rule.enabled }); await loadRules(); markDirty(false); } catch (err) { toast(err.message, "bad"); } });
      row.querySelector(".rule-delete").addEventListener("click", async () => { if (!window.confirm("删除当前规则？历史命中记录不会删除。")) return; try { await api.del(`/api/blacklist/rules/${id}`); await loadRules(); toast("规则已删除", "ok"); } catch (err) { toast(err.message, "bad"); } });
      row.querySelector(".rule-hits").addEventListener("click", async () => {
        try {
          const hits = await api.get(`/api/blacklist/rules/${id}/hits?limit=100`);
          const body = document.createElement("div");
          body.innerHTML = (hits.items || []).map((hit) => `<div class="settings-rule"><strong>${escapeHtml(hit.job_name || "未知职位")}</strong><div>${escapeHtml(hit.brand_name || "")} · ${escapeHtml(hit.matched_text || "")}</div><div class="muted">${escapeHtml(hit.action || "")} · ${escapeHtml(hit.created_at || "")}</div></div>`).join("") || `<div class="muted">暂无命中记录</div>`;
          modal({ title: `规则命中详情：${escapeHtml(rule.pattern)}`, body, wide: true });
        } catch (err) { toast(err.message, "bad"); }
      });
      row.querySelector(".rule-test").addEventListener("click", () => openRuleModal(rule, true));
      row.querySelector(".rule-edit").addEventListener("click", () => openRuleModal(rule, false));
    });
  }

  /**
   * 新增/编辑/测试黑名单规则弹窗。公司规则只有精确/正则；JD 规则多一个匹配范围。
   *
   * @param {Object}  [existing]  已有规则；空对象表示新增
   * @param {boolean} [testOnly]  只做匹配测试，不出现「保存」
   */
  function openRuleModal(existing = {}, testOnly = false) {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="field hidden"><label>规则类型</label><select class="select" id="rule-type"><option value="company">公司黑名单</option><option value="jd_keyword">JD 关键词</option></select></div>
      <div class="field"><label>规则内容</label><input class="input" id="rule-pattern" autocomplete="off"></div>
      <div class="field"><label>匹配方式</label><select class="select" id="rule-mode"><option value="exact">精确匹配</option><option value="contains">包含匹配</option><option value="regex">正则匹配</option></select>
        <div class="muted" id="rule-mode-help" style="font-size:12px"></div>
      </div>
      <div class="field" id="rule-scope-field"><label>匹配范围</label><select class="select" id="rule-scope"><option value="both">职位名 + JD 正文</option><option value="job_name">职位名</option><option value="job_desc">JD 正文</option></select></div>
      <div class="field"><label>说明</label><input class="input" id="rule-description"></div>
      <div class="field ${testOnly ? "" : "hidden"}" id="rule-test-fields"><label>测试职位名</label><input class="input" id="rule-job-name"><label>测试 JD 正文</label><textarea class="input" id="rule-job-desc" rows="4"></textarea><label>测试公司名</label><input class="input" id="rule-brand"></div>
      <div class="btn-row mt-16"><button class="btn" id="rule-validate" type="button">${testOnly ? "测试匹配" : "验证规则"}</button>${testOnly ? "" : `<button class="btn primary" id="rule-submit" type="button">保存规则</button>`}</div>
      <div class="banner info mt-12" id="rule-result">正则规则保存或启用前会在服务端验证。</div>`;
    const get = (id) => body.querySelector("#" + id);
    get("rule-type").value = existing.rule_type || "company";
    get("rule-pattern").value = existing.pattern || "";
    get("rule-mode").value = existing.match_mode || (existing.rule_type === "company" ? "exact" : "contains");
    get("rule-scope").value = existing.scope || "both";
    get("rule-description").value = existing.description || "";
    /** 按规则类型 + 匹配方式给出帮助文案。 */
    const ruleModeHelpContent = () => {
      const ruleType = get("rule-type").value;
      const ruleMode = get("rule-mode").value;
      if (ruleType === "company") {
        return ruleMode === "exact"
          ? "精确匹配：公司名标准化后全等才算命中。"
          : "正则匹配：按正则搜索公司名，保存/启用前会校验正则。";
      } else {
        return ruleMode === "contains"
          ? "包含匹配：文本里出现规则内容即命中。"
          : "正则匹配：按正则搜索职位名/JD 正文，保存/启用前会校验正则。";
      }
    }
    /** 规则类型变了就换匹配方式选项，并刷新帮助文案。 */
    const updateMode = () => {
      const company = get("rule-type").value === "company";
      get("rule-scope-field").classList.toggle("hidden", company);
      get("rule-mode").innerHTML = company
        ? `<option value="exact">精确匹配</option><option value="regex">正则匹配</option>`
        : `<option value="contains">包含匹配</option><option value="regex">正则匹配</option>`;
      if (![...get("rule-mode").options].some((option) => option.value === get("rule-mode").value)) get("rule-mode").value = company ? "exact" : "contains";
      get("rule-mode-help").textContent = ruleModeHelpContent();
      get("rule-mode").addEventListener("change", () => {
        get("rule-mode-help").textContent = ruleModeHelpContent();
      });
    };
    get("rule-type").addEventListener("change", updateMode);
    updateMode();
    const dialog = modal({ title: testOnly ? "测试黑名单规则" : (existing.rule_id ? "编辑黑名单规则" : "新增黑名单规则"), body, wide: true });
    get("rule-validate").addEventListener("click", async () => {
      try {
        const ruleType = get("rule-type").value;
        const isCompany = ruleType === "company";
        const payload = {
          rule_type: ruleType,
          pattern: get("rule-pattern").value.trim(),
          match_mode: get("rule-mode").value,
          scope: isCompany ? "" : get("rule-scope").value,
          description: get("rule-description").value.trim(),
          brand_name: get("rule-brand")?.value || "",
          job_name: get("rule-job-name")?.value || "",
          job_desc: get("rule-job-desc")?.value || "",
        };
        if (testOnly) {
          const result = await api.post("/api/blacklist/test", payload);
          get("rule-result").textContent = result.matched ? `命中 ${result.matches.length} 条` : "未命中";
        } else {
          await api.post("/api/blacklist/test", payload);
          get("rule-result").textContent = "规则格式有效，可以保存";
        }
      } catch (err) { get("rule-result").textContent = err.message || "规则无效"; }
    });
    if (!testOnly) get("rule-submit").addEventListener("click", async () => {
      try {
        const ruleType = get("rule-type").value;
        const isCompany = ruleType === "company";
        const payload = {
          rule_type: ruleType,
          pattern: get("rule-pattern").value.trim(),
          match_mode: get("rule-mode").value,
          scope: isCompany ? "" : get("rule-scope").value,
          description: get("rule-description").value.trim(),
          enabled: existing.rule_id ? existing.enabled !== false : true,
        };
        const url = existing.rule_id ? `/api/blacklist/rules/${existing.rule_id}` : "/api/blacklist/rules";
        const saved = existing.rule_id ? await api.put(url, payload) : await api.post(url, payload);
        dialog.close(); await loadRules(); toast(saved.item ? "规则已保存" : "规则已保存", "ok");
      } catch (err) { get("rule-result").textContent = err.message || "保存失败"; }
    });
  }
  $("collection-save").addEventListener("click", async () => { try { await fill(await api.put("/api/collection/config", payload())); markDirty(false); toast("采集配置已保存", "ok"); } catch (err) { toast(err.message, "bad"); } });
  $("collection-reset").addEventListener("click", async () => { if (!window.confirm("确定重置搜索条件和采集参数？")) return; try { await fill(await api.post("/api/collection/config/reset")); markDirty(false); toast("已重置", "ok"); } catch (err) { toast(err.message, "bad"); } });
  $("rule-add-company").addEventListener("click", () => openRuleModal({ rule_type: "company", match_mode: "exact", scope: "" }));
  $("rule-add-jd").addEventListener("click", () => openRuleModal({ rule_type: "jd_keyword", match_mode: "contains", scope: "both" }));
  $("rules-recheck").addEventListener("click", async () => {
    try {
      const task = await api.post("/api/blacklist/recheck", {});
      $("recheck-status").textContent = `规则重检已开始：${task.task_id}`;
      const timer = setInterval(async () => {
        try {
          const state = await api.get("/api/blacklist/recheck/status");
          const p = state.progress || {};
          $("recheck-status").textContent = state.task_id ? `规则重检：${state.status} · ${p.done || 0}/${p.total || 0}` : "";
          if (["done", "error", "cancelled"].includes(state.status)) { clearInterval(timer); await loadRules(); }
        } catch { clearInterval(timer); }
      }, 1000);
    } catch (err) { toast(err.message || "重检启动失败", "bad"); }
  });
  await load();
}
