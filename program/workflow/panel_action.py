"""Fixed actions used by the panel executor; shares existing workflow implementations."""
import argparse
import json
from pathlib import Path
import sys
import time

from workflow_env import app_source
sys.path.insert(0, str(app_source()))
import deliver_round_v2 as dr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--action', choices=['collect', 'match', 'feedback'], required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--stop-file', required=True)
    args = parser.parse_args()
    request = Path(args.request).resolve()
    stop = Path(args.stop_file).resolve()
    allowed = (dr.RUNS / 'panel-jobs').resolve()
    if not request.is_relative_to(allowed) or not stop.is_relative_to(allowed):
        raise RuntimeError('任务输入必须位于 runs/panel-jobs')
    payload = json.loads(request.read_text(encoding='utf-8'))
    if dr.load_profile().get('version') != payload['profile_version']:
        raise RuntimeError('求职资料已变化，请重新创建任务')
    if stop.exists():
        return 8
    if args.action == 'collect':
        import collector as col
        with dr.Lock('dsh-boss-collector-1'):
            state = col.load_state()
            try:
                note = col.one_cycle(state, col.load_cfg(), dr.load_config()['resume_id'])
                col.heartbeat(state, note)
                print(note)
            finally:
                col.save_state(state)
        return 8 if stop.exists() else 0
    if args.action == 'feedback':
        import feedback_sync
        sys.argv = [str(Path(feedback_sync.__file__)), '--max-jobs', str(int(payload.get('limit') or 20))]
        return feedback_sync.main()
    # 补分任务会把「当前简历还没打分」的岗位 id 放进 payload；没有就用空列表（= 匹配后端自选范围）
    job_ids = [str(x) for x in (payload.get('job_ids') or []) if str(x).strip()]
    # 任务创建时冻结的方案/快照：执行时对不上就拒绝，不拿"当前活动方案"顶替
    fingerprint = str(payload.get('snapshot_fingerprint') or '')
    if fingerprint:
        state = dr.snapshot_state()
        if str(state.get('fingerprint') or '') != fingerprint:
            raise RuntimeError('方案或资料在任务创建后变过，这个任务不能再用旧快照执行；请重新创建任务')
    request = {'resume_id': dr.load_config()['resume_id'], 'job_ids': job_ids}
    if int(payload.get('limit') or 0):
        # 上限必须跟着请求走：否则 job_ids 为空时后端会按"全库"重新选一遍
        request['limit'] = int(payload['limit'])
    if payload.get('plan_id'):
        request['plan_id'] = str(payload['plan_id'])
    task = dr.api('POST', '/api/match/analyze', request)
    task_id = task.get('task_id')
    if not task_id:
        raise RuntimeError('未取得匹配任务编号')
    cancel_sent = False
    while True:
        state = dr.api('GET', '/api/match/analyze/' + task_id)
        if state.get('task_id') != task_id:
            raise RuntimeError('匹配任务归属发生变化')
        if state.get('status') in {'done', 'error', 'cancelled'}:
            print(json.dumps({'task_id': task_id, 'status': state['status'],
                              'progress': state.get('progress'), 'error': state.get('error')}, ensure_ascii=False))
            return 0 if state['status'] == 'done' else 8 if state['status'] == 'cancelled' else 7
        if stop.exists() and not cancel_sent:
            dr.api('POST', '/api/match/analyze/' + task_id + '/cancel')
            cancel_sent = True
        time.sleep(1)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except dr.Busy as exc:
        print('已有同类任务运行：' + str(exc))
        raise SystemExit(5)
    except Exception as exc:
        print(f'任务失败：{type(exc).__name__}: {exc}')
        raise SystemExit(7)
