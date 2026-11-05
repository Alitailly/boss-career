"""Whitelisted, identity-bound facts from a job-detail response."""
from typing import Any

from .errors import JobDataError
from .models import clean_text, clean_salary, extract_job_desc


def extract(payload: dict, expected_id: str, *, require_identity: bool = False) -> dict[str, Any]:
    data = payload.get('zpData') or payload.get('data') or {}
    info = data.get('jobInfo') or {}
    returned_id = str(info.get('encryptId') or info.get('encryptJobId') or '')
    if returned_id and returned_id != expected_id:
        raise JobDataError('岗位详情ID与待投岗位不一致，停止使用该详情')
    if not returned_id:
        if require_identity:
            raise JobDataError('最新详情缺少岗位ID，不能确认与待投岗位相同')
        return {}
    values = {}
    for destination, sources in {
        'job_name': ('jobName',), 'salary_desc': ('salaryDesc',),
        'job_experience': ('experienceName', 'jobExperience'),
        'job_degree': ('degreeName', 'jobDegree'), 'city_name': ('cityName',),
    }.items():
        for source in sources:
            if isinstance(info.get(source), str) and info[source].strip():
                values[destination] = clean_salary(info[source]) if destination == 'salary_desc' else clean_text(info[source])
                break
    brand = data.get('brandComInfo') or {}
    if brand.get('brandName'):
        values['brand_name'] = clean_text(brand['brandName'])
    if isinstance(info.get('jobType'), int):
        values['job_type'] = info['jobType']
    desc = extract_job_desc(payload)
    if desc:
        values['job_desc'] = desc
    return values
