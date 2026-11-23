import logging
from datetime import date, timedelta
from sqlalchemy import and_, func, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain import staffing
from src.domain.analysis import analyze
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule, StaffingStep
from src.models.source import Dataset
from src.services.audit import record
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author))


MAX_ATTEMPTS = 3
MAX_RESUMED_ATTEMPTS = 40


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        saved = 0
        if job.kind == 'staffing':
            saved = db.scalar(select(func.count()).select_from(StaffingStep)
                              .where(StaffingStep.run_id == job.id)) or 0
        # 落过检查点的排班推算继续算，计入续算次数而不消耗重试次数；
        # 无检查点的中断仍按 MAX_ATTEMPTS 收口，续算自身也有上限防止空转。
        if saved >= 1:
            if job.resumes >= MAX_RESUMED_ATTEMPTS:
                job.status = Status.FAILED
                job.error = '续算次数过多，请检查输入后重新发起'
                job.finished_at = stamp
                record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
                return None
            job.resumes += 1
        elif job.attempt >= MAX_ATTEMPTS:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        else:
            job.attempt += 1
        job.status = Status.RUNNING
        job.lease_until = stamp + timedelta(minutes=3)
        records = db.get(Dataset, job.dataset_id).rows if job.dataset_id else None
        frozen = (job.params or {}).get('basis') if job.kind == 'staffing' else None
        return {
            'id': job.id, 'attempt': job.attempt, 'kind': job.kind,
            'rows': records, 'params': job.params, 'basis': frozen,
        }


def _budget():
    return SPEC.get('staffing', {}).get('budget') or 2


def _staffing(lease, progress):
    """排班推算：逐单元计算并落检查点，返回 (结果, 是否还有未完成单元)。

    中断后只补算未完成的单元；结果只在全部单元完成后一次写入，
    因此重试不会生成重复结果或重复回执。
    """
    info = staffing.plan(lease['basis'], lease['params']['counts'], lease['params']['tolerance'])
    model = info['model']
    done = dict(progress)
    if not info['feasible']:
        if 'conflicts' not in done:
            done['conflicts'] = staffing.compute_unit(model, 'conflicts')
            _save_step(lease, 'conflicts', done['conflicts'])
        return staffing.assemble(model, False, done), False
    budget = _budget()
    computed = 0
    for unit in staffing.units(model, True):
        if unit in done:
            continue
        if computed >= budget:
            return None, True
        done[unit] = staffing.compute_unit(model, unit)
        _save_step(lease, unit, done[unit])
        computed += 1
    return staffing.assemble(model, True, done), False


def execute(lease):
    """执行一次领取的计算；返回 True 表示还有待续算的工作单元。"""
    job_id, attempt = lease['id'], lease['attempt']
    result = export = failure = None
    more = False
    try:
        if lease['kind'] == 'staffing':
            result, more = _staffing(lease, _load_progress(job_id))
            if more:
                _release(lease)
                return True
        else:
            result = analyze(lease['rows'], lease['params'])
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return False
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient, state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
    return False


def _load_progress(job_id):
    with Session() as db:
        return {step.unit: step.payload for step in
                db.scalars(select(StaffingStep).where(StaffingStep.run_id == job_id))}


def _save_step(lease, unit, payload):
    """保存一个检查点、刷新租约，并把运行放回队列以便继续下一个单元。"""
    with Session.begin() as db:
        job = db.get(Run, lease['id'], with_for_update=True)
        if job.attempt != lease['attempt'] or job.status != Status.RUNNING:
            return
        exists = db.scalar(select(StaffingStep.id).where(StaffingStep.run_id == job.id,
                                                         StaffingStep.unit == unit))
        if not exists:
            db.add(StaffingStep(run_id=job.id, unit=unit, payload=payload))
        job.lease_until = now() + timedelta(minutes=3)


def _release(lease):
    """让出运行：状态回到排队，下一轮从检查点继续。"""
    with Session.begin() as db:
        job = db.get(Run, lease['id'], with_for_update=True)
        if job.attempt != lease['attempt'] or job.status != Status.RUNNING:
            return
        job.status = Status.QUEUED
        job.lease_until = None
