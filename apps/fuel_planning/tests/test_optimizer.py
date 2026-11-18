"""Known fuel costs, range limits, and a small independent solver comparison."""

from decimal import Decimal
from fractions import Fraction
from unittest import TestCase

from apps.fuel_planning.services.distances import RouteExcursionDistanceProvider
from apps.fuel_planning.services.optimizer import NoFeasiblePlan, plan_fuel_stops
from apps.fuel_planning.services.reconstruction import verify_fuel_conservation
from apps.fuel_planning.types import RouteSource, VehicleSpec

from .fixtures import position, route_measure, visit
from .oracle import exhaustive_unit_fuel_cost

ASSIGNMENT_STATIONS = (
    ("A", 350, 4, 0, 0),
    ("B", 450, 3, 0, 0),
    ("C", 800, "3.5", 0, 0),
    ("D", 900, 4, 0, 0),
)
DETOUR_STATIONS = (("A", 450, 4, 0, 0), ("B", 450, "3.5", 3, 3))


def plan(miles, stations, *, dominance=True):
    measure = route_measure(miles)
    visits = tuple(
        visit(name, progress, outward, returning, measure=measure, price=price)
        for name, progress, price, outward, returning in stations
    )
    vehicle = VehicleSpec()
    provider = RouteExcursionDistanceProvider(measure, visits, vehicle)
    return plan_fuel_stops(
        RouteSource(position(0, measure=measure)),
        provider.destination,
        visits,
        vehicle,
        provider,
        initial_fuel_gallons=50,
        enable_dominance=dominance,
    )


class FuelOptimizerTests(TestCase):
    def test_multistop_trip_minimizes_purchase_cost(self):
        result = plan(1200, ASSIGNMENT_STATIONS)
        self.assertEqual([stop.station_id for stop in result.stops], ["B", "C"])
        self.assertEqual([stop.gallons_purchased for stop in result.stops], [45, 25])
        self.assertEqual(result.purchase_cost, Fraction("222.50"))
        self.assertEqual(result.gallons_consumed, 120)
        self.assertEqual(result.final_fuel_gallons, 0)
        self.assertTrue(result.quality.search_complete)
        verify_fuel_conservation(result)

    def test_detours_count_toward_fuel_and_cost(self):
        result = plan(850, DETOUR_STATIONS)
        self.assertEqual([stop.station_id for stop in result.stops], ["B"])
        self.assertEqual(result.total_driving_miles, 856)
        self.assertEqual(result.gallons_purchased, Fraction("35.6"))
        self.assertEqual(result.purchase_cost, Fraction("124.60"))
        verify_fuel_conservation(result)

    def test_tank_range_is_500_miles(self):
        result = plan(500, ())
        self.assertEqual(result.purchase_cost, 0)
        self.assertEqual(result.final_fuel_gallons, 0)
        with self.assertRaises(NoFeasiblePlan):
            plan(501, ())

    def test_optimizer_matches_independent_small_graph_search(self):
        cases = (
            (1200, ASSIGNMENT_STATIONS, Fraction(1)),
            (850, DETOUR_STATIONS, Fraction(1, 10)),
            (600, (("paid", 100, 3, 0, 0), ("free", 100, 0, 0, 0)), Fraction(1)),
            (1000, (), Fraction(1)),
        )
        for miles, stations, fuel_unit in cases:
            # Build edges independently of the production distance provider.
            origins = [("start", 0, 0), *[(s[0], s[1], s[4]) for s in stations]]
            targets = [("finish", miles, 0), *[(s[0], s[1], s[3]) for s in stations]]
            graph = {
                origin: {
                    target: Fraction(returning) + end - start + outward
                    for target, end, outward in targets
                    if target != origin and end >= start
                }
                for origin, start, returning in origins
            }
            expected = exhaustive_unit_fuel_cost(
                graph,
                {s[0]: Decimal(str(s[2])) for s in stations},
                source="start",
                destination="finish",
                capacity=50,
                initial_fuel=50,
                mpg=10,
                fuel_unit=fuel_unit,
            )
            for dominance in (False, True):
                with self.subTest(miles=miles, dominance=dominance):
                    if expected is None:
                        with self.assertRaises(NoFeasiblePlan):
                            plan(miles, stations, dominance=dominance)
                    else:
                        result = plan(miles, stations, dominance=dominance)
                        self.assertEqual(result.purchase_cost, expected)
                        self.assertTrue(result.quality.search_complete)
                        verify_fuel_conservation(result)
