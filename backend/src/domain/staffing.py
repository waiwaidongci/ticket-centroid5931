"""按已完成验收的分流去向比例，推算新批次各队接件量的整数上下界。

验收依据给出每个归口队的留出件在各去向（业务队列与人工分诊）上的比例。
新批次只录入各去向的件数和统一容差，求满足

    Σ_o n_o = N                      各队件数取非负整数，合计为来信总数
    |Σ_o n_o · r_{o,d} - c_d| ≤ 容差   每个去向的推算件数落在容差内

的全部整数分配 n_o。返回各归口队件数的上下界及达到边界的完整分配；
若无可行分配，则给出互相冲突的最小约束集合。

比例与容差都以二进制浮点传入，这里用 ``Fraction`` 还原成十进制精确值，
再把所有系数放大到公共整数尺度求解，边界判定不受浮点误差影响。
"""

from fractions import Fraction
from itertools import combinations
from math import lcm

MAX_TEAMS = 3
MAX_COUNT = 100_000


def _decimal(value):
    """把数值按十进制字面量转成分数，避免浮点尾差。"""
    if isinstance(value, bool):
        raise ValueError("数值格式有误。")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("数值格式有误。")
        return Fraction(str(value))
    raise ValueError("数值格式有误。")


def prepare(matrix):
    """校验冻结的依据快照，返回归口、去向和精确比例。"""
    if not isinstance(matrix, dict):
        raise ValueError("验收依据格式有误。")
    origins = list(matrix.get("origins") or [])
    destinations = list(matrix.get("destinations") or [])
    if not origins:
        raise ValueError("验收依据没有可沿用的归口队。")
    if not destinations:
        raise ValueError("验收依据没有可沿用的分流去向。")
    if len(origins) > MAX_TEAMS:
        raise ValueError(f"验收依据的归口队最多 {MAX_TEAMS} 个，请在验收中先合并队列。")
    totals, counts = {}, {}
    for origin in origins:
        total = matrix.get("totals", {}).get(origin)
        if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
            raise ValueError(f"验收依据中「{origin}」没有留出件，无法沿用其分流去向比例。")
        totals[origin] = total
        row = matrix.get("counts", {}).get(origin) or {}
        counts[origin] = {}
        for destination in destinations:
            value = row.get(destination, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"验收依据中「{origin} → {destination}」的件数格式有误。")
            counts[origin][destination] = value
        if sum(counts[origin].values()) != total:
            raise ValueError(f"验收依据中「{origin}」的去向件数合计与留出总数不一致。")
    ratios = {
        origin: {destination: Fraction(counts[origin][destination], totals[origin])
                 for destination in destinations}
        for origin in origins
    }
    return origins, destinations, ratios


def validate_counts(destinations, counts):
    if not isinstance(counts, dict):
        raise ValueError("新批次去向件数格式有误。")
    missing = [name for name in destinations if name not in counts]
    if missing:
        raise ValueError(f"请录入去向「{'、'.join(missing)}」的件数。")
    unknown = [name for name in counts if name not in destinations]
    if unknown:
        raise ValueError(f"存在不属于本项目的去向「{'、'.join(unknown)}」。")
    values = []
    for name in destinations:
        value = counts[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"去向「{name}」的件数须为非负整数。")
        if value > MAX_COUNT:
            raise ValueError(f"去向「{name}」的件数不能超过 {MAX_COUNT}。")
        values.append(value)
    total = sum(values)
    if total <= 0:
        raise ValueError("新批次来信总数须大于零。")
    if total > MAX_COUNT:
        raise ValueError(f"来信总数不能超过 {MAX_COUNT}，请分批推算。")
    return values


def model(matrix, counts, tolerance):
    """重建整数尺度模型。冻结输入相同则结果完全可复现。"""
    origins, destinations, ratios = prepare(matrix)
    values = validate_counts(destinations, counts)
    tolerance = _decimal(tolerance)
    if tolerance < 0:
        raise ValueError("统一容差须为非负数。")
    scale = tolerance.denominator
    for origin in origins:
        for destination in destinations:
            scale = lcm(scale, ratios[origin][destination].denominator)
    a = [[int(ratios[origin][destination] * scale) for destination in destinations]
         for origin in origins]
    allowance = tolerance * scale
    lower = [values[d] * scale - allowance for d in range(len(destinations))]
    upper = [values[d] * scale + allowance for d in range(len(destinations))]
    return {
        "basis_name": matrix.get("basis_name") or "已完成验收",
        "origins": origins, "destinations": destinations,
        "counts": values, "tolerance": tolerance, "scale": scale,
        "a": a, "lower": lower, "upper": upper,
        "total": sum(values),
    }


def _ceil_div(a, b):
    """整数上取整，b 可正可负。"""
    if b > 0:
        return -((-a) // b)
    return -(a // (-b))


def _floor_div(a, b):
    if b > 0:
        return a // b
    return (-a) // (-b)


def _valid(model, allocation, active=None):
    a = model["a"]
    for d in (range(len(model["lower"])) if active is None else active):
        low, high = model["lower"][d], model["upper"][d]
        if not low <= sum(a[o][d] * allocation[o] for o in range(len(a))) <= high:
            return False
    return True


def _scan(model, objective, active):
    """在 active 去向约束下优化一个整数目标，返回 (取值, 分配) 或 None。

    归口队最多三个，把最后一个变量用总数消去后只剩一维枚举。
    """
    a = model["a"]
    low_bounds, upper_bounds = model["lower"], model["upper"]
    total = model["total"]
    size = len(model["origins"])
    active = tuple(active)
    best = None
    edge = objective[2]

    def keep(value, allocation):
        nonlocal best
        if best is None or (edge == "min" and value < best[0]) or (edge == "max" and value > best[0]):
            best = (value, list(allocation))

    def score(allocation):
        if objective[0] == "team":
            return allocation[objective[1]]
        destination = objective[1]
        return sum(a[o][destination] * allocation[o] for o in range(size))

    if size == 1:
        allocation = [total]
        if _valid(model, allocation, active):
            keep(score(allocation), allocation)
        return best

    if size == 2:
        for first in range(total + 1):
            allocation = [first, total - first]
            if _valid(model, allocation, active):
                keep(score(allocation), allocation)
        return best

    for first in range(total + 1):
        low, high = 0, total - first
        for d in active:
            slope = a[1][d] - a[2][d]
            base = a[0][d] * first + a[2][d] * (total - first)
            need_low, need_high = low_bounds[d] - base, upper_bounds[d] - base
            if slope == 0:
                if not need_low <= 0 <= need_high:
                    low, high = 1, 0
                    break
            elif slope > 0:
                low = max(low, _ceil_div(need_low, slope))
                high = min(high, _floor_div(need_high, slope))
            else:
                low = max(low, _ceil_div(need_high, slope))
                high = min(high, _floor_div(need_low, slope))
            if low > high:
                break
        if low > high:
            continue
        if objective[0] == "team":
            index = objective[1]
            if index == 0:
                second = low
            elif index == 1:
                second = low if edge == "min" else high
            else:
                second = high if edge == "min" else low
            allocation = [first, second, total - first - second]
            keep(score(allocation), allocation)
            continue
        destination = objective[1]
        slope = a[1][destination] - a[2][destination]
        base = a[0][destination] * first + a[2][destination] * (total - first)
        if edge == "min":
            second = low if slope >= 0 else high
        else:
            second = high if slope >= 0 else low
        keep(base + slope * second, [first, second, total - first - second])
    return best


def _feasible(model, active):
    return _scan(model, ("team", 0, "min"), tuple(active)) is not None


def _reachable(model, destination, active):
    low = _scan(model, ("dest", destination, "min"), tuple(active))
    high = _scan(model, ("dest", destination, "max"), tuple(active))
    if low is None or high is None:
        return None
    scale = model["scale"]
    return Fraction(low[0], scale), Fraction(high[0], scale)


def _minimal_conflicts(model):
    """找出互相冲突的最小约束集合（按去向）。"""
    width = len(model["destinations"])
    singletons = [d for d in range(width) if not _feasible(model, (d,))]
    if singletons:
        return [frozenset((d,)) for d in singletons]
    found = []
    for size in range(2, width + 1):
        for subset in combinations(range(width), size):
            if _feasible(model, subset):
                continue
            if any(not _feasible(model, tuple(x for x in subset if x != d)) for d in subset):
                continue
            found.append(frozenset(subset))
    return found or [frozenset(range(width))]


def conflicts(model):
    """给出冲突的最小约束集合，以及各冲突去向在其余约束下的可达区间。"""
    destinations = model["destinations"]
    counts, tolerance = model["counts"], model["tolerance"]
    groups = _minimal_conflicts(model)
    entries, seen = [], set()
    for group in groups:
        for d in sorted(group):
            if d in seen:
                continue
            seen.add(d)
            others = tuple(x for x in group if x != d)
            reachable = _reachable(model, d, others)
            entries.append({
                "destination": destinations[d], "entered": counts[d],
                "allowed": (Fraction(counts[d]) - tolerance, Fraction(counts[d]) + tolerance),
                "reachable": reachable,
                "scope": "其余冲突去向" if others else "仅总数",
            })
    return {
        "minimal": [[destinations[d] for d in sorted(group)] for group in groups],
        "entries": entries,
    }


def units(model, feasible):
    """需要逐个计算并落检查点的工作单元，只含尚未完成的结果。"""
    if not feasible:
        return ["conflicts"]
    items = []
    for index in range(len(model["origins"])):
        items += [f"team:{index}:lower", f"team:{index}:upper"]
    for index in range(len(model["destinations"])):
        items += [f"dest:{index}:min", f"dest:{index}:max"]
    return items


def compute_unit(model, unit):
    """计算一个工作单元，返回可直接存入 JSON 的片段。"""
    scale = model["scale"]
    active = tuple(range(len(model["destinations"])))
    if unit == "conflicts":
        return _conflicts_payload(model)
    if unit.startswith("team:"):
        _, index, edge = unit.split(":")
        found = _scan(model, ("team", int(index), "min" if edge == "lower" else "max"), active)
        return _pack(model, found) if found else {"value": None}
    _, index, edge = unit.split(":")
    found = _scan(model, ("dest", int(index), "min" if edge == "min" else "max"), active)
    return _pack(model, found, scale) if found else {"value": None}


def _conflicts_payload(model):
    detail = conflicts(model)
    return {
        "minimal": detail["minimal"],
        "entries": [{
            "destination": entry["destination"], "entered": entry["entered"],
            "allowed": [str(entry["allowed"][0]), str(entry["allowed"][1])],
            "reachable": [str(entry["reachable"][0]), str(entry["reachable"][1])]
                         if entry["reachable"] else None,
            "scope": entry["scope"],
        } for entry in detail["entries"]],
    }


def _pack(model, found, scale=1):
    value, allocation = found
    return {
        "value": value if scale == 1 else str(Fraction(value, scale)),
        "allocation": list(allocation),
        "estimates": {name: str(amount) for name, amount in _estimates(model, allocation).items()},
    }


def _amount(value):
    return value if isinstance(value, int) else Fraction(str(value))


def _estimates(model, allocation):
    scale = model["scale"]
    return {destination: Fraction(sum(model["a"][o][d] * allocation[o]
                                      for o in range(len(model["a"]))), scale)
            for d, destination in enumerate(model["destinations"])}


def plan(matrix, counts, tolerance):
    """冻结输入后重建模型并判定可行性。"""
    model_ = model(matrix, counts, tolerance)
    return {"model": model_, "feasible": _feasible(model_, tuple(range(len(model_["destinations"]))))}


def run(matrix, counts, tolerance, progress=None):
    """一次算完所有边界；progress 为已完成单元的片段，用于中断续算。"""
    info = plan(matrix, counts, tolerance)
    model_ = info["model"]
    done = dict(progress or {})
    if not info["feasible"]:
        done.setdefault("conflicts", compute_unit(model_, "conflicts"))
        return assemble(model_, False, done)
    for unit in units(model_, True):
        if unit not in done:
            done[unit] = compute_unit(model_, unit)
    return assemble(model_, True, done)


def _round(value):
    return round(float(value), 6)


def _allocation_text(origins, allocation):
    return "、".join(f"{name} {count}" for name, count in zip(origins, allocation))


def _estimate_text(estimates):
    return "、".join(f"{name} {_round(value):g}" for name, value in estimates.items())


def _within(model, estimates):
    return all(abs(estimates[name] - model["counts"][d]) <= model["tolerance"]
               for d, name in enumerate(model["destinations"]))


def assemble(model_, feasible, done):
    if not feasible:
        return _conflict_result(model_, done["conflicts"])
    origins, destinations = model_["origins"], model_["destinations"]
    counts, tolerance, total = model_["counts"], model_["tolerance"], model_["total"]

    bounds = []
    for index, origin in enumerate(origins):
        lower, upper = done[f"team:{index}:lower"], done[f"team:{index}:upper"]
        bounds.append({
            "team": origin, "lower": lower["value"], "upper": upper["value"],
            "lower_allocation": dict(zip(origins, lower["allocation"])),
            "lower_estimates": {name: _amount(amount) for name, amount in lower["estimates"].items()},
            "upper_allocation": dict(zip(origins, upper["allocation"])),
            "upper_estimates": {name: _amount(amount) for name, amount in upper["estimates"].items()},
        })
    ranges = []
    for index, name in enumerate(destinations):
        low = _amount(done[f"dest:{index}:min"]["value"])
        high = _amount(done[f"dest:{index}:max"]["value"])
        allowed_low = counts[index] - tolerance
        allowed_high = counts[index] + tolerance
        ranges.append({
            "destination": name, "entered": counts[index],
            "allowed": (allowed_low, allowed_high), "reachable": (low, high),
            "within": allowed_low <= low and high <= allowed_high,
        })

    bound_rows = [{
        "team": row["team"], "lower": row["lower"],
        "lower_allocation": _allocation_text(origins, [row["lower_allocation"][o] for o in origins]),
        "upper": row["upper"],
        "upper_allocation": _allocation_text(origins, [row["upper_allocation"][o] for o in origins]),
    } for row in bounds]
    edge_rows = []
    for row in bounds:
        for edge, allocation, estimates in (
            ("下界", row["lower_allocation"], row["lower_estimates"]),
            ("上界", row["upper_allocation"], row["upper_estimates"]),
        ):
            edge_rows.append({
                "team": row["team"], "edge": edge,
                "allocation": _allocation_text(origins, [allocation[o] for o in origins]),
                "estimates": _estimate_text(estimates),
                "within": "是" if _within(model_, estimates) else "否",
            })
    range_rows = [{
        "destination": row["destination"], "entered": row["entered"],
        "allowed": f"{_round(row['allowed'][0]):g} ~ {_round(row['allowed'][1]):g}",
        "reachable": f"{_round(row['reachable'][0]):g} ~ {_round(row['reachable'][1]):g}",
        "within": "是" if row["within"] else "否",
    } for row in ranges]

    lower_total = sum(row["lower"] for row in bounds)
    upper_total = sum(row["upper"] for row in bounds)
    return {
        "state": "ok", "kind": "staffing", "feasible": True, "ready": True,
        "headline": (f"新批 {total} 封沿用验收流向比例存在整数分配："
                     f"各队下界合计 {lower_total}，上界合计 {upper_total}。"),
        "note": (f"依据「{model_['basis_name']}」的留出件分流比例，"
                 f"统一容差 {_round(tolerance):g} 件，人工分诊计入去向。"),
        "metrics": [
            {"label": "来信总数", "value": total, "detail": "各去向录入件数之和"},
            {"label": "统一容差", "value": _round(tolerance), "detail": "推算件数与录入件数之差上限"},
            {"label": "归口队数", "value": len(origins), "detail": "沿用验收中有人工归口的业务队列"},
            {"label": "下界合计", "value": lower_total, "detail": "各队下界之和（未必同批同时取到）"},
            {"label": "上界合计", "value": upper_total, "detail": "各队上界之和（未必同批同时取到）"},
        ],
        "tables": [
            _table("各队接件量上下界", [
                ("team", "归口队"), ("lower", "下界"), ("lower_allocation", "下界对应分配"),
                ("upper", "上界"), ("upper_allocation", "上界对应分配"),
            ], bound_rows),
            _table("边界分配的推算去向", [
                ("team", "归口队"), ("edge", "边界"), ("allocation", "完整分配"),
                ("estimates", "推算去向"), ("within", "均在容差内"),
            ], edge_rows),
            _table("各去向推算件数范围", [
                ("destination", "去向"), ("entered", "录入件数"), ("allowed", "容差允许区间"),
                ("reachable", "可行分配可达区间"), ("within", "可达区间落在允许区间内"),
            ], range_rows),
        ],
        "charts": [{
            "title": "各队接件量上下界", "kind": "bar",
            "series": [{"label": f"{row['team']} 下界", "value": row["lower"]} for row in bounds]
                      + [{"label": f"{row['team']} 上界", "value": row["upper"]} for row in bounds],
        }],
        "sections": [{
            "title": "排班取数说明",
            "text": (
                "按验收留出件的归口到去向比例，把新批次各去向件数还原为各队接件量。"
                "上下界是该队在全部满足容差的整数分配中的最小与最大值；对应分配给出达到边界的整批去向件数，"
                "可直接用于排班。人工分诊作为独立去向参与约束，不并入业务组。"
            ),
        }],
    }


def _conflict_result(model_, payload):
    detail = payload
    rows = []
    for entry in detail["entries"]:
        reachable = entry["reachable"]
        allowed_low, allowed_high = (_amount(entry["allowed"][0]), _amount(entry["allowed"][1]))
        rows.append({
            "destination": entry["destination"], "entered": entry["entered"],
            "allowed": f"{_round(allowed_low):g} ~ {_round(allowed_high):g}",
            "reachable": (f"{_round(_amount(reachable[0])):g} ~ {_round(_amount(reachable[1])):g}"
                          if reachable else "无可行分配"),
            "scope": entry["scope"],
        })
    return {
        "state": "warning", "kind": "staffing", "feasible": False, "ready": False,
        "headline": "新批次件数与验收流向比例在统一容差内无整数分配，请调整容差或件数。",
        "note": (f"来信总数 {model_['total']}，统一容差 {_round(model_['tolerance']):g} 件。"
                 "下表列出互相冲突的最小去向组合及各去向可达范围。"),
        "metrics": [
            {"label": "来信总数", "value": model_["total"], "detail": "各去向录入件数之和"},
            {"label": "统一容差", "value": _round(model_["tolerance"]), "detail": "推算件数与录入件数之差上限"},
            {"label": "可行整数分配", "value": 0, "detail": "容差内没有满足全部去向的整数分配"},
        ],
        "tables": [
            _table("冲突约束与可达范围", [
                ("destination", "去向"), ("entered", "录入件数"), ("allowed", "容差允许区间"),
                ("reachable", "其余约束下可达区间"), ("scope", "可达范围口径"),
            ], rows),
            _table("最小冲突组合", [("group", "冲突去向")],
                   [{"group": "、".join(group)} for group in detail["minimal"]]),
        ],
        "charts": [],
        "sections": [{
            "title": "调整建议",
            "text": (
                "先核对冲突去向的录入件数是否与来信总数一致；件数无误时可放宽统一容差，"
                "或把新批次拆成更小的分组分别推算。最小冲突组合中的去向无法同时落在容差内。"
            ),
        }],
    }


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns],
            "rows": rows}
