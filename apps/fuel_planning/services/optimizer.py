"""Exact continuous-fuel search over a declared shortcut-closed movement model."""

import heapq
import time
from collections import OrderedDict
from decimal import Decimal
from fractions import Fraction

from apps.fuel_planning.types import (
    FuelAction,
    PlanQuality,
    RoutePosition,
    RouteSource,
    SearchBudget,
    SearchStatistics,
    VehicleSpec,
)

from .distances import MovementNotPermitted
from .labels import LabelStore, SearchLabel
from .numeric import as_exact_amount, validate_fuel
from .reconstruction import reconstruct_plan


class InvalidDistanceProvider(ValueError):
    """The provider cannot support the optimizer's transition guarantees."""


class NoFeasiblePlan(Exception):
    """A completed search found no feasible plan in the declared model."""

    search_complete = True

    def __init__(self, statistics):
        super().__init__("No fuel plan is reachable in the declared movement model.")
        self.statistics = statistics


class SearchIncomplete(Exception):
    """The search budget ended before finding a feasible plan."""

    search_complete = False

    def __init__(self, statistics):
        super().__init__("Search budget exhausted before finding a feasible fuel plan.")
        self.statistics = statistics


class _BudgetReached(Exception):
    pass


class _FuelSearch:
    def __init__(
        self,
        source,
        destination,
        visits,
        vehicle,
        provider,
        initial_fuel,
        budget,
        enable_dominance,
        clock,
    ):
        self.clock = clock
        self.started = clock()
        self.source = source
        self.destination = destination
        self.vehicle = vehicle
        self.provider = provider
        self.initial_fuel = validate_fuel(initial_fuel, vehicle.tank_capacity_gallons)
        self.budget = budget
        self.visits = {visit.visit_id: visit for visit in visits}
        if len(self.visits) != len(visits):
            raise ValueError("Station visit identities must be unique.")
        provider_visits = {visit.visit_id: visit for visit in provider.visits}
        if provider_visits != self.visits or len(provider_visits) != len(
            provider.visits
        ):
            raise InvalidDistanceProvider(
                "Provider must retain the complete supplied visit catalog."
            )
        if provider.vehicle != vehicle or provider.destination != destination:
            raise InvalidDistanceProvider(
                "Provider vehicle and destination must match this search."
            )
        self.prices = {
            visit_id: as_exact_amount(visit.price_per_gallon)
            for visit_id, visit in self.visits.items()
        }
        self.minimum_price = min(self.prices.values(), default=Fraction())
        self.store = LabelStore(enable_dominance=enable_dominance)
        self.queue = []
        self.generated = self.accepted = self.settled = self.transitions = (
            self.stale
        ) = 0
        self.incumbent = None
        self.destination_legs = {}
        # Reuse repeated label expansions without retaining a dense all-pairs
        # graph. Eviction only causes recomputation; it never removes candidates.
        self.neighbor_edges = OrderedDict()

    def _check_deadline(self):
        if self.budget.deadline is not None and self.clock() >= self.budget.deadline:
            raise _BudgetReached("deadline")

    def _statistics(self, reason):
        return SearchStatistics(
            generated_labels=self.generated,
            accepted_labels=self.accepted,
            settled_labels=self.settled,
            transitions=self.transitions,
            stale_pops=self.stale,
            elapsed_seconds=max(0.0, self.clock() - self.started),
            stop_reason=reason,
        )

    def _offer(self, visit_id, fuel, cost, predecessor_id, action):
        self._check_deadline()
        if self.incumbent is not None and cost >= self.incumbent.cost:
            return None
        if self.generated >= self.budget.maximum_generated_labels:
            raise _BudgetReached("label_limit")
        validate_fuel(fuel, self.vehicle.tank_capacity_gallons)
        label = SearchLabel(
            self.generated, visit_id, fuel, cost, predecessor_id, action
        )
        self.generated += 1
        if not self.store.accept(label):
            return None
        self.accepted += 1
        heapq.heappush(self.queue, (label.cost, label.label_id, label.label_id))
        if visit_id is None:
            self.incumbent = label
        return label

    def _destination_leg(self, visit_id):
        if visit_id not in self.destination_legs:
            try:
                leg = self.provider.leg(self.visits[visit_id], self.destination)
            except MovementNotPermitted:
                leg = None
            self.destination_legs[visit_id] = leg
        return self.destination_legs[visit_id]

    def _cannot_improve(self, label):
        leg = self._destination_leg(label.visit_id)
        if leg is None:
            # Shortcut closure means no chain can reach a target whose direct
            # bypass is absent from this node's permitted movement relation.
            return True
        if self.incumbent is None:
            return False
        missing_fuel = max(
            Fraction(), leg.miles / self.vehicle.mpg - label.arrival_fuel
        )
        # Every eventual itinerary is at least the direct bypass distance and
        # cannot buy cheaper than the catalog minimum. This is a lower bound,
        # never an estimate used to discard stations from the catalog.
        return label.cost + missing_fuel * self.minimum_price >= self.incumbent.cost

    def _seed_source_labels(self):
        neighbors = self.provider.source_neighbors(self.source, self.initial_fuel)
        self._check_deadline()
        for visit in neighbors:
            self._check_deadline()
            self._check_visit(visit)
            self.transitions += 1
            leg = self.provider.leg(self.source, visit)
            fuel = self.initial_fuel - leg.miles / self.vehicle.mpg
            if fuel < 0:
                raise InvalidDistanceProvider(
                    "Source neighbor exceeds the available fuel."
                )
            label = self._offer(
                visit.visit_id,
                fuel,
                Fraction(),
                None,
                FuelAction(leg, Fraction(), Decimal(0)),
            )
            if label is not None:
                self._relax_destination(label)

    def _check_visit(self, visit):
        if self.visits.get(visit.visit_id) != visit:
            raise InvalidDistanceProvider(
                "Provider returned an unknown station context."
            )

    def _seed_incumbent(self):
        """Try a short feasible chain as an upper bound, never as an optimum.

        Strictly decreasing remaining distance avoids cycles in this optional
        construction. Its step limit does not constrain the subsequent search.
        """
        if self.incumbent is not None:
            return
        seeds = []
        for label in self.store.labels.values():
            leg = self._destination_leg(label.visit_id)
            if leg is not None:
                seeds.append((leg.miles, label.label_id))
        if not seeds:
            return
        remaining, label_id = min(seeds)
        current = self.store.get(label_id)
        for _ in range(32):
            self._check_deadline()
            best = None
            for target_id, leg, required in self._edges(current.visit_id):
                self._check_deadline()
                self.transitions += 1
                onward = self._destination_leg(target_id)
                if onward is None or onward.miles >= remaining:
                    continue
                if best is None or onward.miles < best[0]:
                    best = (onward.miles, target_id, leg, required)
            if best is None:
                return
            remaining, target_id, leg, required = best
            purchase = max(Fraction(), required - current.arrival_fuel)
            current = self._offer(
                target_id,
                current.arrival_fuel + purchase - required,
                current.cost + purchase * self.prices[current.visit_id],
                current.label_id,
                FuelAction(
                    leg, purchase, self.visits[current.visit_id].price_per_gallon
                ),
            )
            if current is None:
                return
            self._relax_destination(current)
            if self.incumbent is not None:
                return

    def _relax_destination(self, label):
        self._check_deadline()
        self.transitions += 1
        leg = self._destination_leg(label.visit_id)
        if leg is None:
            return
        required = leg.miles / self.vehicle.mpg
        if required > self.vehicle.tank_capacity_gallons:
            return
        purchase = max(Fraction(), required - label.arrival_fuel)
        price = self.prices[label.visit_id]
        self._offer(
            None,
            label.arrival_fuel + purchase - required,
            label.cost + purchase * price,
            label.label_id,
            FuelAction(leg, purchase, self.visits[label.visit_id].price_per_gallon),
        )

    def _edges(self, visit_id):
        if visit_id in self.neighbor_edges:
            self.neighbor_edges.move_to_end(visit_id)
            return self.neighbor_edges[visit_id]
        visit = self.visits[visit_id]
        edges = []
        seen = set()
        neighbors = self.provider.full_tank_neighbors(visit)
        self._check_deadline()
        for target in neighbors:
            self._check_deadline()
            self._check_visit(target)
            target_id = target.visit_id
            if target_id == visit_id or target_id in seen:
                continue
            seen.add(target_id)
            leg = self.provider.leg(visit, target)
            required = leg.miles / self.vehicle.mpg
            if required > self.vehicle.tank_capacity_gallons:
                raise InvalidDistanceProvider("Station neighbor exceeds tank capacity.")
            edges.append((target_id, leg, required))
        self.neighbor_edges[visit_id] = tuple(edges)
        if len(self.neighbor_edges) > 32:
            self.neighbor_edges.popitem(last=False)
        return self.neighbor_edges[visit_id]

    def _expand_station_label(self, label):
        if self._cannot_improve(label):
            return
        price = self.prices[label.visit_id]
        for target_id, leg, required in self._edges(label.visit_id):
            self._check_deadline()
            self.transitions += 1
            if self.prices[target_id] > price:
                purchase = self.vehicle.tank_capacity_gallons - label.arrival_fuel
            elif label.arrival_fuel <= required:
                purchase = required - label.arrival_fuel
            else:
                continue
            if purchase == 0:
                continue
            self._offer(
                target_id,
                label.arrival_fuel + purchase - required,
                label.cost + purchase * price,
                label.label_id,
                FuelAction(leg, purchase, self.visits[label.visit_id].price_per_gallon),
            )

    def _result(self, complete, reason):
        label = self.incumbent
        qualities = set()
        estimated = False
        while label is not None:
            estimated |= label.action.leg.distance_quality not in {
                "ors_baseline",
                "station_identity",
            }
            if label.predecessor_id is None:
                break
            predecessor = self.store.get(label.predecessor_id)
            if label.action.gallons_purchased > 0:
                qualities.add(self.visits[predecessor.visit_id].coordinate_quality)
            label = predecessor
        quality = PlanQuality(
            movement_model=self.incumbent.action.leg.movement_model,
            distance_quality="estimated_access" if estimated else "ors_baseline",
            station_location_quality=(
                next(iter(qualities))
                if len(qualities) == 1
                else "mixed"
                if qualities
                else "not_applicable"
            ),
            search_complete=complete,
            optimality_scope="declared_movement_model"
            if complete
            else "not_established",
        )
        return reconstruct_plan(
            self.incumbent,
            self.store.labels,
            self.visits,
            initial_fuel_gallons=self.initial_fuel,
            vehicle=self.vehicle,
            destination=self.destination,
            quality=quality,
            statistics=self._statistics(reason),
        )

    def run(self):
        try:
            self._check_deadline()
            self.transitions += 1
            try:
                direct = self.provider.leg(self.source, self.destination)
            except MovementNotPermitted:
                raise NoFeasiblePlan(self._statistics("exhausted")) from None
            required = direct.miles / self.vehicle.mpg
            if required <= self.initial_fuel:
                self._offer(
                    None,
                    self.initial_fuel - required,
                    Fraction(),
                    None,
                    FuelAction(direct, Fraction(), Decimal(0)),
                )
                self.settled += 1
                return self._result(True, "optimal")
            self._seed_source_labels()
            self._seed_incumbent()
            while self.queue:
                self._check_deadline()
                _, _, label_id = heapq.heappop(self.queue)
                if not self.store.is_active(label_id):
                    self.stale += 1
                    continue
                label = self.store.get(label_id)
                self.settled += 1
                if label.visit_id is None:
                    # A generated destination is only an upper bound. An active
                    # destination popped at minimum queue cost proves the result.
                    self.incumbent = label
                    return self._result(True, "optimal")
                self._relax_destination(label)
                self._expand_station_label(label)
            self._check_deadline()
        except _BudgetReached as exc:
            reason = str(exc)
            if self.incumbent is not None:
                return self._result(False, reason)
            raise SearchIncomplete(self._statistics(reason)) from None
        raise NoFeasiblePlan(self._statistics("exhausted"))


def plan_fuel_stops(
    source,
    destination,
    visits,
    vehicle,
    distance_provider,
    *,
    initial_fuel_gallons=Decimal(50),
    budget=None,
    enable_dominance=True,
    clock=None,
):
    """Minimize purchases, preserving exact quantities and every traveled leg."""
    if not isinstance(source, RouteSource) or not isinstance(
        destination, RoutePosition
    ):
        raise TypeError("Use explicit RouteSource and RoutePosition contexts.")
    if not isinstance(vehicle, VehicleSpec):
        raise TypeError("Vehicle must be a VehicleSpec.")
    if getattr(distance_provider, "shortcut_closed", False) is not True:
        raise InvalidDistanceProvider(
            "Fuel search requires a shortcut-closed distance provider."
        )
    if type(enable_dominance) is not bool:
        raise TypeError("enable_dominance must be a boolean.")
    budget = SearchBudget() if budget is None else budget
    if not isinstance(budget, SearchBudget):
        raise TypeError("budget must be a SearchBudget.")
    return _FuelSearch(
        source,
        destination,
        tuple(visits),
        vehicle,
        distance_provider,
        initial_fuel_gallons,
        budget,
        enable_dominance,
        time.monotonic if clock is None else clock,
    ).run()
