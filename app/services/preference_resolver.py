"""
Preference resolution pipeline (PRD §4.2 + §4.3 + §4.7).

Pure function over (Notification, UserPreferences | None, channels_override)
that returns the list of channels we should attempt to deliver on. Side-effect
free: the caller decides how to materialize the result (create Deliveries,
log a "filtered" event, etc.).

Resolution order (deterministic — see DECISIONS.md §3):

  Step 1. channels_override (PRD §4.7) — if present, BYPASS all preference
          filtering and use the override verbatim. Rationale: override is a
          deliberate ops/admin escape hatch (password reset, security alert).

  Step 2. If no UserPreferences row exists or is_paused=True → no channels.

  Step 3. Start with `enabled_channels` (the global allow-list).

  Step 4. If `per_type_preferences[notification_type]` is set, intersect it
          with `enabled_channels` (per-type narrows; it never widens).

  Step 5. Quiet hours (PRD §4.3): if "now" falls inside the user's quiet
          window AND the notification is not HIGH priority, drop ALL
          channels. HIGH priority bypasses quiet hours unconditionally.

  Step 6. Frequency-cap enforcement (PRD §4.6) — Sprint 4 only; this
          resolver records the caps but does not enforce them.

  Step 7. WEBHOOK requires a configured `user_pref.webhook_url`; if absent
          the channel is dropped (logged) so we never ship a webhook
          delivery without a destination.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.rate_limiter import check_and_consume
from app.models import Notification, UserPreferences
from app.models.enums import ChannelType, NotificationPriority

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """The output of the resolver — channels + a structured reason trace."""

    channels: list[ChannelType]
    # Diagnostic flags for logging / response metadata. Not exposed via API.
    used_override: bool = False
    filtered_by_pause: bool = False
    filtered_by_quiet_hours: bool = False
    filtered_by_frequency_cap: bool = False
    tripped_cap_window: str | None = None
    missing_preferences: bool = False


def resolve_channels_for_notification(
    *,
    notification: Notification,
    user_pref: UserPreferences | None,
    channels_override: list[ChannelType] | None,
    now: datetime | None = None,
) -> ResolutionResult:
    """
    Compute the channels to dispatch on. See module docstring for the
    full step-by-step.

    `now` is injected for testability; defaults to wall-clock UTC.
    """
    # Step 1 — explicit override wins, no filtering applied.
    if channels_override:
        return ResolutionResult(channels=list(channels_override), used_override=True)

    # Step 2 — no prefs / paused.
    if user_pref is None:
        _logger.info("preferences.missing", user_id=notification.recipient_user_id)
        return ResolutionResult(channels=[], missing_preferences=True)
    if user_pref.is_paused:
        _logger.info("preferences.paused", user_id=user_pref.user_id)
        return ResolutionResult(channels=[], filtered_by_pause=True)

    # Step 3 — global enabled set.
    enabled = _coerce_channels(user_pref.enabled_channels)

    # Step 4 — per-type narrowing (intersection with `enabled`).
    per_type_raw = user_pref.per_type_preferences.get(notification.notification_type)
    if per_type_raw:
        per_type = _coerce_channels(per_type_raw)
        candidates = [c for c in per_type if c in enabled]
    else:
        candidates = list(enabled)

    # Step 5 — quiet hours (HIGH priority bypasses, per §4.3).
    in_quiet = _is_in_quiet_hours(user_pref, now or datetime.now(tz=ZoneInfo("UTC")))
    is_high = notification.priority is NotificationPriority.HIGH
    if in_quiet and not is_high:
        _logger.info(
            "preferences.quiet_hours_filtered",
            user_id=user_pref.user_id,
            notification_type=notification.notification_type,
        )
        return ResolutionResult(channels=[], filtered_by_quiet_hours=True)

    # Step 6 — frequency caps (PRD §4.6). HIGH priority bypasses when
    # `frequency_cap_high_priority_bypass` is enabled (default true) —
    # documented in DECISIONS §3 step 6.
    cap_decision = _check_frequency_caps(user_pref=user_pref, is_high=is_high)
    if not cap_decision.allowed:
        return ResolutionResult(
            channels=[],
            filtered_by_frequency_cap=True,
            tripped_cap_window=cap_decision.tripped_window,
        )

    # Step 7 — webhook needs a destination URL on the profile.
    if ChannelType.WEBHOOK in candidates and not user_pref.webhook_url:
        _logger.warning(
            "preferences.webhook_dropped_no_url", user_id=user_pref.user_id
        )
        candidates = [c for c in candidates if c is not ChannelType.WEBHOOK]

    return ResolutionResult(channels=candidates)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_channels(raw: list[str]) -> list[ChannelType]:
    """
    Convert JSONB-stored strings into ChannelType, dropping unknowns.

    Defensive: an unknown channel could exist in JSONB if a future enum
    value was inserted by an old worker. We log + skip rather than crash.
    """
    out: list[ChannelType] = []
    for c in raw:
        try:
            out.append(ChannelType(c))
        except ValueError:
            _logger.warning("preferences.unknown_channel", value=c)
    return out


def _check_frequency_caps(
    *, user_pref: UserPreferences, is_high: bool
):
    """
    Resolve effective per-window caps and consult the Redis rate limiter.

    Effective caps = the user's `frequency_caps` overlaid on the global
    defaults (`default_frequency_cap_per_*`). Per-user value of `0`/`None`
    DISABLES that window for the user (explicit opt-out).

    HIGH-priority bypass is honored when
    `frequency_cap_high_priority_bypass=True` (default).
    """
    settings = get_settings()
    if is_high and settings.frequency_cap_high_priority_bypass:
        from app.core.rate_limiter import CapDecision  # local import: cheap, no cycle
        return CapDecision(allowed=True)

    user_caps = user_pref.frequency_caps or {}
    effective: dict[str, int | None] = {
        "per_hour": user_caps.get("per_hour", settings.default_frequency_cap_per_hour),
        "per_day": user_caps.get("per_day", settings.default_frequency_cap_per_day),
    }
    return check_and_consume(user_id=user_pref.user_id, caps=effective)


def _is_in_quiet_hours(prefs: UserPreferences, now_utc: datetime) -> bool:
    """
    True iff `now_utc`, expressed in the user's quiet-hours timezone, falls
    inside [quiet_hours_start, quiet_hours_end). Handles the wrap-around
    case where end < start (e.g. 22:00 → 07:00).
    """
    if (
        prefs.quiet_hours_start is None
        or prefs.quiet_hours_end is None
        or prefs.quiet_hours_timezone is None
    ):
        return False

    tz = ZoneInfo(prefs.quiet_hours_timezone)
    local_now: time = now_utc.astimezone(tz).time()
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end

    if start == end:
        # Convention: equal times = "always quiet" (24h). Documented choice.
        return True
    if start < end:
        return start <= local_now < end
    # Wrap across midnight.
    return local_now >= start or local_now < end
