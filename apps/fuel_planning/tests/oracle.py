"""Independent exhaustive fuel-unit search for small, exact test graphs.

Unlike the production algorithm, this oracle buys one unit at a time and always
allows travel without buying fuel. Inputs must lie on the requested fuel lattice;
it never rounds real distances or approximates the continuous problem.
"""

import heapq
from fractions import Fraction
from itertools import count


def exhaustive_unit_fuel_cost(
    distances,
    prices,
    *,
    source,
    destination,
    capacity,
    initial_fuel,
    mpg=1,
    fuel_unit=Fraction(1),
):
    """Return minimum exact purchase cost, or None if no route is feasible.

    ``distances`` maps a node to a mapping of reachable nodes and miles.
    ``prices`` maps purchase-capable nodes to prices per gallon. All other nodes
    permit travel only. Node identifiers may be any hashable values.
    """
    unit = Fraction(fuel_unit)
    economy = Fraction(mpg)
    if unit <= 0 or economy <= 0:
        raise ValueError("Fuel unit and economy must be positive.")

    def units(value):
        amount = Fraction(value) / unit
        if amount < 0 or amount.denominator != 1:
            raise ValueError("Oracle inputs must lie on a nonnegative fuel lattice.")
        return amount.numerator

    tank = units(capacity)
    initial = units(initial_fuel)
    if tank <= 0 or initial > tank:
        raise ValueError("Oracle fuel must fit a positive tank capacity.")
    purchase_costs = {node: Fraction(price) * unit for node, price in prices.items()}
    if any(cost < 0 for cost in purchase_costs.values()):
        raise ValueError("Oracle prices cannot be negative.")
    graph = {
        node: tuple(
            (target, units(Fraction(miles) / economy))
            for target, miles in edges.items()
        )
        for node, edges in distances.items()
    }
    serial = count()
    queue = [(Fraction(), next(serial), source, initial)]
    best = {(source, initial): Fraction()}

    def offer(node, fuel, cost):
        state = node, fuel
        previous = best.get(state)
        if previous is None or cost < previous:
            best[state] = cost
            heapq.heappush(queue, (cost, next(serial), node, fuel))

    while queue:
        cost, _, node, fuel = heapq.heappop(queue)
        if cost != best[node, fuel]:
            continue
        if node == destination:
            return cost
        if node in purchase_costs and fuel < tank:
            offer(node, fuel + 1, cost + purchase_costs[node])
        for target, needed in graph.get(node, ()):
            if needed <= fuel:
                offer(target, fuel - needed, cost)
    return None
