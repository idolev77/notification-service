"""
Pytest configuration & shared fixtures for the notification-service test suite.

Design choices (defensible):
  - Tests run with ZERO infrastructure: no Postgres, no Redis, no Celery worker.
    We isolate the SUT (system under test) by injecting fakes/mocks at the
    well-defined seams the production code already exposes:
        * FastAPI `dependency_overrides` for the DB session.
        * Celery `apply_async` / `delay` patched on the per-channel tasks
          for any test that exercises `enqueue_delivery`.
        * `app.core.rate_limiter.check_and_consume` patched to allow-all
          so the resolver path never reaches Redis.
  - This keeps the suite fast (`pytest -q` < 1s) and CI-friendly. Integration
    tests against a real stack are out of scope for the exam — see
    DECISIONS.md §5 ("what I'd do differently").
"""

from __future__ import annotations

import random

import pytest

from app.core.rate_limiter import CapDecision


@pytest.fixture(autouse=True)
def _seed_random() -> None:
    """Make any random.* call deterministic per test."""
    random.seed(1234)


@pytest.fixture(autouse=True)
def _allow_all_frequency_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Default fixture: rate limiter says yes. Tests that want to exercise
    cap-tripping behaviour patch `check_and_consume` themselves.
    """
    def _allow(*, user_id: str, caps):  # noqa: ANN001, ARG001
        return CapDecision(allowed=True)

    monkeypatch.setattr(
        "app.services.preference_resolver.check_and_consume", _allow
    )
