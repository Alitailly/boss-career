/** 同一份本地评分索引供概览和列表使用；不触发评分或平台访问。 */
import { api } from '../api.js';

let cached = null, cachedAt = 0, inflight = null;
export async function readScoreIndex(force = false) {
  if (inflight) return inflight;
  if (!force && cached && Date.now() - cachedAt < 30000) return cached;
  inflight = api.get('/api/match/index');
  try {
    const data = await inflight;
    if (!data || !data.items || typeof data.items !== 'object' || Array.isArray(data.items)) throw new Error('评分索引格式不完整');
    cached = data; cachedAt = Date.now(); return data;
  } finally { inflight = null; }
}

export function hasValidScore(m = {}) {
  const raw = m.match_score;
  return raw !== null && raw !== undefined && raw !== '' && typeof raw !== 'boolean'
    && Number.isFinite(Number(raw)) && Number(raw) >= 0 && Number(raw) <= 100
    && !['failed','hard_filtered'].includes(m.status);
}

export function summarizeScores(index, threshold) {
  if (!index?.items) return {analyzed:null, high:null, low:null, review:null, ungraded:null};
  const validThreshold = threshold !== null && threshold !== undefined && threshold !== '' && Number.isFinite(Number(threshold));
  let analyzed = 0, high = 0, review = 0;
  for (const row of Object.values(index.items)) {
    const m = row.item || {}, raw = m.match_score;
    if (!hasValidScore(m)) continue;
    analyzed++;
    if (validThreshold && Number(raw) >= Number(threshold)) {
      high++;
      if (m.status === 'needs_review') review++;
    }
  }
  return {analyzed, high:validThreshold ? high : null, low:validThreshold ? analyzed-high : null, review:validThreshold ? review : null, ungraded:Object.keys(index.items).length-analyzed};
}
