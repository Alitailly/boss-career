/** 系统配置 → 招呼语：编辑写稿用的规则，并预览模型实际收到的完整提示词。 */

import { api } from "../api.js";
import { toast, escapeHtml } from "../ui.js";

/**
 * 「招呼语」面板：编辑写稿规则，并预览模型实际收到的完整提示词。
 *
 * 只开这一段约束；匹配 / 重生成 / 简历解析的其他提示词固定在代码里。
 *
 * @param {HTMLElement} root 挂载容器
 * @param {(v: boolean) => void} markDirty 标/清未保存修改
 * @returns {Promise<void>}
 */
export async function renderGreetingPrompt(root, markDirty) {
  root.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">招呼语</h2>
        <div class="card-sub">核对字数、语气、选材和禁语。评分与其他阶段的方法，在“当前个人方案”中查看，再和你的 AI 调整。</div>
      </div>
      <div class="btn-row">
        <button class="btn primary" id="greet-save" type="button">保存</button>
      </div>
    </div>
    <div class="settings-scroll">
      <div class="field">
        <label for="greet-req">打招呼语约束</label>
        <textarea class="input textarea" id="greet-req" rows="10" maxlength="3000" placeholder="写清楚：写成什么样、用哪些事实、字数和禁语"></textarea>
        <span class="hint">这份规则就是模型实际收到的写作要求（没有另一份隐藏默认值）。最多 3000 字，保存后下一次写稿立即生效。</span>
      </div>
      <div class="card-sub" id="greet-status"></div>
      <div class="divider"></div>
      <div class="flex between center gap-12 wrap mb-8">
        <div>
          <div class="card-sub">个人求职口径（到岗时间 / 每周天数 / 可持续时长 / 城市）</div>
          <div class="card-sub">写招呼语时只能用这里填了的内容；留空就不提这项。个人 AI 按 <code>greeting/skill-求职口径.md</code> 写同一份设置。</div>
        </div>
        <button class="btn" id="pref-save" type="button">保存口径</button>
      </div>
      <div class="wf-fields">
        <label>可以到岗时间<input class="input" id="pref-start" maxlength="40" placeholder="如 立即 / 2026-07 / 一个月内"></label>
        <label>每周可到岗天数<input class="input" id="pref-days" type="number" min="1" max="7" placeholder="留空 = 不提"></label>
        <label>可持续时长（月）<input class="input" id="pref-months" type="number" min="1" max="36" placeholder="留空 = 不提"></label>
        <label>接受异地线下<select class="input" id="pref-remote"><option value="">不设置（不提）</option><option value="true">接受</option><option value="false">不接受</option></select></label>
        <label>可接受城市<input class="input" id="pref-cities" maxlength="120" placeholder="用顿号或逗号分隔，如 上海、杭州"></label>
      </div>
      <div class="card-sub" id="pref-status"></div>
      <div class="divider"></div>
      <details class="manual-tools"><summary>完整提示词预览（供 AI 核对）</summary><div class="manual-tools-body">
      <div class="flex between center gap-12 wrap mb-8">
        <div>
          <div class="card-sub">完整提示词（模型实际收到的原文）</div>
          <div class="card-sub" id="greet-preview-sub">结构说明 + 上面这份规则</div>
        </div>
        <button class="btn sm" id="greet-preview-btn" type="button">刷新预览</button>
      </div>
      <pre class="update-log-view" id="greet-preview">点「刷新预览」加载</pre>
      </div></details>
    </div>
  `;

  const $ = (id) => root.querySelector(`#${id}`);
  const input = $("greet-req");
  let baseline = "";

  /** 回包 → 文本框与状态行。 */
  function fill(data) {
    const custom = (data && data.custom) || "";
    input.value = custom;
    baseline = custom;
    $("greet-status").textContent = custom
      ? "当前状态：已配置（模型用的就是这份）"
      : "当前状态：未配置 —— 写稿会停下，请先写一份再保存";
    markDirty(false);
  }

  /** 读当前设置。 */
  async function load() {
    try {
      fill(await api.get("/api/system/greeting-prompt"));
    } catch (err) {
      toast("读招呼语设置失败：" + (err.message || ""), "bad");
    }
  }

  input.addEventListener("input", () => {
    markDirty(input.value.trim() !== baseline.trim());
  });

  /** 拉完整提示词预览：结构和规则一起给，方便对照着改。 */
  async function loadPreview() {
    const box = $("greet-preview");
    box.textContent = "加载中…";
    try {
      const data = await api.get("/api/workflow/prompt-preview");
      if (!data.ok) {
        box.textContent = "预览失败：" + (data.error || "未知原因");
        $("greet-preview-sub").textContent = "预览不可用";
        return;
      }
      box.textContent = data.instruction;
      $("greet-preview-sub").textContent =
        `规则 ${data.requirement_chars} 字 · 全文 ${data.instruction_chars} 字 · ` +
        `上限 ${data.max_len} 字 · ${data.policy_version || ""}（保存后需点刷新预览）`;
    } catch (err) {
      box.textContent = "预览失败：" + (err.message || "");
    }
  }

  $("greet-preview-btn").addEventListener("click", () => loadPreview());

  $("greet-save").addEventListener("click", async () => {
    if (!input.value.trim()) {
      toast("规则不能为空：写稿要用它，空着会直接停下", "bad");
      return;
    }
    try {
      const saved = await api.put("/api/system/greeting-prompt", {
        requirement: input.value,
      });
      fill(saved);
      await loadPreview();
      toast("招呼语约束已保存", "ok");
    } catch (err) {
      toast(err.message || "保存失败", "bad");
    }
  });

  await load();
  await loadPreview();

  // ---- 个人求职口径：写成设置，不塞进简历事实；留空就是"这次不提" ----
  let prefsVersion = "";
  const prefsText = (data) => {
    const facts = (data && data.facts) || [];
    return facts.length
      ? "会写进招呼语：" + facts.map(f => f.text).join("；")
      : "现在什么都不提：招呼语里不会出现到岗时间、每周天数、可持续时长、城市和异地。";
  };
  function fillPrefs(data) {
    const p = (data && data.preferences) || {};
    prefsVersion = p.version || "";
    $("pref-start").value = p.start || "";
    $("pref-days").value = p.days_per_week ?? "";
    $("pref-months").value = p.months ?? "";
    $("pref-remote").value = p.remote === true ? "true" : p.remote === false ? "false" : "";
    $("pref-cities").value = (p.cities || []).join("、");
    $("pref-status").textContent = prefsText(data) + (p.updated_at ? `（保存于 ${p.updated_at}，${p.updated_by || "panel"}）` : "");
  }
  async function loadPrefs() {
    try {
      fillPrefs(await api.get("/api/workflow/control/preferences"));
    } catch (err) {
      $("pref-status").textContent = "读个人口径失败：" + (err.message || "");
    }
  }
  $("pref-save").addEventListener("click", async () => {
    const digits = (id, low, high, label) => {
      const raw = $(id).value.trim();
      if (!raw) return null;
      const n = Number(raw);
      if (!Number.isInteger(n) || n < low || n > high) throw new Error(`${label}要在 ${low}–${high} 之间，留空表示不提`);
      return n;
    };
    try {
      const body = {
        expected_version: prefsVersion,
        start: $("pref-start").value.trim(),
        days_per_week: digits("pref-days", 1, 7, "每周到岗天数"),
        months: digits("pref-months", 1, 36, "可持续时长"),
        remote: $("pref-remote").value === "" ? null : $("pref-remote").value === "true",
        cities: $("pref-cities").value.split(/[、,，;；\s]+/).filter(Boolean),
        updated_by: "panel",
      };
      const saved = await api.put("/api/workflow/control/preferences", body);
      fillPrefs(saved);
      toast("个人口径已保存；已写好的稿子需要重写", "ok");
    } catch (err) {
      toast(err.message || "保存失败", "bad");
    }
  });
  await loadPrefs();
}
