#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探测 / 记录 BOSS 招聘者的「活跃时间」。

BOSS 职位详情里有两个字段（2026-10-29 实测）：

  zpData.bossInfo.activeTimeDesc   "刚刚活跃" / "今日活跃" / "3日内活跃" …
  zpData.brandComInfo.activeTime   最近活跃的毫秒时间戳（更精确，用它）

程序只把 JD 正文写库，这两个字段被丢掉了，所以这里单独抓一遍，落到
`greeting/activity.json`：{job_id: {"active_at": 秒级时间戳, "desc": "刚刚活跃",
"checked_at": "..."}}。`deliver_round_v2.collect_eligible()` 会读这个文件做活跃筛选。

只发 GET，只读；不发消息、不改库。

用法：
  python probe_activity.py --eligible            # 给当前准入名单补活跃时间
  python probe_activity.py --ids <id>[,<id>...]  # 指定岗位
  python probe_activity.py --eligible --dry-run  # 只看要探几个
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from workflow_env import CODE_ROOT, app_source, workspace_root

HERE = workspace_root()
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(app_source()))

ACTIVITY = HERE / "greeting" / "activity.json"


def load_activity() -> dict:
    if not ACTIVITY.exists():
        return {}
    try:
        return json.loads(ACTIVITY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_activity(data: dict) -> None:
    ACTIVITY.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVITY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(ACTIVITY)


def active_at_of(raw: dict) -> tuple[float | None, str]:
    """从详情 payload 里取最近活跃时间（秒）与文字描述。"""
    zp = (raw or {}).get("zpData") or {}
    desc = str(((zp.get("bossInfo") or {}).get("activeTimeDesc")) or "")
    ms = ((zp.get("brandComInfo") or {}).get("activeTime")) or 0
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        ms = 0.0
    sec = ms / 1000.0 if ms > 1e11 else (ms or 0.0)
    if not sec and desc:
        sec = parse_desc(desc)
    return (sec or None), desc


def parse_desc(desc: str) -> float:
    """把「3日内活跃」这类文字折成近似时间戳；认不出就回 0。"""
    now = time.time()
    table = {
        "刚刚": 0.02, "在线": 0.02, "今日": 0.3, "今天": 0.3,
        "3日": 3.0, "三日": 3.0, "本周": 5.0, "7日": 7.0, "一周": 7.0,
        "2周": 14.0, "两周": 14.0, "半月": 15.0, "本月": 25.0, "30日": 30.0, "一月": 30.0,
    }
    for key, days in table.items():
        if key in desc:
            return now - days * 86400
    return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--eligible", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--interval", type=float, default=1.2)
    ap.add_argument("--refresh-days", type=float, default=1.0,
                    help="已有的活跃记录超过这么多天就重探")
    args = ap.parse_args()

    import deliver_round_v2 as dr  # 放在这里，避免 --help 也要连程序

    cfg = json.loads(dr.CFG_PATH.read_text(encoding="utf-8"))
    resume_id = str(cfg.get("resume_id") or "")
    min_score = int(cfg.get("min_score") or 80)

    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    titles: dict[str, str] = {}
    if args.eligible or not ids:
        dr.ACTIVITY_GATE = False
        for it in dr.collect_eligible(min_score, resume_id):
            ids.append(it["job_id"])
            titles[it["job_id"]] = f"{it.get('company')} / {it.get('title')}"
    print(f"待探活跃：{len(ids)} 个")
    if args.dry_run:
        return 0
    if not ids:
        return 0

    data = load_activity()
    now = time.time()
    todo = []
    for jid in ids:
        rec = data.get(jid) or {}
        checked = float(rec.get("checked_at_ts") or 0)
        if now - checked < args.refresh_days * 86400 and rec:
            continue
        todo.append(jid)
    print(f"需要探：{len(todo)} 个（其余 {len(ids) - len(todo)} 个记录还新鲜）")

    from boss_jobs import create_client
    from boss_jobs.store import JobStore

    client = create_client()
    store = JobStore()
    ok = fail = 0
    for i, jid in enumerate(todo, 1):
        job = store.get_job(jid)
        if job is None:
            print(f"  [{i}/{len(todo)}] {jid} 库里没有，跳过")
            fail += 1
            continue
        try:
            detail = client.fetch_job_detail(security_id=job.security_id, lid=job.lid)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(todo)}] {jid} 抓详情失败：{exc}")
            fail += 1
            time.sleep(args.interval)
            continue
        sec, desc = active_at_of(detail.raw)
        if sec:
            data[jid] = {
                "active_at": sec,
                "desc": desc,
                "checked_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "checked_at_ts": time.time(),
                "brand": str(job.brand_name or ""),
                "title": str(job.job_name or ""),
            }
            ok += 1
            age_h = (time.time() - sec) / 3600
            print(f"  [{i}/{len(todo)}] {job.brand_name} / {job.job_name}: {desc or '-'} "
                  f"（约 {age_h:.1f} 小时前）")
        else:
            fail += 1
            print(f"  [{i}/{len(todo)}] {jid} 没有活跃字段（desc={desc!r}）")
        time.sleep(args.interval)

    save_activity(data)
    print(f"\n完成：成功 {ok} / 失败 {fail}，文件 {ACTIVITY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
