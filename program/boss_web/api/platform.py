"""Local request control state; these endpoints never contact BOSS."""
from typing import Literal
from fastapi import APIRouter, Query
from pydantic import BaseModel
from boss_jobs.request_control import get_control

router = APIRouter()
Scope = Literal['all', 'account', 'search', 'details', 'send', 'chat', 'token', 'other']


class PauseBody(BaseModel):
    scope: Scope = 'account'


class ResumeBody(BaseModel):
    confirmed: Literal[True]
    scope: Scope = 'all'


@router.get('/control')
def status():
    return {'ok': True, **get_control().status()}


@router.get('/diagnostics')
def diagnostics(hours: int = Query(24, ge=1, le=168), limit: int = Query(100, ge=1, le=1000)):
    return get_control().diagnostics(hours=hours, limit=limit)


@router.post('/pause')
def pause(body: PauseBody):
    get_control().pause('account' if body.scope == 'all' else body.scope)
    return status()


@router.post('/resume')
def resume(body: ResumeBody):
    # Explicit local acknowledgement only; no automatic platform test or queued work.
    get_control().resume(body.scope)
    return status()
