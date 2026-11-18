"""Request-scoped provider attempts; cache hits never increment the counters."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RequestMetrics:
    provider_calls: dict = field(default_factory=lambda: {"nominatim": 0, "ors": 0})


_current = ContextVar("trip_request_metrics", default=None)


@contextmanager
def collect_provider_metrics():
    metrics = RequestMetrics()
    token = _current.set(metrics)
    try:
        yield metrics
    finally:
        _current.reset(token)


@contextmanager
def provider_attempt(provider):
    metrics = _current.get()
    if metrics is not None:
        metrics.provider_calls[provider] += 1
    yield
