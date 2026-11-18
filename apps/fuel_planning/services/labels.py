"""Exact search labels and per-visit dominance without losing predecessors."""

from dataclasses import dataclass
from fractions import Fraction

from apps.fuel_planning.types import FuelAction, VisitKey

from .numeric import nonnegative_amount


def _check_visit_id(visit_id):
    if visit_id is not None and (
        not isinstance(visit_id, tuple)
        or len(visit_id) != 3
        or any(not isinstance(part, str) or not part for part in visit_id)
    ):
        raise ValueError(
            "A visit identity must contain station, attachment and access context."
        )


@dataclass(frozen=True, slots=True)
class SearchLabel:
    """An immutable arrival; ``visit_id=None`` represents the destination.

    The incoming action purchases at the predecessor before driving its leg.
    Source seeds have no predecessor and carry a zero-purchase source leg.
    """

    label_id: int
    visit_id: VisitKey | None
    arrival_fuel: Fraction
    cost: Fraction
    predecessor_id: int | None
    action: FuelAction

    def __post_init__(self):
        if type(self.label_id) is not int:
            raise TypeError("A label ID must be an integer.")
        if self.label_id < 0:
            raise ValueError("A label ID must be nonnegative.")
        if self.predecessor_id is not None:
            if type(self.predecessor_id) is not int:
                raise TypeError("A predecessor ID must be an integer.")
            if self.predecessor_id < 0 or self.predecessor_id == self.label_id:
                raise ValueError("A label needs a distinct nonnegative predecessor ID.")
        _check_visit_id(self.visit_id)
        if not isinstance(self.action, FuelAction):
            raise TypeError("A label needs its incoming fuel action.")
        object.__setattr__(self, "arrival_fuel", nonnegative_amount(self.arrival_fuel))
        object.__setattr__(self, "cost", nonnegative_amount(self.cost))


class LabelStore:
    """Keep the active Pareto frontier and all accepted reconstruction history."""

    def __init__(self, enable_dominance=True):
        if type(enable_dominance) is not bool:
            raise TypeError("The dominance option must be a boolean.")
        self.enable_dominance = enable_dominance
        self.labels: dict[int, SearchLabel] = {}
        self._seen_ids: set[int] = set()
        self._active: set[int] = set()
        self._frontiers: dict[VisitKey | None, dict[Fraction, int]] = {}
        self._best_states: dict[tuple[VisitKey | None, Fraction], int] = {}

    def get(self, label_id: int) -> SearchLabel:
        return self.labels[label_id]

    def is_active(self, label_id: int) -> bool:
        return label_id in self._active

    def is_dominated(self, visit_id, fuel, cost) -> bool:
        _check_visit_id(visit_id)
        fuel, cost = nonnegative_amount(fuel), nonnegative_amount(cost)
        previous_id = self._best_states.get((visit_id, fuel))
        if previous_id is not None and self.labels[previous_id].cost <= cost:
            return True
        if not self.enable_dominance:
            return False
        return any(
            self.labels[label_id].cost <= cost
            and self.labels[label_id].arrival_fuel >= fuel
            for label_id in self._frontiers.get(visit_id, {}).values()
        )

    def accept(self, label: SearchLabel) -> bool:
        if not isinstance(label, SearchLabel):
            raise TypeError("The store accepts SearchLabel instances.")
        if label.label_id in self._seen_ids:
            raise ValueError("Label IDs must be unique, including rejected candidates.")
        if label.predecessor_id is not None and label.predecessor_id not in self.labels:
            raise ValueError("A predecessor must be an accepted label.")
        self._seen_ids.add(label.label_id)
        if self.is_dominated(label.visit_id, label.arrival_fuel, label.cost):
            return False

        frontier = self._frontiers.get(label.visit_id, {})
        obsolete = [
            existing_id
            for existing_id in frontier.values()
            if self.labels[existing_id].cost >= label.cost
            and (
                self.labels[existing_id].arrival_fuel == label.arrival_fuel
                or (
                    self.enable_dominance
                    and self.labels[existing_id].arrival_fuel <= label.arrival_fuel
                )
            )
        ]
        for existing_id in obsolete:
            self.mark_stale(existing_id)
        self.labels[label.label_id] = label
        self._active.add(label.label_id)
        self._frontiers.setdefault(label.visit_id, {})[label.arrival_fuel] = (
            label.label_id
        )
        self._best_states[(label.visit_id, label.arrival_fuel)] = label.label_id
        return True

    def mark_stale(self, label_id: int):
        """Retire an arrival from expansion while preserving its immutable history."""
        label = self.labels[label_id]
        self._active.discard(label_id)
        frontier = self._frontiers.get(label.visit_id, {})
        if frontier.get(label.arrival_fuel) == label_id:
            del frontier[label.arrival_fuel]
            if not frontier:
                del self._frontiers[label.visit_id]
