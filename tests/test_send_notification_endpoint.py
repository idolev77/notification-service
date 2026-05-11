"""
Test 1 — Send notification end-to-end (PRD §7.5).

Exercises the public HTTP boundary:
  POST /notifications  →  202 Accepted  →  service called  →  Celery enqueued.

Approach: stub the service & Celery task at the import-time seam so the test
needs no DB and no broker. We assert the request reached the service layer
unchanged (Pydantic round-trip) and that the response shape conforms to
`SendNotificationResponse`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.enums import NotificationStatus


@pytest.fixture
def captured_calls() -> list[dict]:
    return []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, captured_calls: list[dict]) -> TestClient:
    """
    Replace the service entrypoint with a recorder that returns a fake
    Notification. This isolates the route from DB + Celery.
    """
    fake_id = uuid.uuid4()
    created = datetime.now(timezone.utc)

    def _fake_service(*, session, request):  # noqa: ANN001, ARG001
        captured_calls.append({
            "notification_type": request.notification_type,
            "recipient_user_id": request.recipient_user_id,
            "recipient_contact": request.recipient_contact,
            "priority": request.priority,
        })
        return SimpleNamespace(
            id=fake_id,
            status=NotificationStatus.PROCESSING,
            scheduled_at=None,
            created_at=created,
        )

    # Patch at the API import site (FastAPI binds the symbol at import time).
    monkeypatch.setattr(
        "app.api.notifications.create_and_dispatch_notification", _fake_service
    )
    # Skip the real DB session dependency — return a fake that no-ops commit.
    from app.core.db import get_db_session
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: SimpleNamespace(
        commit=lambda: None, rollback=lambda: None
    )
    return TestClient(app)


def test_send_notification_returns_202_with_persisted_id(
    client: TestClient, captured_calls: list[dict]
) -> None:
    response = client.post(
        "/notifications",
        json={
            "recipient_contact": "ada@example.com",
            "notification_type": "welcome",
            "content": "Hello, {{user.name}}!",
            "variables": {"user": {"name": "Ada"}},
        },
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "processing"
    assert uuid.UUID(payload["id"])  # round-trips as a real UUID

    # Service was invoked exactly once with the expected parameters.
    assert len(captured_calls) == 1
    assert captured_calls[0]["notification_type"] == "welcome"
    assert captured_calls[0]["recipient_contact"] == "ada@example.com"


def test_send_notification_rejects_missing_recipient(client: TestClient) -> None:
    """422 from Pydantic — XOR rule on recipient fields."""
    response = client.post(
        "/notifications",
        json={"notification_type": "welcome", "content": "hi"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_both_recipient_fields_rejected(client: TestClient) -> None:
    """
    Providing both `recipient_user_id` AND `recipient_contact` violates the
    XOR rule and must return 422 — the service cannot know which to use.
    """
    response = client.post(
        "/notifications",
        json={
            "recipient_user_id": "user-123",
            "recipient_contact": "ada@example.com",
            "notification_type": "welcome",
            "content": "hi",
        },
    )
    assert response.status_code == 422


def test_empty_channels_override_rejected(client: TestClient) -> None:
    """
    `channels_override: []` (explicitly empty) must be rejected with 422.
    An empty override would silently suppress all deliveries, which is almost
    certainly a client bug.
    """
    response = client.post(
        "/notifications",
        json={
            "recipient_contact": "ada@example.com",
            "notification_type": "welcome",
            "content": "hi",
            "channels_override": [],
        },
    )
    assert response.status_code == 422


def test_unknown_extra_field_rejected(client: TestClient) -> None:
    """
    `extra="forbid"` on the schema must reject any unknown key with 422.
    This guards against typos like `recepient_user_id` being silently
    ignored, which would result in a dropped or mis-routed notification.
    """
    response = client.post(
        "/notifications",
        json={
            "recipient_contact": "ada@example.com",
            "notification_type": "welcome",
            "content": "hi",
            "typo_field": "oops",
        },
    )
    assert response.status_code == 422


def test_content_at_exact_max_length_accepted(client: TestClient) -> None:
    """A body of exactly 64 000 chars must be accepted (boundary: in-range)."""
    response = client.post(
        "/notifications",
        json={
            "recipient_contact": "ada@example.com",
            "notification_type": "welcome",
            "content": "x" * 64_000,
        },
    )
    assert response.status_code == 202


def test_content_exceeding_max_length_rejected(client: TestClient) -> None:
    """A body of 64 001 chars must be rejected with 422 (boundary: out-of-range)."""
    response = client.post(
        "/notifications",
        json={
            "recipient_contact": "ada@example.com",
            "notification_type": "welcome",
            "content": "x" * 64_001,
        },
    )
    assert response.status_code == 422
