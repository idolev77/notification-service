"""
Frequency-cap rate limiter (PRD §4.6).

Approach: **fixed-window counters in Redis**, mutated through a single
atomic Lua script.

  Key:   "freqcap:{user_id}:{window}:{bucket_id}"
  Value: integer count of notifications charged in this window
  TTL:   window length (auto-expires; no GC needed)

  bucket_id derivation:
    - per_hour → epoch_seconds // 3600
    - per_day  → epoch_seconds // 86_400  (UTC; documented choice)

WHY a Lua script:
  - GET-then-INCR from the client is racy: two workers can both read
    `count == limit-1`, both decide "under cap", both INCR -> overshoot
    by one (or many) per concurrent caller. Eliminating the gap requires
    a server-side atomic compare-and-increment, which Redis exposes via
    `EVAL`. The script below is one round-trip and the entire body runs
    under Redis's single-threaded execution loop, so it cannot interleave
    with any other command.

WHY fixed-window over sliding-window:
  - O(1) per check vs O(n) for sorted-set sliding windows.
  - Redis-side TTL means the counter cannot leak past the window — no
    sweeper / cron needed.
  - Trade-off: a burst spanning the window boundary can deliver up to
    2*N in a 2*window span. Documented in DECISIONS.md §3 step 6.

WHY Redis (vs Postgres):
  - Atomic INCR + per-key TTL out of the box.
  - Already provisioned (broker + result backend) → zero new infra.
  - Failure mode: if Redis is down, we FAIL OPEN (allow the send) and
    log a warning — caps are best-effort, the dispatch path must not
    become unavailable because of an auxiliary counter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

import redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.logging import get_logger

_logger = get_logger(__name__)

# Window length in seconds, keyed by the cap-name used in UserPreferences.
_WINDOW_SECONDS: dict[str, int] = {
    "per_hour": 3_600,
    "per_day": 86_400,
}

# ---------------------------------------------------------------------------
# Lua script — atomic "check N caps, increment all on success".
#
# Contract:
#   KEYS    = the Redis keys for the active windows, in caller-supplied order
#   ARGV    = [limit_1, ttl_1, limit_2, ttl_2, ...] paired 1:1 with KEYS
#
# Returns:
#   - On allow:  a flat list of the *new* counts (post-increment), one per key.
#                e.g. {3, 17}
#   - On deny:   a 2-element list { -1, tripped_index }  (1-based index into
#                KEYS) so the Python side can report which window tripped
#                without a second round trip.
#
# The script first checks every cap (read-only), then — only if all pass —
# increments and (re)sets the TTL on each key. This guarantees we never
# half-charge a user when one of N windows trips.
# ---------------------------------------------------------------------------
_LUA_CHECK_AND_CONSUME = """
local n = #KEYS
-- Phase 1: read all current counts and verify every cap.
for i = 1, n do
    local limit = tonumber(ARGV[(i-1)*2 + 1])
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    if current >= limit then
        return {-1, i}
    end
end
-- Phase 2: every cap passed; charge one slot in each window.
local out = {}
for i = 1, n do
    local ttl = tonumber(ARGV[(i-1)*2 + 2])
    local newv = redis.call('INCR', KEYS[i])
    -- Only set TTL when the bucket is brand new (newv == 1) to avoid
    -- repeatedly extending it across the window boundary.
    if newv == 1 then
        redis.call('EXPIRE', KEYS[i], ttl)
    end
    out[i] = newv
end
return out
"""


@dataclass(frozen=True, slots=True)
class CapDecision:
    """Outcome of a frequency-cap check."""

    allowed: bool
    # If denied, which window tripped (e.g. "per_hour"). None when allowed.
    tripped_window: str | None = None
    # Counts at decision time (purely informational; for logs).
    counts: dict[str, int] | None = None


@lru_cache(maxsize=1)
def _client() -> redis.Redis:
    """Process-wide Redis client for the cap counters."""
    settings = get_settings()
    # decode_responses=True so INCR returns Python ints transparently.
    return redis.Redis.from_url(settings.rate_limiter_redis_url, decode_responses=True)


@lru_cache(maxsize=1)
def _registered_script() -> "redis.commands.core.Script":
    """
    Pre-register the Lua script with Redis so subsequent calls go through
    EVALSHA (single-byte command) rather than re-shipping the source.
    """
    return _client().register_script(_LUA_CHECK_AND_CONSUME)


def check_and_consume(
    *,
    user_id: str,
    caps: dict[str, int | None],
) -> CapDecision:
    """
    Atomically check all configured caps and, if every one is below limit,
    consume one slot in each window. Returns the decision.

    Atomicity guarantee:
      The check and the increment happen inside a single Lua script
      executed by Redis, so no other client can squeeze in between. This
      eliminates the overshoot race that a GET-then-INCR pattern has.

    Failure modes:
      - Redis unreachable          -> FAIL OPEN, log error.
      - Lua script error           -> FAIL OPEN, log error (defensive; the
                                      script itself has no failure paths).
    """
    active_windows = _active_windows(caps)
    if not active_windows:
        return CapDecision(allowed=True)

    now = int(time.time())
    # Stable ordering so the script's index-back-to-window mapping is correct.
    ordered = sorted(active_windows.items())  # [("per_day", 100), ("per_hour", 10)]
    keys = [_key_for(user_id, window, now) for window, _ in ordered]
    args: list[int] = []
    for window, limit in ordered:
        # Pair shape required by the Lua script: (limit_i, ttl_i) per key.
        args.extend([limit, _WINDOW_SECONDS[window]])

    try:
        script = _registered_script()
        result = script(keys=keys, args=args, client=_client())
    except RedisError as exc:
        # Behaviour controlled by `rate_limiter_fail_open`. Default = False
        # (fail-closed) so a Redis outage cannot lift caps for every user
        # simultaneously \u2014 which would be catastrophic for an alerting
        # path. Operators on a marketing-only deployment can flip this on.
        fail_open = get_settings().rate_limiter_fail_open
        _logger.error(
            "freqcap.redis_unavailable",
            error=str(exc),
            user_id=user_id,
            fail_open=fail_open,
        )
        return CapDecision(
            allowed=fail_open,
            tripped_window=None if fail_open else "redis_unavailable",
        )

    # Result is either {-1, tripped_idx} (deny) or [new_count_1, ...] (allow).
    if isinstance(result, list) and len(result) >= 1 and int(result[0]) == -1:
        tripped_idx = int(result[1]) - 1  # Lua is 1-based
        tripped_window, tripped_limit = ordered[tripped_idx]
        _logger.info(
            "freqcap.tripped",
            user_id=user_id,
            window=tripped_window,
            limit=tripped_limit,
        )
        return CapDecision(
            allowed=False,
            tripped_window=tripped_window,
            counts=None,  # we did not pay for the read of every key on deny
        )

    counts = {window: int(count) for (window, _), count in zip(ordered, result)}
    return CapDecision(allowed=True, counts=counts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _active_windows(caps: dict[str, int | None]) -> dict[str, int]:
    """Filter `caps` to {window: positive_int} entries we should enforce."""
    out: dict[str, int] = {}
    for window, limit in (caps or {}).items():
        if window not in _WINDOW_SECONDS:
            # Unknown window — skip (forward-compat with future config).
            continue
        if not limit or limit <= 0:
            continue
        out[window] = limit
    return out


def _key_for(user_id: str, window: str, now_epoch_seconds: int) -> str:
    """Build the Redis key for the (user, window, current bucket) triple."""
    bucket = now_epoch_seconds // _WINDOW_SECONDS[window]
    return f"freqcap:{user_id}:{window}:{bucket}"
