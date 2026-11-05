"""Local option lookup and validation. No network on save, compile, or search."""
from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
from typing import Any

from .fallback import build_fallback_conditions

SCALARS = ('city', 'jobType', 'salary', 'multiBusinessDistrict', 'multiSubway')
MULTIPLE = ('experience', 'degree', 'industry', 'scale', 'payType', 'partTime', 'stage', 'position')
_catalog_lock = threading.Lock()
REFRESH_COOLDOWN = 60 * 60


class FilterValidationError(ValueError):
    def __init__(self, field: str, value: Any, message: str):
        self.field = field
        endpoint = '/api/filters/locations?city=实际城市代码' if field in ('multiBusinessDistrict','multiSubway','city_filters') else '/api/filters/conditions'
        super().__init__(f'{field}：{message}（收到 {value!r}）；请读取 {endpoint} 后选择')


def catalog_path() -> Path:
    from .config import DEFAULT_DB_PATH
    base = os.environ.get('BOSS_APP_DATA')
    root = Path(base) if base else Path(os.environ.get('BOSS_DB') or DEFAULT_DB_PATH).parent
    return root / 'platform-filter-options.json'


def read_catalog() -> dict[str, Any]:
    # A maintained local override takes precedence over the shipped public catalog.
    for path, storage in ((catalog_path(), 'local'), (Path(__file__).with_name('option_catalog.json'), 'builtin')):
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(doc, dict) and isinstance(doc.get('options'), dict):
                return {**doc, 'storage': storage}
        except (OSError, ValueError):
            continue
    return {'options': build_fallback_conditions().to_dict(), 'fetched_at': None,
            'storage': 'reference',
            'note': '程序参考选项，尚未在线核实；不能称为平台最新选项'}


def save_catalog(options: dict[str, Any]) -> dict[str, Any]:
    # Only public option tables, never request headers or authentication data.
    allowed = ('city', 'hotCity', 'jobType', 'salary', *MULTIPLE, 'source', 'degraded')
    doc = {'options': {k: options[k] for k in allowed if k in options},
           'fetched_at': time.time(), 'note': '最近一次平台读取尝试；source/degraded 标明在线或参考选项'}
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)
    return doc


def cached_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """日常只读程序目录，不自动联网；refresh=True 是显式维护操作。

    每次最多读取三个公共元数据接口，不带账号 Cookie，不重试，间隔至少5秒。
    手动刷新受一小时冷却约束；目录年龄不会触发普通读取联网。
    """
    with _catalog_lock:
        doc = read_catalog()
        age = time.time() - (doc.get('fetched_at') or 0)
        if refresh is True and age >= REFRESH_COOLDOWN:
            from .client import FilterClient
            client = FilterClient(retries=0, timeout=6, request_interval=5,
                                  endpoints={'recommend_conditions': '', 'search_condition': ''})
            options = client.get_filter_conditions().to_dict()
            doc = {**save_catalog(options), 'storage': 'local'}
        return {**doc, 'refresh_after': (doc.get('fetched_at') or 0) + REFRESH_COOLDOWN}


def option_map(kind: str, catalog: dict[str, Any] | None = None) -> dict[str, str]:
    if kind == 'position':
        from .precision import position_options
        return {'0': '不限', **position_options()}
    data = (catalog or read_catalog())['options']
    result = {}
    def walk(rows):
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if row.get('code') is not None and row.get('name'):
                result[str(row['code'])] = str(row['name'])
            for key in ('options', 'children'):
                if isinstance(row.get(key), list):
                    walk(row[key])
    if kind == 'city':
        # Province/district nodes are not single-city search options.
        walk(data.get('hotCity'))
        for province in data.get('city') or []:
            for city in province.get('children') or []:
                if city.get('code') is not None and city.get('name'):
                    result[str(city['code'])] = str(city['name'])
    else:
        walk(data.get(kind))
    if kind in MULTIPLE:
        result.setdefault('0', '不限')
    return result


def resolve(kind: str, value: Any, catalog: dict[str, Any] | None = None) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise FilterValidationError(kind, value, '需要平台代码或精确名称')
    text = str(value).strip()
    if not text:
        return ''
    options = option_map(kind, catalog)
    if text in options:
        return text
    matches = [code for code, name in options.items() if name == text
               or (kind == 'city' and name.removesuffix('市') == text.removesuffix('市'))]
    if len(matches) == 1:
        return matches[0]
    raise FilterValidationError(kind, value, '选项不明确' if matches else '不在当前选项表中，不能按不限发送')


def normalize_fields(data: dict[str, Any], catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog = catalog or read_catalog()
    out = dict(data)
    for key in SCALARS:
        if key in data:
            if key in ('multiBusinessDistrict', 'multiSubway'):
                from .precision import normalize_location
                out[key] = normalize_location(key, data[key], out.get('city') or '')
            else:
                out[key] = resolve(key, data[key], catalog)
    for key in MULTIPLE:
        if key not in data:
            continue
        values = data[key]
        if not isinstance(values, (list, tuple)):
            raise FilterValidationError(key, values, '需要数组，不能用逗号字符串代替')
        codes = list(dict.fromkeys(resolve(key, v, catalog) for v in values if v is not None and v != ''))
        if '0' in codes and len(codes) > 1:
            raise FilterValidationError(key, values, '不限不能与具体选项同时选择')
        out[key] = codes
    return out


def web_selection(catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """当前网页关键词入口的菜单契约；不是所有客户端的功能总表。

    2026-11-04 官方 job~1/getConfigFields 与 job~0/筛选组件核对：
    兼职切换为 payType/partTime；融资阶段在关键词搜索的 Y 字段集合中，兼职隐藏。
    """
    types = option_map('jobType', catalog)
    internship = next((code for code, name in types.items() if name in ('实习', '实习生')), None)
    common = ['city', 'jobType', 'position', 'multiBusinessDistrict', 'multiSubway', 'industry', 'experience', 'degree', 'scale']
    return {'client': 'desktop_web', 'entry': 'keyword_search', 'revision': 'desktop-search/2026-11-05',
            'mobile_equivalence': 'unverified', 'daily_to_monthly_conversion': 'unverified',
            'profiles': {
                'regular': {'fields': common + ['salary', 'stage']},
                'internship': {'fields': common + ['salary', 'stage'], 'job_type_code': internship,
                               'note': '岗位性质只用实际返回的选项；在校生不等于实习，未提供实习专选时如实说明'},
                'part_time': {'fields': common + ['payType', 'partTime'],
                              'note': '兼职使用结算方式/时段；日结不是日薪金额区间'}},
            'not_integrated': [],
            'location_options': 'GET /api/filters/locations?city=城市代码：仅本地目录；refresh=true 为显式在线维护，普通读取不联网',
            'location_scope': '精细位置绑定具体城市；多城市使用 search.city_filters 分别保存；areaBusiness 为网页链接别名，程序统一使用 multiBusinessDistrict',
            'salary_options': '使用本次返回的 salary 名称及代码；若有日薪选项则直接采用，不自行折算月薪',
            'first_step': '采集前主动向用户介绍当前入口可选条件及相关选项，再按用户选择生成配置；代码校验是兜底'}


def validate_search_context(fields: dict[str, Any]) -> None:
    """合法枚举仍需属于当前菜单分支，避免提交网页已经移除的条件。"""
    def chosen(key):
        value = fields.get(key)
        return any(str(v) not in ('', '0') for v in value) if isinstance(value, (list, tuple)) else value not in (None, '', '0')
    for key in ('multiBusinessDistrict', 'multiSubway'):
        if chosen(key) and fields.get('city') in (None, '', '100010000'):
            raise FilterValidationError(key, fields[key], '精细位置需要绑定一个具体城市')
    if fields.get('jobType') == '1903':
        if chosen('stage'):
            raise FilterValidationError('stage', fields['stage'], '当前网页兼职入口不显示融资阶段筛选')
        if chosen('salary'):
            raise FilterValidationError('salary', fields['salary'], '当前网页兼职入口使用结算方式和兼职时段，不提供该薪资筛选')
    else:
        for key in ('payType', 'partTime'):
            if chosen(key):
                raise FilterValidationError(key, fields[key], '当前网页仅在兼职 jobType=1903 时启用此条件')


def audit(plan: dict[str, Any], bases: list[dict[str, Any]]) -> dict[str, Any]:
    catalog = read_catalog()
    options = catalog['options']
    target = plan.get('target') or {}
    raw = (plan.get('search') or {}).get('base') or {}
    aliases = {'city': 'cities', 'industry': 'industries', 'scale': 'company_scales',
               'jobType': 'employment_types'}
    rows = []
    for key in SCALARS + MULTIPLE:
        choice = target.get(aliases.get(key, '')) or {}
        values = list(dict.fromkeys(v for b in bases for v in (
            b.get(key, []) if isinstance(b.get(key), list) else [b.get(key, '')]) if v != ''))
        state = '已配置平台条件' if values and values != ['0'] else (
            '已明确不限' if choice.get('mode') == 'any' or values == ['0'] else '未限制，尚无明确选择记录')
        source = 'target' if choice.get('mode') in ('any', 'list') else 'search.base' if key in raw else 'not_set'
        if key == 'salary' and not values and any((plan.get('salary') or {}).get(k) is not None for k in ('min', 'max')):
            state, source = '采集后判断个人薪资区间', 'salary'
        if key == 'jobType' and not values and choice.get('mode') == 'list':
            state = '采集后判断岗位性质（平台无对应单选）'
        names = [option_map(key, catalog).get(str(v), str(v)) for v in values]
        if key in ('multiBusinessDistrict', 'multiSubway') and values:
            from .precision import location_label
            if any(key in fields for fields in ((plan.get('search') or {}).get('city_filters') or {}).values()):
                source = 'search.city_filters'
            names = list(dict.fromkeys(
                option_map('city', catalog).get(b['city'], b['city']) + ' · ' + location_label(b['city'], key, b[key])
                for b in bases if b.get(key)))
        rows.append({'field': key, 'state': state, 'codes': values,
                     'names': names,
                     'source': source})
    salary = plan.get('salary') or {}
    notes = ['本人学历和在校身份不自动等同于岗位搜索要求。',
             '空字段不会作为参数发送；平台可能采用默认行为，不能据此报告为用户已选择不限。']
    if salary.get('min') is not None or salary.get('max') is not None:
        notes.append('个人薪资区间在采集后判断，不自动转换为平台月薪档。')
    if (target.get('employment_types') or {}).get('mode') == 'list':
        notes.append('实习/全职等个人岗位性质要求仍需采集后核对，不能用在校生筛选代替。')
    return {'rows': rows, 'notes': notes, 'city_batches': len(bases),
            'catalog_source': options.get('source', 'fallback'),
            'catalog_degraded': options.get('degraded') or {},
            'catalog_fetched_at': catalog.get('fetched_at'),
            'catalog_note': catalog.get('note')}
