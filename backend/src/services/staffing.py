"""排班推算运行的创建编排：开跑即冻结验收依据与新批件数。

幂等约定：同一验收依据、同一件数快照和同一容差只对应一个 request_key；
重复提交由唯一约束拦截并返回既有运行，重试不会重复发布结果。
"""

import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.constants.enums import Status
from src.domain import staffing
from src.domain.spec import SPEC
from src.models.jobs import Run
from src.services.audit import record
from src.services.validation import cell


def basis_of(run):
    """取一份已完成验收的分流依据，冻结为纯数据快照。"""
    if run is None or run.kind != 'acceptance':
        raise HTTPException(404, '找不到指定的验收依据')
    if run.status != Status.COMPLETE or not run.result:
        raise ValueError('验收依据须为已完成的分流验收')
    basis = run.result.get('basis')
    if not basis:
        raise ValueError('该验收缺少分流去向比例，请先重跑验收')
    return {
        'basis_run_id': run.id, 'basis_name': run.name,
        'origins': list(basis['origins']), 'destinations': list(basis['destinations']),
        'totals': {origin: int(basis['totals'][origin]) for origin in basis['origins']},
        'counts': {origin: {d: int(basis['counts'][origin].get(d, 0)) for d in basis['destinations']}
                   for origin in basis['origins']},
    }


def frozen_params(body, basis):
    """校验并冻结录入件数与统一容差，返回可持久化的参数快照。"""
    tolerance_field = SPEC['staffing']['tolerance']
    tolerance = cell(tolerance_field, body.params.get(tolerance_field['key'], tolerance_field['default']))
    destinations = basis['destinations']
    staffing.validate_counts(destinations, body.counts)
    frozen = {
        'basis': {key: basis[key] for key in ('basis_name', 'origins', 'destinations', 'totals', 'counts')},
        'counts': {name: body.counts[name] for name in destinations},
        'tolerance': tolerance,
    }
    # 建模一次，提交时即可暴露比例或容差问题。
    staffing.model(frozen['basis'], frozen['counts'], frozen['tolerance'])
    return frozen


def request_key(basis_run_id, params):
    """依据、件数与容差的稳定指纹，用于幂等发布。"""
    raw = json.dumps({'basis_run_id': basis_run_id, 'counts': params['counts'],
                      'tolerance': str(params['tolerance'])},
                     ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def create(db, body, actor, basis, params):
    key = request_key(basis['basis_run_id'], params)
    item = Run(kind='staffing', basis_run_id=basis['basis_run_id'], dataset_id=None,
               name=body.name, params=params, request_key=key, author=actor.username)
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(Run).where(Run.request_key == key).with_for_update())
        if not existing:
            raise
        if existing.status == Status.FAILED:
            # 同一份冻结输入重试时复用该运行，清空失败痕迹并重新排队，不另发一份。
            existing.status = Status.QUEUED
            existing.error = None
            existing.finished_at = None
            existing.attempt = 0
            existing.resumes = 0
            record(db, actor.username, 'staffing.requeued', existing.id)
            return existing, False
        return existing, True
    record(db, actor.username, 'staffing.queued', item.id,
           basis_run_id=basis['basis_run_id'], tolerance=params['tolerance'])
    db.flush()
    return item, False
