"""排班推算求解器的属性测试：与穷举结果逐一比对。"""

import itertools
from fractions import Fraction

import pytest

from src.domain import staffing


def compositions(size, total):
    if size == 1:
        yield (total,)
        return
    for head in range(total + 1):
        for tail in compositions(size - 1, total - head):
            yield (head,) + tail


def brute(matrix, counts, tolerance):
    origins, destinations, ratios = staffing.prepare(matrix)
    values = staffing.validate_counts(destinations, counts)
    tolerance = Fraction(str(tolerance))
    expected = []
    for allocation in compositions(len(origins), sum(values)):
        estimates = [
            sum(allocation[o] * ratios[origin][name] for o, origin in enumerate(origins))
            for name in destinations
        ]
        if all(abs(estimates[d] - values[d]) <= tolerance for d in range(len(destinations))):
            expected.append((allocation, estimates))
    return origins, destinations, values, tolerance, expected


def parse_allocation(text, origins):
    return [int(part.split()[-1]) for part in text.split('、')]


@pytest.mark.parametrize('seed', range(120))
def test_bounds_match_bruteforce(seed):
    rng = __import__('random').Random(seed)
    origins = [f'队{i}' for i in range(rng.choice([1, 2, 3]))]
    destinations = [f'去{i}' for i in range(rng.choice([2, 3, 4]))]
    counts = {origin: {name: 0 for name in destinations} for origin in origins}
    for origin in origins:
        for _ in range(rng.randint(1, 4)):
            counts[origin][rng.choice(destinations)] += 1
    matrix = {'origins': origins, 'destinations': destinations,
              'totals': {o: sum(counts[o].values()) for o in origins}, 'counts': counts}
    entered = {name: rng.randint(0, 6) for name in destinations}
    if not sum(entered.values()):
        entered[destinations[0]] = 1
    tolerance = rng.choice([0, 0.5, 1, 1.5, 2, 3])

    team_origins, names, values, limit, expected = brute(matrix, entered, tolerance)
    result = staffing.run(matrix, entered, tolerance)

    if not expected:
        assert result['feasible'] is False
        assert result['tables'][0]['rows'], '无解时必须说明冲突约束'
        return
    assert result['feasible'] is True
    bounds = result['tables'][0]['rows']
    for index, origin in enumerate(team_origins):
        lower = min(allocation[index] for allocation, _ in expected)
        upper = max(allocation[index] for allocation, _ in expected)
        row = bounds[index]
        assert (row['lower'], row['upper']) == (lower, upper)
        for edge, value in (('lower_allocation', lower), ('upper_allocation', upper)):
            allocation = parse_allocation(row[edge], team_origins)
            assert sum(allocation) == sum(values)
            assert allocation[index] == value
            estimates = [
                sum(allocation[o] * staffing.prepare(matrix)[2][team_origins[o]][name]
                    for o in range(len(team_origins)))
                for name in names
            ]
            assert all(abs(estimates[d] - values[d]) <= limit for d in range(len(names)))
    ranges = result['tables'][2]['rows']
    for d, name in enumerate(names):
        low = min(estimate[d] for _, estimate in expected)
        high = max(estimate[d] for _, estimate in expected)
        reach_low, reach_high = (Fraction(part.strip()) for part in ranges[d]['reachable'].split('~'))
        assert abs(float(reach_low) - float(low)) < 1e-5
        assert abs(float(reach_high) - float(high)) < 1e-5


def test_resume_matches_full_run():
    matrix = {
        'origins': ['甲', '乙', '丙'],
        'destinations': ['票务', '失物', '人工分诊'],
        'totals': {'甲': 3, '乙': 2, '丙': 2},
        'counts': {'甲': {'票务': 3, '失物': 0, '人工分诊': 0},
                   '乙': {'票务': 0, '失物': 2, '人工分诊': 0},
                   '丙': {'票务': 0, '失物': 1, '人工分诊': 1}},
    }
    entered = {'票务': 18, '失物': 7, '人工分诊': 5}
    full = staffing.run(matrix, entered, 2)
    info = staffing.plan(matrix, entered, 2)
    model = info['model']
    units = staffing.units(model, info['feasible'])
    progress = {}
    for unit in units:
        progress[unit] = staffing.compute_unit(model, unit)
        # 每个检查点片段都能被 JSON 往返保存后原样重建结果。
        progress = __import__('json').loads(__import__('json').dumps(progress))
    assert staffing.assemble(model, info['feasible'], progress) == full
    assert staffing.run(matrix, entered, 2, progress) == full
    # 只完成一半时，续算结果仍与一次算完一致。
    half = dict(list(progress.items())[:len(units) // 2])
    assert staffing.run(matrix, entered, 2, half) == full


def test_infeasible_reports_minimal_conflict():
    matrix = {
        'origins': ['甲', '乙'],
        'destinations': ['票务', '失物'],
        'totals': {'甲': 2, '乙': 2},
        'counts': {'甲': {'票务': 1, '失物': 1}, '乙': {'票务': 1, '失物': 1}},
    }
    # 甲乙比例相同（各半），无论怎样整数分配，票务与失物都只能各占一半。
    result = staffing.run(matrix, {'票务': 10, '失物': 0}, 1)
    assert result['feasible'] is False
    groups = result['tables'][1]['rows']
    assert any('失物' in row['group'] for row in groups)
    assert result['note']


def test_single_team_must_take_all_tickets():
    matrix = {'origins': ['票务'], 'destinations': ['票务', '人工分诊'],
              'totals': {'票务': 2}, 'counts': {'票务': {'票务': 1, '人工分诊': 1}}}
    result = staffing.run(matrix, {'票务': 6, '人工分诊': 6}, 0)
    assert result['feasible'] is True
    row = result['tables'][0]['rows'][0]
    assert row['lower'] == row['upper'] == 12


@pytest.mark.parametrize('counts,tolerance', [
    ({'票务': -1, '失物': 3}, 1),
    ({'票务': 1.5, '失物': 3}, 1),
    ({'票务': 3}, 1),
    ({'票务': 3, '失物': 3}, -1),
])
def test_invalid_input_rejected(counts, tolerance):
    matrix = {'origins': ['甲'], 'destinations': ['票务', '失物'],
              'totals': {'甲': 2}, 'counts': {'甲': {'票务': 1, '失物': 1}}}
    with pytest.raises(ValueError):
        staffing.run(matrix, counts, tolerance)


def test_zero_tolerance_requires_exact_integers():
    matrix = {'origins': ['甲'], 'destinations': ['票务', '失物'],
              'totals': {'甲': 2}, 'counts': {'甲': {'票务': 1, '失物': 1}}}
    assert staffing.run(matrix, {'票务': 5, '失物': 5}, 0)['feasible'] is True
    assert staffing.run(matrix, {'票务': 6, '失物': 4}, 0)['feasible'] is False


def brute_feasible(matrix, counts, tolerance, active):
    origins, destinations, ratios = staffing.prepare(matrix)
    values = staffing.validate_counts(destinations, counts)
    tolerance = Fraction(str(tolerance))
    for allocation in compositions(len(origins), sum(values)):
        ok = True
        for d in active:
            estimate = sum(allocation[o] * ratios[origin][destinations[d]]
                           for o, origin in enumerate(origins))
            if abs(estimate - values[d]) > tolerance:
                ok = False
                break
        if ok:
            return True
    return False


@pytest.mark.parametrize('seed', range(80))
def test_conflict_groups_are_minimal(seed):
    rng = __import__('random').Random(1000 + seed)
    origins = [f'队{i}' for i in range(rng.choice([1, 2, 3]))]
    destinations = [f'去{i}' for i in range(rng.choice([2, 3, 4]))]
    counts = {origin: {name: 0 for name in destinations} for origin in origins}
    for origin in origins:
        for _ in range(rng.randint(1, 3)):
            counts[origin][rng.choice(destinations)] += 1
    matrix = {'origins': origins, 'destinations': destinations,
              'totals': {o: sum(counts[o].values()) for o in origins}, 'counts': counts}
    entered = {name: rng.randint(0, 5) for name in destinations}
    if not sum(entered.values()):
        entered[destinations[0]] = 1
    tolerance = rng.choice([0, 1, 2])
    result = staffing.run(matrix, entered, tolerance)
    if result['feasible']:
        assert brute_feasible(matrix, entered, tolerance, range(len(destinations)))
        return
    assert not brute_feasible(matrix, entered, tolerance, range(len(destinations)))
    groups = [[destinations.index(name) for name in row['group'].split('、')]
              for row in result['tables'][1]['rows']]
    assert groups
    for group in groups:
        assert not brute_feasible(matrix, entered, tolerance, group), '列出的冲突组应确实无解'
        if len(group) > 1:
            for d in group:
                assert brute_feasible(matrix, entered, tolerance, [x for x in group if x != d]), \
                    '去掉任一约束后应可行'
