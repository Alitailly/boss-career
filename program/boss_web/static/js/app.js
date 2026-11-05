/** 星舰控制台入口：hash 路由 + 舰桥状态 */

import { api } from "./api.js";
import { toast, bindThemeToggle, initTheme } from "./ui.js";
import { maybePromptUpdate, startUpdateWatcher } from "./update.js";
import { renderHome } from "./views/home.js";
import { installPanelFeedback } from './panel_ui.js';
import { renderLogin } from "./views/login.js";
import { renderJobs } from "./views/jobs.js";
import { renderResume, renderResumeOptimization } from "./views/resume.js";
import { renderMatch } from "./views/match.js";
import { renderInterview } from "./views/interview.js";
import { renderSettings } from "./views/settings.js";
import { renderReview, renderTasks, renderCandidate, renderExecution, renderFeedback, renderActivity } from "./views/workflow_pages.js";

/** hash 路径 → 视图渲染函数。 */
const routes = {
  "": renderHome,
  "/": renderHome,
  "/login": renderLogin,
  "/jobs": renderJobs,
  "/resume": renderResume,
  "/resume/optimize": renderResumeOptimization,
  "/match": renderMatch,
  "/workflow": renderHome,
  "/review": renderReview,
  "/tasks": renderTasks,
  "/feedback": renderFeedback,
  "/activity": renderActivity,
  "/candidate": renderCandidate,
  "/execution": renderExecution,
  "/interview": renderInterview,
  "/settings": renderSettings,
  "/llm": renderSettings,
};

/** 上一视图的清理函数，换页前调用。 */
let disposePrev = null;
let navigationVersion = 0;

function disposeView(dispose) {
  if (typeof dispose !== 'function') return;
  try { dispose(); } catch (err) { console.error(err); }
}

/** 从 `location.hash` 取路径（去掉 query）。 */
function currentPath() {
  const h = location.hash.replace(/^#/, "") || "/";
  return h.split("?")[0];
}

/** 路径前缀 → 舰桥阶段 key；不在流程里返回 ""。 */
function stageOf(path) {
  if (path.startsWith("/login")) return "login";
  if (path.startsWith("/jobs")) return "jobs";
  if (path.startsWith("/resume")) return "resume";
  if (path.startsWith("/match")) return "match";
  if (path.startsWith("/review")) return "review";
  if (path.startsWith("/tasks")) return "tasks";
  if (path.startsWith("/feedback")) return "feedback";
  if (path.startsWith("/activity")) return "activity";
  if (path.startsWith("/candidate")) return "resume";
  if (path.startsWith("/execution")) return "settings";
  if (path.startsWith("/workflow")) return "home";
  if (path.startsWith("/interview")) return "interview";
  if (path.startsWith("/settings") || path.startsWith("/llm")) return "settings";
  return "home";
}

/** 把 `#stages` 里当前阶段的链接标成 `.active`。 */
function paintStages(path) {
  const active = stageOf(path);
  const label = document.getElementById('workspace-page-label');
  const labels = {home:'求职总览',resume:'简历与资料',tasks:'岗位采集',match:'匹配评分',review:'招呼语与发送',feedback:'每日复盘',interview:'面试准备',jobs:'本地岗位库',activity:'任务记录',settings:'AI 与设置',login:'平台账号'};
  if (label) label.textContent = labels[active] || '求职总览';
  document.querySelectorAll("#stages a").forEach((a) => {
    a.classList.toggle("active", a.dataset.stage === active);
    if (a.dataset.stage === active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

/** 按 hash 渲染当前视图，换页前先调用上一视图的 dispose。 */
export async function navigate() {
  const version = ++navigationVersion;
  const path = currentPath();
  paintStages(path);
  closeSidebar();
  const previousDispose = disposePrev;
  disposePrev = null;
  disposeView(previousDispose);
  const previousView = document.getElementById("view");
  if (!previousView) return;
  // 异步渲染仍会查询自己的DOM；切页只移除旧容器，不清空它的子节点。
  const view = previousView.cloneNode(false);
  previousView.replaceWith(view);
  view.innerHTML = `<div class="empty"><div class="empty-icon">◌</div><p>正在接入…</p></div>`;
  const render = routes[path] || renderHome;
  try {
    const dispose = await render(view);
    if (version !== navigationVersion) {
      disposeView(dispose);
      return;
    }
    disposePrev = dispose;
  } catch (err) {
    if (version !== navigationVersion) return;
    console.error(err);
    view.innerHTML = `<div class="empty"><div class="empty-icon">⚠</div><p>${err.message || err}</p></div>`;
  }
  window.scrollTo({ top: 0, behavior: 'auto' });
}

/** 刷新舰桥会话信号球，并广播 `boss:session` 给首页卡片。 */
export async function refreshBridge() {
  const dot = document.getElementById("signal-session");
  const label = document.getElementById("signal-session-label");
  try {
    const st = await api.get("/api/auth/status");
    window.dispatchEvent(new CustomEvent("boss:session", { detail: st }));
    if (st.logged_in) {
      dot.className = "signal ok";
      label.textContent = st.phone_masked || "已登录";
    } else {
      dot.className = "signal warn";
      label.textContent = "未登录";
    }
    if (st.task && ["pending", "sending_sms", "need_code", "need_slider", "logging_in"].includes(st.task.status)) {
      dot.className = "signal busy";
      label.textContent = "登录进行中";
    }
  } catch {
    dot.className = "signal bad";
    label.textContent = "离线";
  }
}

/**
 * 还没配置工作区时，把首屏直接带到「系统配置 → 工作区」。
 *
 * 只从首页跳，且只在 `configured === false` 时跳：读不到状态（离线、接口挂了）
 * 就留在首页，别因为一次探测失败把人拦在门外。
 */
async function gotoWorkspaceIfNeeded() {
  const path = currentPath();
  if (path !== "/" && path !== "") return;
  try {
    const state = await api.get("/api/system/workspace");
    if (state && state.configured === false) {
      location.hash = "#/settings?tab=workspace";
    }
  } catch {
    /* 探测失败不影响正常浏览 */
  }
}

window.addEventListener("hashchange", navigate);
function closeSidebar() {
  document.body?.classList.remove('sidebar-open');
  const sidebar = document.getElementById('workspace-sidebar');
  const compact = window.matchMedia?.('(max-width: 720px)')?.matches;
  if (sidebar) { sidebar.inert = Boolean(compact); sidebar.setAttribute('aria-hidden', String(Boolean(compact))); }
  const content = document.getElementById('view');
  if (content) content.inert = false;
  document.getElementById('sidebar-toggle')?.setAttribute('aria-expanded', 'false');
  const scrim = document.getElementById('sidebar-scrim');
  if (scrim) scrim.hidden = true;
}
window.addEventListener("load", () => {
  initTheme();
  bindThemeToggle(document.getElementById("theme-toggle"));
  installPanelFeedback();
  window.matchMedia?.('(max-width: 720px)')?.addEventListener('change', closeSidebar);
  document.getElementById('sidebar-toggle')?.addEventListener('click', () => {
    const open = document.body.classList.toggle('sidebar-open');
    document.getElementById('sidebar-toggle').setAttribute('aria-expanded', String(open));
    document.getElementById('sidebar-scrim').hidden = !open;
    const sidebar = document.getElementById('workspace-sidebar');
    sidebar.inert = !open;
    sidebar.setAttribute('aria-hidden', String(!open));
    document.getElementById('view').inert = open;
  });
  document.getElementById('sidebar-scrim')?.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) { closeSidebar(); document.getElementById('sidebar-toggle')?.focus(); }
  });
  document.querySelector('.skip-link')?.addEventListener('click', event => { event.preventDefault(); document.getElementById('view')?.focus(); });
  navigate();
  gotoWorkspaceIfNeeded();
  refreshBridge();
  setInterval(refreshBridge, 8000);
  maybePromptUpdate().catch(() => { });
  startUpdateWatcher();
});

export { toast, api };
