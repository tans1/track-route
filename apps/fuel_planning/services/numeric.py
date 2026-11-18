"""Exact arithmetic for the declared model; geographic estimates stay estimates."""

from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction

METERS_PER_MILE = Fraction("1609.344")


def as_exact_amount(value) -> Fraction:
    if isinstance(value, bool):
        raise TypeError("Boolean values are not amounts.")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if not isinstance(value, (str, Decimal, float)):
        raise TypeError("Use a decimal number or decimal string.")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Amount must be a finite decimal number.") from exc
    if not decimal.is_finite():
        raise ValueError("Amount must be finite.")
    return Fraction(decimal)


def nonnegative_amount(value) -> Fraction:
    amount = as_exact_amount(value)
    if amount < 0:
        raise ValueError("Amount must not be negative.")
    return amount


def meters_to_miles(meters) -> Fraction:
    return nonnegative_amount(meters) / METERS_PER_MILE


def gallons_for_distance(miles, mpg=10) -> Fraction:
    efficiency = as_exact_amount(mpg)
    if efficiency <= 0:
        raise ValueError("Fuel economy must be positive.")
    return nonnegative_amount(miles) / efficiency


def validate_fuel(gallons, capacity=50) -> Fraction:
    fuel = nonnegative_amount(gallons)
    if fuel > nonnegative_amount(capacity):
        raise ValueError("Fuel exceeds tank capacity.")
    return fuel


def coordinates(value) -> tuple[float, float]:
    """Validate a [longitude, latitude] point without treating booleans as numbers."""
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Coordinates require longitude and latitude.")
    result = []
    for component, limit in zip(value, (180, 90), strict=True):
        amount = as_exact_amount(component)
        if not -limit <= amount <= limit:
            raise ValueError("Coordinate is out of range.")
        number = float(amount)
        result.append(number if number else 0.0)
    return tuple(result)


def format_money(value, places=2) -> str:
    """Display-only, exact half-up rounding, independent of Decimal context."""
    if type(places) is not int or not 0 <= places <= 12:
        raise ValueError("Display precision must be between 0 and 12 places.")
    amount = as_exact_amount(value)
    scaled = abs(amount) * 10**places
    units, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder * 2 >= scaled.denominator:
        units += 1
    if amount < 0:
        units = -units
    with localcontext() as context:
        context.prec = max(28, len(str(abs(units))) + places + 1)
        return format(Decimal(units).scaleb(-places), f".{places}f")
