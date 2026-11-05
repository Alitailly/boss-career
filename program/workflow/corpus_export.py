#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地语料库：把抓到的岗位（含 JD 全文）按日期归档到磁盘，长期保留。

磁盘够用，所以**不做每日清理**，只增量合并。目录结构：

    corpus/
      README.md                 说明
      index.jsonl               一行一个岗位的当前快照（机器读，按 job_id 去重）
      by-date/2026-10-29.json   当天**首次入库**岗位的完整记录（含 JD 全文）
      by-date/2026-10-29.md     同一天的人读清单（按城市分组）
      stats.json                各日期条数、JD 完整率、匹配/发送状态统计

写入策略（双写，不许只留一侧）：
- **库是源头，磁盘是同一批数据的归档副本**。采集器每轮先看库里岗位总数，一变就在**同一轮**
  把新增岗位同步落盘；发送轮的补货抓取结束后也补一次。不接受“只在库里”“只在盘里”。
- `first_seen` 一旦写入就固定；内容真变了才更新 `last_seen`（时间戳只表示“最近一次内容变化”），
  所以内容没变时文件不重写，mtime 可以当作“上次真变了”的依据。
- 只追加，不删除；同一天重跑是幂等的（覆盖当天文件）。
- 只读程序接口，不写数据库、不发消息。
- `python corpus_export.py --verify` 对照库与盘：列出“只在库 / 只在盘”的条数，非 0 退出码 1。

用法：
  python corpus_export.py              # 全量同步一次
  python corpus_export.py --dry-run    # 只统计，不写盘
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from workflow_env import CODE_ROOT, workspace_root

HERE = workspace_root()
sys.path.insert(0, str(CODE_ROOT))
import deliver_round_v2 as dr  # noqa: E402

# 目录可以用环境变量顶掉：离线测试必须指到临时目录，绝不能写生产语料库。
CORPUS = Path(os.environ.get("BOSS_CORPUS_DIR") or (HERE / "corpus"))
INDEX = CORPUS / "index.jsonl"
BY_DATE = CORPUS / "by-date"
STATS = CORPUS / "stats.json"


def iter_jobs(page: int = 200, api=None) -> list[dict]:
    """分页取库里全部岗位（只读）。api 可注入，测试时传桩，避免打到真实接口。"""
    call = api or dr.api
    out: list[dict] = []
    offset = 0
    while True:
        batch = call("GET", f"/api/jobs?limit={page}&offset={offset}", timeout=60).get("items") or []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


def load_index() -> dict[str, dict]:
    """读已有索引；坏行跳过并报错（不整体丢弃）。"""
    if not INDEX.exists():
        return {}
    out: dict[str, dict] = {}
    bad = 0
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        jid = str(rec.get("job_id") or "")
        if jid:
            out[jid] = rec
    if bad:
        print(f"索引里有 {bad} 行坏数据，已跳过（原文件未改）")
    return out


def row_from_job(job: dict) -> dict:
    """把接口返回的岗位压成一条语料记录（字段名保持可读）。"""
    desc = str(job.get("job_desc") or "")
    return {
        "job_id": str(job.get("encrypt_job_id") or ""),
        "title": str(job.get("job_name") or ""),
        "company": str(job.get("brand_name") or ""),
        "salary": str(job.get("salary_desc") or ""),
        "degree": str(job.get("job_degree") or ""),
        "experience": str(job.get("job_experience") or ""),
        "city": str(job.get("city_name") or ""),
        "district": "·".join(x for x in (job.get("area_district"), job.get("business_district")) if x),
        "industry": str(job.get("brand_industry") or ""),
        "scale": str(job.get("brand_scale_name") or ""),
        "jd_status": str(job.get("jd_status") or ""),
        "jd_len": len(desc),
        "jd_sha": dr.jd_sha(desc) if desc else "",
        "jd_text": desc,
        "page": job.get("page"),
        "first_imported_at": str(job.get("detail_fetched_at") or ""),
        "last_greeted_at": str(job.get("last_greeted_at") or ""),
    }


_EMPTY_VALUES = (None, "", [], {}, 0, False)


def _stable(rec: dict) -> dict:
    """判断“内容是否真的变了”用的规范化记录：去掉时间戳，空值等同于缺字段。

    老索引里没有的字段，接口会补成空串；这种差别不算变化，否则每次同步都会重写全盘。
    """
    return {k: v for k, v in rec.items()
            if k != "last_seen" and not any(v == empty for empty in _EMPTY_VALUES)}


def merge_records(old: dict[str, dict], jobs: list[dict],
                  today: str) -> tuple[dict[str, dict], int, int]:
    """增量合并：保留 first_seen，内容变了才更新 last_seen；返回 (新索引, 新增, 更新)。"""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    merged = dict(old)
    added = updated = 0
    for job in jobs:
        rec = row_from_job(job)
        jid = rec["job_id"]
        if not jid:
            continue
        prev = merged.get(jid)
        if prev:
            rec["first_seen"] = prev.get("first_seen") or today
            if _stable(rec) == _stable(prev):
                merged[jid] = prev  # 内容没变：原样保留，磁盘不抖动
                continue
            rec["last_seen"] = now
            updated += 1
        else:
            added += 1
            # 首次建库时尽量用岗位自己的入库时间，而不是全算成今天
            stamped = str(rec.get("first_imported_at") or "")[:10]
            rec["first_seen"] = stamped if len(stamped) == 10 else today
            rec["last_seen"] = now
        merged[jid] = rec
    return merged, added, updated


def render_md(day: str, records: list[dict]) -> str:
    """人读清单：按城市分组，列公司/岗位/日薪/学历/经验/JD 字数。"""
    lines = [f"# {day} 采集岗位（{len(records)} 个）", ""]
    by_city: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_city[r.get("city") or "未知城市"].append(r)
    for city in sorted(by_city, key=lambda c: (-len(by_city[c]), c)):
        rows = sorted(by_city[city], key=lambda r: (r.get("company") or "", r.get("title") or ""))
        lines += [f"## {city}（{len(rows)} 个）", "",
                  "| 公司 | 岗位 | 日薪 | 学历 | 经验 | JD字数 | 状态 |",
                  "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r.get('company','')} | {r.get('title','')} | {r.get('salary','')} | "
                f"{r.get('degree','')} | {r.get('experience','')} | {r.get('jd_len',0)} | "
                f"{r.get('jd_status','')} |"
            )
        lines.append("")
    return "\n".join(lines)


def write_readme() -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    _write_if_changed(CORPUS / "README.md",
        "# 本地语料库\n\n"
        "抓到的岗位都留在这里，**不按天清理**。库（data/boss.db）与这里**双写同步**：\n"
        "采集器看到库里新增就同一轮落盘，发送轮补货、手动抓取跑完也会补一次。\n\n"
        "| 路径 | 内容 |\n|---|---|\n"
        "| `index.jsonl` | 一行一个岗位的快照（含 JD 哈希、首次/最近入库时间），按 job_id 去重 |\n"
        "| `by-date/YYYY-MM-DD.json` | 当天**首次入库**岗位的完整记录（含 JD 全文） |\n"
        "| `by-date/YYYY-MM-DD.md` | 同一天的人读清单，按城市分组 |\n"
        "| `stats.json` | 各日期条数、JD 完整率、已发送统计 |\n\n"
        "重建：`python corpus_export.py`（只读程序接口，幂等，可随时重跑）。\n"
        "对账：`python corpus_export.py --verify`（只在库/只在盘都必须为 0）。\n",
    )


def _write_if_changed(path: Path, text: str) -> bool:
    """内容一样就不重写：少抖磁盘，也让 mtime 只表示“真变了”。"""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    return True


def write_outputs(records: dict[str, dict]) -> dict:
    """把索引落盘，并按 first_seen 生成每天的文件与统计。"""
    CORPUS.mkdir(parents=True, exist_ok=True)
    BY_DATE.mkdir(parents=True, exist_ok=True)
    write_readme()

    body = "".join(json.dumps(records[jid], ensure_ascii=False) + "\n" for jid in sorted(records))
    _write_if_changed(INDEX, body)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for rec in records.values():
        by_day[str(rec.get("first_seen") or "unknown")].append(rec)

    for day, rows in by_day.items():
        _write_if_changed(BY_DATE / f"{day}.json",
                          json.dumps(rows, ensure_ascii=False, indent=1))
        _write_if_changed(BY_DATE / f"{day}.md", render_md(day, rows))

    greets = Counter("已打招呼" if r.get("last_greeted_at") else "未打招呼" for r in records.values())
    with_jd = sum(1 for r in records.values() if r.get("jd_len"))
    stats = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_jobs": len(records),
        "with_jd": with_jd,
        "jd_ratio": round(with_jd / len(records), 3) if records else 0,
        "by_day": {d: len(v) for d, v in sorted(by_day.items())},
        "by_city": dict(Counter(r.get("city") or "?" for r in records.values()).most_common(10)),
        **greets,
    }
    _write_if_changed(STATS, json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


def verify(api=None, records: dict[str, dict] | None = None) -> dict:
    """对照库与盘：两个方向都查，任一侧多出来就是没同步。"""
    jobs = iter_jobs(api=api)
    db_ids = {str(j.get("encrypt_job_id") or "") for j in jobs}
    db_ids.discard("")
    idx = load_index() if records is None else records
    disk_ids = set(idx)
    only_db = sorted(db_ids - disk_ids)
    only_disk = sorted(disk_ids - db_ids)
    return {
        "db_total": len(db_ids),
        "disk_total": len(disk_ids),
        "only_in_db": len(only_db),
        "only_on_disk": len(only_disk),
        "only_in_db_ids": only_db[:20],
        "only_on_disk_ids": only_disk[:20],
        "with_jd_db": sum(1 for j in jobs if str(j.get("job_desc") or "").strip()),
        "with_jd_disk": sum(1 for r in idx.values() if r.get("jd_len")),
        "ok": not only_db and not only_disk,
    }


def sync(api=None) -> dict:
    """库里一有新增就调用：增量同步语料库，返回统计（含库/盘两侧条数）。"""
    today = dt.date.today().isoformat()
    jobs = iter_jobs(api=api)
    db_ids = {str(j.get("encrypt_job_id") or "") for j in jobs}
    db_ids.discard("")
    old = load_index()
    merged, added, updated = merge_records(old, jobs, today)
    stats = write_outputs(merged)
    stats["added"] = added
    stats["updated"] = updated
    stats["db_total"] = len(db_ids)
    stats["disk_total"] = len(merged)
    stats["missing_on_disk"] = len(db_ids - set(merged))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--verify", action="store_true", help="对照库与盘，非 0 即退出码 1")
    args = ap.parse_args()

    if args.verify:
        rep = verify()
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0 if rep["ok"] else 1

    today = dt.date.today().isoformat()
    jobs = iter_jobs()
    old = load_index()
    merged, added, updated = merge_records(old, jobs, today)
    print(f"接口返回 {len(jobs)} 个岗位；索引原有 {len(old)} 个；"
          f"本次新增 {added} 个、更新 {updated} 个；合计 {len(merged)} 个")
    if args.dry_run:
        return 0
    stats = write_outputs(merged)
    print("已写入：", CORPUS)
    print(json.dumps({k: stats[k] for k in ("total_jobs", "with_jd", "jd_ratio", "by_day")},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
