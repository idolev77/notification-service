"""
Test 2 — Preference filtering (PRD §7.5 + §4.2 + §4.3).

Pure-unit tests of `resolve_channels_for_notification` covering:
  - Channel-override bypass (PRD §4.7)
  - Paused user → empty channels
  - Per-type narrowing intersects with global enabled set
  - Quiet-hours filter drops everything for non-HIGH
  - HIGH priority bypasses quiet hours (§4.3)
  - Frequency-cap denial drops everything

Each case constructs the model objects in-memory (no DB session needed).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest

from app.core.rate_limiter import CapDecision
from app.models.enums import ChannelType, NotificationPriority
from app.services.preference_resolver import resolve_channels_for_notification


def _make_notification(
    *, notification_type: str = "welcome", priority: NotificationPriority = NotificationPriority.NORMAL
):
    return SimpleNamespace(
        recipient_user_id="u1",
        notification_type=notification_type,
        priority=priority,
    )


def _make_prefs(
    *,
    enabled=("email", "sms"),
    per_type=None,
    is_paused=False,
    quiet_start=None,
    quiet_end=None,
    quiet_tz=None,
    frequency_caps=None,
    webhook_url=None,
):
    return SimpleNamespace(
        user_id="u1",
        enabled_channels=list(enabled),
        per_type_preferences=per_type or {},
        is_paused=is_paused,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        quiet_hours_timezone=quiet_tz,
        frequency_caps=frequency_caps or {},
        webhook_url=webhook_url,
    )


def test_override_bypasses_all_filters() -> None:
    result = resolve_channels_for_notification(
        notification=_make_notification(),
        user_pref=_make_prefs(is_paused=True),  # pause is normally blocking
        channels_override=[ChannelType.EMAIL],
    )
    assert result.used_override is True
    assert result.channels == [ChannelType.EMAIL]


def test_paused_user_yields_no_channels() -> None:
    result = resolve_channels_for_notification(
        notification=_make_notification(),
        user_pref=_make_prefs(is_paused=True),
        channels_override=None,
    )
    assert result.channels == []
    assert result.filtered_by_pause is True


def test_per_type_narrows_global_enabled_set() -> None:
    result = resolve_channels_for_notification(
        notification=_make_notification(notification_type="alerts"),
        user_pref=_make_prefs(
            enabled=("email", "sms", "push"),
            per_type={"alerts": ["sms"]},  # narrows
        ),
        channels_override=None,
    )
    assert result.channels == [ChannelType.SMS]


def test_quiet_hours_blocks_non_high_priority() -> None:
    # Window: 22:00 → 07:00 in UTC; pick a "now" inside the window.
    result = resolve_channels_for_notification(
        notification=_make_notification(priority=NotificationPriority.NORMAL),
        user_pref=_make_prefs(
            quiet_start=time(22, 0),
            quiet_end=time(7, 0),
            quiet_tz="UTC",
        ),
        channels_override=None,
        now=datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc),
    )
    assert result.channels == []
    assert result.filtered_by_quiet_hours is True


def test_high_priority_bypasses_quiet_hours() -> None:
    result = resolve_channels_for_notification(
        notification=_make_notification(priority=NotificationPriority.HIGH),
        user_pref=_make_prefs(
            quiet_start=time(22, 0),
            quiet_end=time(7, 0),
            quiet_tz="UTC",
        ),
        channels_override=None,
        now=datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc),
    )
    assert result.filtered_by_quiet_hours is False
    assert ChannelType.EMAIL in result.channels


def test_frequency_cap_denial_drops_all_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the limiter says no, the resolver returns empty channels."""

    def _deny(*, user_id: str, caps):  # noqa: ANN001, ARG001
        return CapDecision(allowed=False, tripped_window="per_hour")

    monkeypatch.setattr(
        "app.services.preference_resolver.check_and_consume", _deny
    )

    result = resolve_channels_for_notification(
        notification=_make_notification(),
        user_pref=_make_prefs(),
        channels_override=None,
    )
    assert result.channels == []
    assert result.filtered_by_frequency_cap is True
    assert result.tripped_cap_window == "per_hour"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_no_preferences_row_yields_no_channels() -> None:
    """
    When `user_pref` is None (row does not exist in DB), the resolver must
    return an empty channel list with `missing_preferences=True`.
    This prevents a KeyError / AttributeError from reaching the caller.
    """
    result = resolve_channels_for_notification(
        notification=_make_notification(),
        user_pref=None,
        channels_override=None,
    )
    assert result.channels == []
    assert result.missing_preferences is True


def test_empty_enabled_channels_yields_no_channels() -> None:
    """
    A user who has cleared every channel from their preferences should
    receive no deliveries, even without being paused.
    The resolver must not crash on an empty list.
    """
    result = resolve_channels_for_notification(
        notification=_make_notification(),
        user_pref=_make_prefs(enabled=()),
        channels_override=None,
    )
    assert result.channels == []
    # Not a pause, not a quiet-hours filter — just no channels selected.
    assert result.filtered_by_pause is False
    assert result.filtered_by_quiet_hours is False


def test_webhook_dropped_when_no_url_other_channels_survive() -> None:
    """
    Step 7: if WEBHOOK is in `enabled_channels` but `webhook_url` is absent,
    the webhook channel is silently dropped. Email/SMS must still be delivered.
    """
    result = resolve_channels_for_notification(
        notification=_make_notification(),
        user_pref=_make_prefs(enabled=("email", "webhook"), webhook_url=None),
        channels_override=None,
    )
    assert ChannelType.WEBHOOK not in result.channels
    assert ChannelType.EMAIL in result.channels


def test_per_type_intersection_with_empty_result() -> None:
    """
    When per_type_preferences narrows to a channel not in enabled_channels
    the intersection is empty — no deliveries. The system must not fall
    back to the global enabled set after the intersection.
    """
    result = resolve_channels_for_notification(
        notification=_make_notification(notification_type="alerts"),
        user_pref=_make_prefs(
            enabled=("email", "sms"),
            per_type={"alerts": ["push"]},  # push not in enabled → intersection = []
        ),
        channels_override=None,
    )
    assert result.channels == []


def test_quiet_hours_start_equals_end_is_always_quiet() -> None:
    """
    Edge case documented in the resolver: start == end is the convention for
    "always quiet" (24h window). A NORMAL-priority notification at any time
    of day must be blocked.
    """
    result = resolve_channels_for_notification(
        notification=_make_notification(priority=NotificationPriority.NORMAL),
        user_pref=_make_prefs(
            quiet_start=time(12, 0),
            quiet_end=time(12, 0),  # equal → always quiet
            quiet_tz="UTC",
        ),
        channels_override=None,
        now=datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc),  # mid-afternoon
    )
    assert result.channels == []
    assert result.filtered_by_quiet_hours is True


def test_high_priority_bypasses_frequency_cap_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    HIGH-priority notifications must bypass frequency caps entirely when
    `frequency_cap_high_priority_bypass=True` (the default). The resolver
    must not even call `check_and_consume` for HIGH-priority messages.
    """
    call_count = {"n": 0}

    def _deny(*, user_id: str, caps):  # noqa: ANN001, ARG001
        call_count["n"] += 1
        return CapDecision(allowed=False, tripped_window="per_hour")

    monkeypatch.setattr(
        "app.services.preference_resolver.check_and_consume", _deny
    )

    result = resolve_channels_for_notification(
        notification=_make_notification(priority=NotificationPriority.HIGH),
        user_pref=_make_prefs(),
        channels_override=None,
    )
    # HIGH priority must succeed even though check_and_consume would deny.
    assert result.filtered_by_frequency_cap is False
    assert len(result.channels) > 0
    # Confirm bypass happened BEFORE calling the limiter.
    assert call_count["n"] == 0
