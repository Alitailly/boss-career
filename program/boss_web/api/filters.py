"""筛选条件：选项表 + 库里的搜索条件读写。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from requests import RequestException

from boss_filter import (
    get_filter_conditions,
    load_search_filter,
    save_search_filter,
)
from boss_filter.search import JobSearchFilter

router = APIRouter()


class SearchFilterBody(BaseModel):
    """与库里 `doc('search_filter')` 同款字段（空 = 不传给平台）。

    Attributes:
        encryptExpectId: 期望上下文（期望 + 关键词）；空 = 不带。
    """

    query: str = ""
    city: str = ""
    jobType: str = Field("", alias="jobType")
    salary: str = ""
    experience: list[str] = Field(default_factory=list)
    degree: list[str] = Field(default_factory=list)
    industry: list[str] = Field(default_factory=list)
    scale: list[str] = Field(default_factory=list)
    payType: list[str] = Field(default_factory=list)
    partTime: list[str] = Field(default_factory=list)
    stage: list[str] = Field(default_factory=list)
    position: list[str] = Field(default_factory=list)
    multiBusinessDistrict: str = ""
    multiSubway: str = ""
    encryptExpectId: str = ""
    pageSize: int = Field(15, ge=1, le=50)

    model_config = {"populate_by_name": True, "extra": "forbid"}


@router.get("/conditions")
def conditions(refresh: bool = Query(False)) -> dict[str, Any]:
    """程序内置/本地选项表；仅显式 refresh=true 才进行在线维护。"""
    from boss_filter.validation import cached_catalog
    doc = cached_catalog(refresh=refresh)
    data = dict(doc["options"])
    from boss_filter.precision import positions
    data['position'] = positions()
    from boss_filter.validation import web_selection
    data["selection"] = web_selection(doc)
    data.update(fetched_at=doc.get("fetched_at"), catalog_note=doc.get("note"),
                refresh_after=doc.get("refresh_after"), catalog_storage=doc.get("storage"))

    if "city" in data and isinstance(data["city"], list):
        data["city"] = _remove_district_level(data["city"])

    return data


@router.get('/locations', operation_id='filter_locations')
def location_conditions(city: str = Query(...), refresh: bool = Query(False)) -> dict[str, Any]:
    """City-specific public catalog. Offline by default; refresh is explicit maintenance."""
    from boss_filter.precision import locations, refresh_locations, location_options
    from boss_filter.validation import FilterValidationError
    try:
        doc = refresh_locations(city) if refresh else locations(city)
        return {**doc, 'multiBusinessDistrict': location_options(doc['city'], 'multiBusinessDistrict'),
                'multiSubway': location_options(doc['city'], 'multiSubway')}
    except RequestException as exc:
        from ..errors import UpstreamError
        raise UpstreamError('公共位置目录读取失败，已保留原目录；本次不重试') from exc
    except (ValueError, OSError) as exc:
        raise FilterValidationError('city', city, str(exc)) from exc


def _remove_district_level(city_tree: list[dict]) -> list[dict]:
    """去掉城市树中市级节点下的区级子节点。

    保留省→市两级结构，市级节点的 children 被清空。
    前端城市选择器只允许选择市级代码。
    """
    result = []
    for province in city_tree:
        province_copy = {**province}
        if "children" in province_copy and isinstance(province_copy["children"], list):
            cities = []
            for city in province_copy["children"]:
                city_copy = {**city}
                if "children" in city_copy:
                    del city_copy["children"]
                cities.append(city_copy)
            province_copy["children"] = cities
        result.append(province_copy)
    return result


@router.get("/search")
def get_search() -> dict[str, Any]:
    """读库里的搜索条件。"""
    f = load_search_filter()
    return _filter_payload(f)


@router.put("/search")
def put_search(body: SearchFilterBody) -> dict[str, Any]:
    """写库里的搜索条件。"""
    f = JobSearchFilter(
        query=body.query,
        city=body.city,
        job_type=body.jobType,
        salary=body.salary,
        experience=tuple(body.experience),
        degree=tuple(body.degree),
        industry=tuple(body.industry),
        scale=tuple(body.scale),
        pay_type=tuple(body.payType),
        part_time=tuple(body.partTime),
        stage=tuple(body.stage),
        position=tuple(body.position),
        multi_business_district=body.multiBusinessDistrict,
        multi_subway=body.multiSubway,
        encrypt_expect_id=body.encryptExpectId.strip(),
        page_size=body.pageSize,
    )
    f.to_params()  # 先校验当前菜单分支，再保存；不产生平台请求。
    path = save_search_filter(f)
    return {**_filter_payload(load_search_filter()), "path": str(path)}


@router.post("/search/reset")
def reset_search() -> dict[str, Any]:
    """把搜索条件重置为默认（空）。"""
    f = JobSearchFilter()
    path = save_search_filter(f)
    return {**_filter_payload(f), "path": str(path)}


def _filter_payload(f: JobSearchFilter) -> dict[str, Any]:
    """把搜索条件转成前端字段名的字典。"""
    return {
        "query": f.query,
        "city": f.city,
        "jobType": f.job_type,
        "salary": f.salary,
        "experience": list(f.experience),
        "degree": list(f.degree),
        "industry": list(f.industry),
        "scale": list(f.scale),
        "payType": list(f.pay_type),
        "partTime": list(f.part_time),
        "stage": list(f.stage),
        "position": list(f.position),
        "multiBusinessDistrict": f.multi_business_district,
        "multiSubway": f.multi_subway,
        "encryptExpectId": f.encrypt_expect_id,
        "pageSize": f.page_size,
        "is_blank": f.is_blank,
        "summary": _summary(f),
    }


def _summary(f: JobSearchFilter) -> str:
    """拼一句人类可读的筛选摘要。"""
    parts = []
    if f.query:
        parts.append(f"关键词「{f.query}」")
    if f.city:
        parts.append(f"城市 {f.city}")
    if f.salary:
        parts.append(f"薪资 {f.salary}")
    for label, values in (
        ("经验", f.experience),
        ("学历", f.degree),
        ("行业", f.industry),
        ("规模", f.scale),
    ):
        if values:
            parts.append(f"{label} {'/'.join(values)}")
    return " · ".join(parts) if parts else "未设置平台条件（空参数不发送，请确认平台默认行为）"
