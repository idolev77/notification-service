"""
Test 6 — Status tracking (PRD §7.5 + §3.4).

Drives `GET /notifications/{id}` against a stubbed DB session that returns
a notification with two deliveries, and asserts the response shape matches
`NotificationStatusView`.

Also covers the 404 path so the router's not-found handling is locked in.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db_session
from app.main import create_app
from app.models.enums import (
    ChannelType,
    DeliveryStatus,
    NotificationPriority,
    NotificationStatus,
)


def _fake_notification(notif_id: uuid.UUID, deliveries: list[object]):
    """Build a SimpleNamespace mirror of `Notification` (only attrs read)."""
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=notif_id,
        notification_type="welcome",
        recipient_user_id="u1",
        recipient_contact=None,
        priority=NotificationPriority.NORMAL,
        status=NotificationStatus.PROCESSING,
        scheduled_at=None,
        created_at=now,
        updated_at=now,
        deliveries=deliveries,
    )


def _fake_delivery(channel: ChannelType, status: DeliveryStatus, attempts: int = 1):
    return SimpleNamespace(
        id=uuid.uuid4(),
        channel=channel,
        recipient_address="ada@example.com" if channel is ChannelType.EMAIL else "+15555555555",
        status=status,
        attempts=attempts,
        last_attempt_at=None,
        delivered_at=None,
        error_message=None,
        provider_response=None,
    )


@pytest.fixture
def fake_session_factory():
    """
    Build a fake DB session whose `execute(...).scalar_one_or_none()` and
    `get(...)` return canned values.
    """
    notif_id = uuid.uuid4()
    notification = _fake_notification(
        notif_id,
        [
            _fake_delivery(ChannelType.EMAIL, DeliveryStatus.DELIVERED),
            _fake_delivery(ChannelType.SMS, DeliveryStatus.QUEUED, attempts=0),
        ],
    )

    class _Result:
        def scalar_one_or_none(self_inner):
            return notification

    fake_session = SimpleNamespace(
        execute=lambda stmt: _Result(),
        get=lambda model, key: notification if key == notif_id else None,
    )

    def _factory():
        yield fake_session

    return notif_id, _factory


def test_get_notification_status_returns_aggregate_view(fake_session_factory) -> None:
    notif_id, factory = fake_session_factory
    app = create_app()
    app.dependency_overrides[get_db_session] = factory
    client = TestClient(app)

    response = client.get(f"/notifications/{notif_id}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["id"] == str(notif_id)
    assert body["status"] == "processing"
    # Per-channel view is included and ordered as inserted.
    channels = [d["channel"] for d in body["deliveries"]]
    assert channels == ["email", "sms"]
    # The terminal-state delivery surfaces correctly.
    email_row = next(d for d in body["deliveries"] if d["channel"] == "email")
    assert email_row["status"] == "delivered"


def test_get_notification_status_404_when_missing() -> None:
    """Unknown id → 404 (router translates None into HTTPException)."""

    class _NoneResult:
        def scalar_one_or_none(self):
            return None

    fake_session = SimpleNamespace(
        execute=lambda stmt: _NoneResult(),
        get=lambda model, key: None,
    )

    app = create_app()

    def _factory():
        yield fake_session

    app.dependency_overrides[get_db_session] = _factory
    client = TestClient(app)

    response = client.get(f"/notifications/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_malformed_uuid_in_path_returns_422() -> None:
    """
    A non-UUID string in the `{id}` path parameter must return 422, not 500.
    FastAPI's path parameter coercion must catch the parse error at the
    routing layer before the handler or DB are ever touched.
    """
    # No DB override needed — the router rejects before reaching any handler.
    app = create_app()
    client = TestClient(app)
    response = client.get("/notifications/not-a-uuid")
    assert response.status_code == 422


def test_notification_with_zero_deliveries_returns_empty_list(fake_session_factory) -> None:
    """
    A Notification that has been created but not yet dispatched (zero
    Delivery rows) must return 200 with an empty `deliveries` list — not a
    404 or a serialization crash.
    """
    notif_id, _ = fake_session_factory

    now = datetime.now(timezone.utc)
    zero_delivery_notif = SimpleNamespace(
        id=notif_id,
        notification_type="welcome",
        recipient_user_id="u1",
        recipient_contact=None,
        priority=NotificationPriority.NORMAL,
        status=NotificationStatus.PROCESSING,
        scheduled_at=None,
        created_at=now,
        updated_at=now,
        deliveries=[],  # <-- the edge case
    )

    class _Result:
        def scalar_one_or_none(self_inner):
            return zero_delivery_notif

    fake_session = SimpleNamespace(
        execute=lambda stmt: _Result(),
        get=lambda model, key: zero_delivery_notif if key == notif_id else None,
    )

    app = create_app()

    def _factory():
        yield fake_session

    app.dependency_overrides[get_db_session] = _factory
    client = TestClient(app)

    response = client.get(f"/notifications/{notif_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["deliveries"] == []
    assert body["status"] == "processing"
