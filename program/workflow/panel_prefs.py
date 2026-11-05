#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读写「个人求职口径」：给个人 AI / 命令行用，和网页设置是同一份数据。

只改你显式传的字段，其余保持原样；不传任何参数 = 查看当前口径。

用法：
    python panel_prefs.py
    python panel_prefs.py --start 立即 --days 5 --months 6 --remote yes --cities 上海,杭州
    python panel_prefs.py --clear

口径只影响招呼语里怎么写可用时间；留空 = 不提这项，不会替你猜。
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from workflow_env import api_base

BASE = api_base()
PATH = "/api/workflow/control/preferences"
FIELDS = ("start", "days_per_week", "months", "cities", "remote")


def fail(message: str) -> int:
    print(f"[失败] {message}")
    return 1


def fetch() -> dict:
    resp = requests.get(BASE + PATH, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("message") or "读取个人口径失败")
    return data


def save(body: dict) -> dict:
    resp = requests.put(BASE + PATH, json=body, timeout=15)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("message") or resp.json().get("detail") or ""
        except ValueError:
            detail = resp.text[:200]
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("message") or "保存个人口径失败")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 / 修改个人求职口径（招呼语里只写这里填了的内容）")
    parser.add_argument("--start", help="可以到岗的时间，如 立即 / 2027-07")
    parser.add_argument("--days", type=int, help="每周可到岗天数 1-7")
    parser.add_argument("--months", type=int, help="可实习时长（月）1-36")
    parser.add_argument("--remote", choices=["yes", "no", "unset"], help="是否接受异地线下")
    parser.add_argument("--cities", help="可接受城市，逗号分隔；空串 = 清空")
    parser.add_argument("--clear", action="store_true", help="全部清空（招呼语不再提到岗安排）")
    parser.add_argument("--json", dest="payload", help="直接给一段 JSON（覆盖其它参数）")
    parser.add_argument("--dry-run", action="store_true", help="只看将要写入的内容，不保存")
    args = parser.parse_args()

    try:
        current = fetch()
    except Exception as exc:  # noqa: BLE001 - 命令行要给人话
        return fail(f"连不上程序接口 {BASE}：{exc}")

    value = current.get("preferences") or {}
    changers: dict = {}

    if args.payload:
        try:
            changers = json.loads(args.payload)
        except ValueError as exc:
            return fail(f"--json 不是合法 JSON：{exc}")
        if not isinstance(changers, dict):
            return fail("--json 必须是对象")
    else:
        if args.clear:
            changers = {"start": "", "days_per_week": None, "months": None,
                        "cities": [], "remote": None}
        if args.start is not None:
            changers["start"] = args.start.strip()
        if args.days is not None:
            changers["days_per_week"] = args.days
        if args.months is not None:
            changers["months"] = args.months
        if args.remote is not None:
            changers["remote"] = {"yes": True, "no": False, "unset": None}[args.remote]
        if args.cities is not None:
            changers["cities"] = [x.strip() for x in args.cities.replace("，", ",").split(",") if x.strip()]

    if not changers:
        facts = current.get("facts") or []
        print(f"当前口径（版本 {value.get('version') or '—'}"
              + (f"，保存于 {value.get('updated_at')}" if value.get("updated_at") else "") + "）：")
        print(f"  到岗时间：{value.get('start') or '（不提）'}")
        print(f"  每周到岗：{value.get('days_per_week') or '（不提）'}")
        months = value.get("months")
        print(f"  实习时长：{months} 个月" if months else "  实习时长：（不提）")
        print(f"  异地线下：{'接受' if value.get('remote') is True else '不接受' if value.get('remote') is False else '（不提）'}")
        print(f"  可接受城市：{'、'.join(value.get('cities') or []) or '（不提）'}")
        print(f"  写进招呼语：{'；'.join(f['text'] for f in facts) or '（什么都不提）'}")
        return 0

    body = {"expected_version": value.get("version") or ""}
    for key in FIELDS:
        body[key] = changers.get(key, value.get(key))
    body["updated_by"] = "cli"

    if args.dry_run:
        print("将写入（未保存）：" + json.dumps(body, ensure_ascii=False, indent=2))
        return 0

    try:
        saved = save(body)
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc))

    prefs = saved.get("preferences") or {}
    print("已保存个人口径，版本 " + str(prefs.get("version") or "—"))
    print("写进招呼语：" + ("；".join(f["text"] for f in (saved.get("facts") or [])) or "（什么都不提）"))
    print("提示：已写好的旧稿会因口径变化被判为需要重写。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
