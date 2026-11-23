"""端到端：验收后按比例推算，覆盖中断续算、幂等发布与无解说明。"""

import os
import tempfile

import pytest

TMP = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL=f'sqlite:///{TMP}/test.db', JWT_SECRET='t' * 40,
    ARTIFACT_DIR=f'{TMP}/artifacts', SEED_DEMO='true', REDIS_URL='redis://127.0.0.1:6379/15',
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import src.routes.runs as runs_module  # noqa: E402
import src.utils.wakeup as wakeup  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.domain import staffing  # noqa: E402
from src.main import app  # noqa: E402
from src.models.jobs import Delivery, Run, StaffingStep  # noqa: E402
from src.services import execution  # noqa: E402

wakeup.wake = lambda: None
runs_module.wake = lambda: None


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as instance:
        yield instance


@pytest.fixture(scope='module')
def headers(client):
    token = client.post('/auth/login', json={'username': 'admin', 'password': 'admin-demo-2611'}).json()['token']
    return {'Authorization': f'Bearer {token}'}


def drain():
    cycles = 0
    while (lease := execution.claim()):
        execution.execute(lease)
        cycles += 1
        assert cycles < 200, '续算未收敛'
    return cycles


def acceptance(client, headers):
    dataset = client.get('/datasets', headers=headers).json()[0]
    run_id = client.post('/runs', headers=headers,
                         json={'dataset_id': dataset['id'], 'name': '验收依据', 'params': {}}).json()['id']
    drain()
    return run_id


def test_basis_then_staffing_bounds_match_domain(client, headers):
    basis_id = acceptance(client, headers)
    basis = client.get(f'/runs/{basis_id}', headers=headers).json()['result']['basis']
    counts = {'票务退改': 18, '无障碍服务': 8, '失物招领': 4, '人工分诊': 4}
    body = {'basis_run_id': basis_id, 'name': '排班一', 'counts': counts, 'params': {'tolerance': 3}}
    created = client.post('/runs/staffing', headers=headers, json=body).json()
    assert created['reused'] is False
    drain()
    job = client.get(f"/runs/{created['id']}", headers=headers).json()
    assert job['status'] == 'complete'
    expected = staffing.run(basis, counts, 3.0)
    for actual, reference in zip(job['result']['tables'], expected['tables']):
        assert actual['rows'] == reference['rows']
    assert client.get(f"/runs/{created['id']}/export", headers=headers).status_code == 200


def test_resubmit_is_idempotent(client, headers):
    basis_id = acceptance(client, headers)
    counts = {'票务退改': 12, '无障碍服务': 6, '失物招领': 4, '人工分诊': 2}
    body = {'basis_run_id': basis_id, 'name': '排班二', 'counts': counts, 'params': {'tolerance': 2}}
    first = client.post('/runs/staffing', headers=headers, json=body).json()
    drain()
    again = client.post('/runs/staffing', headers=headers, json=body).json()
    assert again['reused'] is True and again['id'] == first['id']


def test_interrupted_run_resumes_without_duplicates(client, headers):
    basis_id = acceptance(client, headers)
    basis = client.get(f'/runs/{basis_id}', headers=headers).json()['result']['basis']
    counts = {'票务退改': 20, '无障碍服务': 8, '失物招领': 5, '人工分诊': 3}
    body = {'basis_run_id': basis_id, 'name': '排班三', 'counts': counts, 'params': {'tolerance': 2}}
    run_id = client.post('/runs/staffing', headers=headers, json=body).json()['id']
    expected = staffing.run(basis, counts, 2.0)

    original = execution._budget
    execution._budget = lambda: 1
    try:
        cycles = drain()
        assert cycles > 2, '每个周期只算一个单元时应跨多轮'
    finally:
        execution._budget = original
    job = client.get(f'/runs/{run_id}', headers=headers).json()
    assert job['status'] == 'complete'
    for actual, reference in zip(job['result']['tables'], expected['tables']):
        assert actual['rows'] == reference['rows']
    with Session() as db:
        steps = db.scalar(select(func.count()).select_from(StaffingStep).where(StaffingStep.run_id == run_id))
        deliveries = db.scalar(select(func.count()).select_from(Delivery).where(Delivery.run_id == run_id))
        run = db.get(Run, run_id)
    assert steps == len(staffing.units(staffing.plan(basis, counts, 2.0)['model'], True))
    assert deliveries == 0
    assert run.attempt == 1, '续算不应消耗重试次数'
    assert run.resumes >= 1


def test_infeasible_input_explains_conflict(client, headers):
    basis_id = acceptance(client, headers)
    body = {'basis_run_id': basis_id, 'name': '无解', 'counts': {
        '票务退改': 0, '无障碍服务': 0, '失物招领': 0, '人工分诊': 40}, 'params': {'tolerance': 1}}
    run_id = client.post('/runs/staffing', headers=headers, json=body).json()['id']
    drain()
    job = client.get(f'/runs/{run_id}', headers=headers).json()
    assert job['status'] == 'complete'
    assert job['result']['feasible'] is False
    assert job['result']['ready'] is False
    assert job['result']['tables'][0]['rows']
    assert job['result']['tables'][1]['rows']


def test_invalid_counts_rejected_before_queueing(client, headers):
    basis_id = acceptance(client, headers)
    before = client.get('/runs', headers=headers).json()
    response = client.post('/runs/staffing', headers=headers, json={
        'basis_run_id': basis_id, 'name': '坏输入',
        'counts': {'票务退改': -1, '无障碍服务': 0, '失物招领': 0, '人工分诊': 0}, 'params': {'tolerance': 1}})
    assert response.status_code == 422
    assert len(client.get('/runs', headers=headers).json()) == len(before)


def test_basis_requires_completed_acceptance(client, headers):
    response = client.post('/runs/staffing', headers=headers, json={
        'basis_run_id': 999999, 'name': '缺失依据', 'counts': {}, 'params': {'tolerance': 1}})
    assert response.status_code == 404


def test_failed_run_is_requeued_not_duplicated(client, headers):
    basis_id = acceptance(client, headers)
    counts = {'票务退改': 11, '无障碍服务': 7, '失物招领': 3, '人工分诊': 3}
    body = {'basis_run_id': basis_id, 'name': '待重试', 'counts': counts, 'params': {'tolerance': 3}}
    first = client.post('/runs/staffing', headers=headers, json=body).json()
    drain()
    # 模拟后台中断后放弃的运行。
    with Session.begin() as db:
        run = db.get(Run, first['id'], with_for_update=True)
        run.status = 'failed'
        run.error = '模拟中断'
        run.attempt = 3
        run.resumes = 9
    again = client.post('/runs/staffing', headers=headers, json=body).json()
    assert again['id'] == first['id'] and again['reused'] is False
    drain()
    job = client.get(f"/runs/{first['id']}", headers=headers).json()
    assert job['status'] == 'complete'
    with Session() as db:
        total = db.scalar(select(func.count()).select_from(Run)
                          .where(Run.kind == 'staffing', Run.basis_run_id == basis_id,
                                 Run.request_key == db.get(Run, first['id']).request_key))
    assert total == 1, '同一冻结输入只保留一个运行'
