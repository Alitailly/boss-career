/** 系统配置 → 工作区：首次启动时创建/绑定用户数据目录。 */

import { api } from "../api.js";
import { toast, escapeHtml } from "../ui.js";

/** 工作区值来源 → 人话。 */
const SOURCE_LABEL = {
  env: "环境变量 BOSS_WORKFLOW_DIR",
  saved: "上次设置（launch.json）",
  default: "默认目录",
};

/** 代码目录来源 → 人话。 */
const CODE_SOURCE_LABEL = {
  env: "环境变量 BOSS_WORKFLOW_CODE",
  saved: "上次设置（launch.json）",
  state: "工作区目录本身",
  bundled: "程序自带的 workflow/",
  none: "没找到",
};

/**
 * 「工作区」面板：显示现状、允许选目录并创建/绑定，成功后提示下一步。
 *
 * 只调 `/api/system/workspace` 两个接口：它们在工作区还没配置时也能正常响应，
 * 所以这个卡片必须是首次启动就能用的一张卡片。
 *
 * @param {HTMLElement} root 挂载容器
 * @returns {Promise<void>}
 */
export async function renderWorkspace(root) {
  root.innerHTML = `
    <div class="flex between center gap-12 wrap mb-16">
      <div>
        <h2 class="card-title">工作区</h2>
        <div class="card-sub">简历、求职方案、台账都放这个目录里。换目录不会删旧数据，只是让程序改看另一份。</div>
      </div>
      <div class="btn-row">
        <button class="btn" id="ws-refresh" type="button">重新检测</button>
        <button class="btn primary" id="ws-create" type="button">创建/绑定</button>
      </div>
    </div>
    <div class="settings-scroll">
      <div class="banner info" id="ws-status">正在读取…</div>
      <div class="row-2">
        <div class="field">
          <label for="ws-path">工作区目录</label>
          <input class="input" id="ws-path" autocomplete="off" placeholder="留空 = 用默认目录">
          <span class="hint" id="ws-default"></span>
        </div>
        <div class="field">
          <label for="ws-code">代码目录</label>
          <input class="input" id="ws-code" autocomplete="off" placeholder="留空 = 自动查找">
          <span class="hint" id="ws-code-hint"></span>
        </div>
      </div>
      <div class="divider"></div>
      <div class="card-sub mb-8">下一步</div>
      <div id="ws-checks"></div>
    </div>
  `;
  const $ = (id) => root.querySelector(`#${id}`);

  /**
   * 回包 → 状态条、输入框、清单。
   *
   * @param {Object} state `/api/system/workspace` 的回包
   */
  function fill(state) {
    const configured = Boolean(state && state.configured);
    const ready = Boolean(state && state.ready);
    const stage = (state && state.stage) || (configured ? "configured" : "empty");
    const missing = ((state && state.missing) || [])
      .map((item) => item.label || item.id || "")
      .filter(Boolean);
    const box = $("ws-status");
    if (!configured) {
      box.className = "banner warn";
      box.textContent = `还没有工作区。留空点「创建/绑定」就会建在默认目录；也可以填一个已有目录。`;
    } else if (stage === "executable") {
      box.className = "banner info";
      box.textContent = `工作区可执行：${state.dir}（来自：${SOURCE_LABEL[state.source] || state.source}）`;
    } else if (!ready) {
      box.className = "banner bad";
      box.textContent = `工作区已配置但目录打不开：${state.dir}`;
    } else {
      // 建好了 ≠ 能跑：以前这里写"已就绪"，用户接着点采集/发送才发现动不了
      box.className = "banner warn";
      box.textContent = `工作区已建立，但阶段还跑不了${missing.length ? `：缺 ${missing.join("、")}` : ""}。看下面的「下一步」。`;
    }
    const path = $("ws-path");
    const showDir = configured || (state && state.source !== "default");
    if (path) path.value = showDir ? (state.dir || "") : "";
    $("ws-default").textContent = `默认：${(state && state.default_dir) || ""}（留空就用它）`;
    $("ws-code").value = (state && state.code_dir) || "";
    const scripts = (state && state.scripts) || {};
    const lacking = Object.entries(scripts).filter(([, ok]) => !ok).map(([name]) => name);
    if (state && state.code_dir) {
      $("ws-code-hint").textContent = lacking.length
        ? `来源：${CODE_SOURCE_LABEL[state.code_source] || state.code_source} · 缺脚本：${lacking.join("、")}`
        : `来源：${CODE_SOURCE_LABEL[state.code_source] || state.code_source} · 脚本齐全`;
    } else {
      $("ws-code-hint").textContent =
        "没找到工作流脚本目录；填一个含 deliver_round_v2.py 的目录，或留空让它自动找。";
    }

    const checks = (state && state.checks) || [];
    $("ws-checks").innerHTML = checks.length
      ? checks.map((item) => `
        <div class="flex between center gap-8 wrap mb-8" data-check="${escapeHtml(item.id || "")}">
          <div style="min-width:0">
            <strong>${escapeHtml(item.label || item.id || "")}</strong>
            <div class="muted" style="font-size:12px">${escapeHtml(item.message || "")}${
              item.action ? ` · ${escapeHtml(item.action)}` : ""
            }</div>
          </div>
          <span class="pill static">${item.ok ? "已就绪" : "待办"}</span>
        </div>`).join("")
      : `<div class="muted">暂时读不到清单，可以点「重新检测」再试。</div>`;
  }

  /** 读当前状态。 */
  async function load() {
    try {
      fill(await api.get("/api/system/workspace"));
    } catch (err) {
      $("ws-status").className = "banner bad";
      $("ws-status").textContent = "读取工作区状态失败：" + (err.message || err);
    }
  }

  $("ws-refresh").addEventListener("click", () => load());

  $("ws-create").addEventListener("click", async () => {
    const path = $("ws-path").value.trim();
    const code = $("ws-code").value.trim();
    const button = $("ws-create");
    button.disabled = true;
    button.textContent = "处理中…";
    try {
      const saved = await api.post("/api/system/workspace", { path, code, confirmed: true });
      fill(saved);
      const ws = saved && saved.dir ? `：${saved.dir}` : "";
      const ok = saved && saved.stage === "executable";
      toast(
        ok
          ? `工作区可执行${ws}。下一步：导入简历 / 设置求职目标`
          : `工作区已绑定${ws}${saved && saved.changed ? "（新建）" : "（原有数据未改动）"}。`
            + `下一步：补齐「工作流代码」，否则采集/发送跑不了`,
        ok ? "ok" : "warn",
      );
    } catch (err) {
      toast(err.message || "创建工作区失败", "bad");
    } finally {
      button.disabled = false;
      button.textContent = "创建/绑定";
    }
  });

  await load();
}
