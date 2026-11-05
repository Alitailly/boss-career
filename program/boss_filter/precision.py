"""Public position/location catalogs. Ordinary reads and validation stay offline."""
from __future__ import annotations

import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import requests

LOCATION_FIELDS = ('multiBusinessDistrict', 'multiSubway')
_lock = threading.Lock()
_last_request: float | None = None


@lru_cache(maxsize=1)
def _bundled() -> dict:
    return json.loads(Path(__file__).with_name('precision_catalog.json').read_text(encoding='utf-8'))


def positions() -> list[dict]:
    return _bundled()['positions']


def position_options() -> dict[str, str]:
    # The web selector selects third-level occupations; headings are navigation only.
    return {leaf['code']: leaf['name'] for top in positions()
            for group in top.get('children', []) for leaf in group.get('children', [])}


def _path(city: str) -> Path:
    from .validation import catalog_path
    return catalog_path().parent / 'platform-locations' / f'{city}.json'


def locations(city: str) -> dict:
    from .validation import resolve
    city = resolve('city', city)
    if not city or city == '100010000':
        raise ValueError('精细位置需要指定一个具体城市')
    try:
        doc = json.loads(_path(city).read_text(encoding='utf-8'))
        if doc.get('city') == city and isinstance(doc.get('districts'), list) and isinstance(doc.get('subways'), list):
            return {**doc, 'storage': 'local'}
    except (OSError, ValueError):
        pass
    bundled = _bundled().get('locations', {}).get(city)
    return {**bundled, 'storage': 'builtin'} if bundled else {
        'city': city, 'districts': [], 'subways': [], 'source': 'missing', 'storage': 'missing',
        'note': '该城市精细位置目录尚未保存；可显式维护一次，日常读取不会访问平台。'}


def _tree(rows: list) -> list[dict]:
    result = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get('code') is None or not row.get('name'):
            continue
        node = {'code': str(row['code']), 'name': row['name']}
        if row.get('subLevelModelList'):
            node['children'] = _tree(row['subLevelModelList'])
        result.append(node)
    return result


def refresh_locations(city: str) -> dict:
    """Explicit maintenance: two public requests, no cookies/retries, 5s apart, 1h cooldown."""
    global _last_request
    from .validation import resolve
    city = resolve('city', city)
    if not city or city == '100010000':
        raise ValueError('精细位置需要指定一个具体城市')
    with _lock:
        old = locations(city)
        if time.time() - (old.get('fetched_at') or 0) < 3600:
            return old
        stamp = _path(city).with_suffix('.attempt')
        try:
            attempted = float(stamp.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            attempted = 0
        if time.time() - attempted < 3600:
            raise ValueError('本城市目录最近已尝试维护；请保留现有目录，满1小时后再维护')
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()), encoding='utf-8')
        session = requests.Session()
        session.trust_env = False
        session.headers['User-Agent'] = 'Mozilla/5.0'
        out = []
        try:
            for index, endpoint in enumerate(('/wapi/zpgeek/businessDistrict.json',
                                              '/wapi/zpCommon/data/getSubwayByCity')):
                if _last_request is not None:
                    time.sleep(max(0, 5 - (time.monotonic() - _last_request)))
                _last_request = time.monotonic()
                response = session.get('https://www.zhipin.com' + endpoint,
                                       params={'cityCode': city}, timeout=8)
                session.cookies.clear()
                response.raise_for_status()
                payload = response.json()
                if payload.get('code') != 0 or not isinstance(payload.get('zpData'), dict):
                    raise ValueError('平台未返回有效精细位置目录；保留原目录，不自动重试')
                out.append(payload['zpData'])
        finally:
            session.close()
        district = out[0].get('businessDistrict')
        if not isinstance(district, dict) or str(district.get('code')) != city or not isinstance(out[1].get('subwayList'), list):
            raise ValueError('平台目录结构或城市不符，未覆盖原目录')
        doc = {'city': city, 'city_name': district.get('name', ''),
               'districts': _tree(district.get('subLevelModelList') or []),
               'subways': _tree(out[1]['subwayList']), 'source': 'api', 'fetched_at': time.time()}
        path = _path(city)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
        return {**doc, 'storage': 'local'}


def location_options(city: str, field: str) -> list[dict]:
    key = 'districts' if field == 'multiBusinessDistrict' else 'subways'
    result = []
    for parent in locations(city)[key]:
        result.append({'code': parent['code'], 'name': parent['name'], 'children': [
            {'code': f"{parent['code']}:{child['code']}", 'name': child['name']}
            for child in parent.get('children', [])]})
    return result


def normalize_location(field: str, value: Any, city: str) -> str:
    from .validation import FilterValidationError
    if not isinstance(value, str):
        raise FilterValidationError(field, value, '需要平台位置组合字符串，或目录中的精确名称')
    if not value.strip() or value.strip() in ('0', '不限'):
        return ''
    if not city:
        # Plans may put the city in target.cities; compile_search validates after binding it.
        return value.strip()
    if city == '100010000':
        raise FilterValidationError(field, value, '全国搜索不能使用单个城市的精细位置')
    groups = location_options(city, field)
    if not groups:
        raise FilterValidationError(field, value, '该城市目录尚未保存或没有此类选项，请显式维护目录后选择')
    aliases: dict[str, list[str]] = {}
    parents = {}
    for group in groups:
        parents[group['code']] = {c['code'].split(':')[1]: c['name'] for c in group['children']}
        for row in [group, *group['children']]:
            aliases.setdefault(row['name'], []).append(row['code'])
    if value in aliases:
        hits = aliases[value]
        if len(hits) != 1:
            raise FilterValidationError(field, value, '名称重复，请使用目录中带父级的位置代码')
        value = hits[0]
    selected = {}
    for token in value.split(','):
        parts = token.strip().split(':')
        parent = parts[0]
        if parent not in parents or len(parts) > 2:
            raise FilterValidationError(field, value, '不在该城市目录中，请按父级:子项_子项格式选择')
        children = list(dict.fromkeys(parts[1].split('_'))) if len(parts) == 2 and parts[1] else []
        if any(child not in parents[parent] for child in children):
            raise FilterValidationError(field, value, '子项不属于所选区/线路，或代码不在本地目录中')
        # Repeated parent entries are merged; whole parent takes precedence.
        if parent not in selected:
            selected[parent] = children
        elif not selected[parent] or not children:
            selected[parent] = []
        else:
            selected[parent] = list(dict.fromkeys(selected[parent] + children))
    if sum(len(children) or 1 for children in selected.values()) > 9:
        raise FilterValidationError(field, value, '当前网页最多选择9个位置项')
    return ','.join(parent + (':' + '_'.join(children) if children else '')
                    for parent, children in selected.items())


def location_label(city: str, field: str, value: str) -> str:
    groups = {row['code']: row for row in location_options(city, field)}
    labels = []
    for token in value.split(','):
        parent, _, children = token.partition(':')
        group = groups.get(parent)
        if not group:
            labels.append(token)
            continue
        names = {row['code'].split(':')[1]: row['name'] for row in group['children']}
        labels.append(group['name'] + (' / ' + '、'.join(names.get(x, x) for x in children.split('_')) if children else '（全部）'))
    return '；'.join(labels)
