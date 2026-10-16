"""Bounded feasibility for jointly rounded Score fields; no values are rewritten."""

EPSILON = 1e-9
MAX_PIVOTS = 2000


def feasible(rows, variables):
    """Check ``a*x <= b, x >= 0`` with a phase-one simplex tableau.

    Score has at most ten variables. Slack columns start as a feasible basis
    except for negative bounds, which receive an artificial column. Maximizing
    minus the artificial mass finds a feasible original distribution. Bland's
    entering/leaving order prevents cycling; exhaustion fails closed.
    """
    size = variables + len(rows) + sum(bound < 0 for _, bound in rows)
    tableau, basis, artificial = [], [], []
    next_artificial = variables + len(rows)
    for index, (coefficients, bound) in enumerate(rows):
        sign = -1 if bound < 0 else 1
        row = [sign * value for value in coefficients] + [0.0] * (size - variables)
        row[variables + index] = sign
        if sign < 0:
            row[next_artificial] = 1.0
            artificial.append(next_artificial)
            basis.append(next_artificial)
            next_artificial += 1
        else:
            basis.append(variables + index)
        tableau.append(row + [sign * bound])
    objective = [0.0] * (size + 1)
    for index in artificial:
        objective[index] = -1.0
    # Reduced costs and objective value for the initial basis.
    value = 0.0
    for row, basic in zip(tableau, basis, strict=True):
        if basic in artificial:
            objective = [
                cost + coefficient for cost, coefficient in zip(objective, row, strict=True)
            ]
            value -= row[-1]
    objective[-1] = value
    for _ in range(MAX_PIVOTS):
        entering = next((j for j in range(size) if objective[j] > EPSILON), None)
        if entering is None:
            return objective[-1] >= -EPSILON
        candidates = [i for i, row in enumerate(tableau) if row[entering] > EPSILON]
        if not candidates:
            return False
        leaving = min(candidates, key=lambda i: (tableau[i][-1] / tableau[i][entering], basis[i]))
        pivot = tableau[leaving][entering]
        tableau[leaving] = [coefficient / pivot for coefficient in tableau[leaving]]
        for index, row in enumerate(tableau):
            if index != leaving:
                factor = row[entering]
                tableau[index] = [
                    a - factor * b for a, b in zip(row, tableau[leaving], strict=True)
                ]
        factor = objective[entering]
        value = objective[-1] + factor * tableau[leaving][-1]
        objective = [a - factor * b for a, b in zip(objective, tableau[leaving], strict=True)]
        objective[-1] = value
        basis[leaving] = entering
    return False


def score_distribution_exists(bounds, score_interval, confidence_interval, mode, uniform_spread):
    """One probability vector must explain mass, mode, score and confidence together."""
    count = len(bounds)
    rows = []
    for index, (low, high) in enumerate(bounds):
        unit = [float(i == index) for i in range(count)]
        rows.extend(((unit, high), ([-v for v in unit], -low)))
        if index != mode:
            highest = unit.copy()
            highest[mode] = -1.0
            rows.append((highest, 0.0))
    mass = [1.0] * count
    rows.extend(((mass, 1.0), ([-v for v in mass], -1.0)))
    weights = list(range(count))
    rows.extend(((weights, score_interval[1]), ([-v for v in weights], -score_interval[0])))
    distances = [abs(i - mode) for i in range(count)]
    rows.append(([-v for v in distances], -uniform_spread * (1 - confidence_interval[1])))
    if confidence_interval[0] > 0:
        rows.append((distances, uniform_spread * (1 - confidence_interval[0])))
    return feasible(rows, count)
