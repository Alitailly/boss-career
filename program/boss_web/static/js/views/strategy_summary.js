/** 当前方案只读摘要；不写任何配置，也不检测外部模型连接。 */
import { mountStageInputs } from './stage_inputs.js';

export async function renderStrategySummary(root) {
  const stages = ['matching','greeting','resume_revision','interview'];
  root.innerHTML = `<h2 class="card-title">当前采用的个人方案</h2><p class="card-sub mb-16">展开查看各阶段实际模板与条件。这里显示当前采用内容，具体结果保留在各阶段页面。</p>${stages.map(stage => `<div data-input-stage="${stage}"></div>`).join('')}<a class="btn sm" href="#/settings?tab=ai">查看程序模型与 API →</a>`;
  const disposers = await Promise.all(stages.map(stage => mountStageInputs(root.querySelector(`[data-input-stage="${stage}"]`),stage)));
  return () => disposers.forEach(dispose => dispose());
}
