# Design Decisions

## 1. Channel Abstraction Design

**Approach chosen:** A `ChannelProvider` Abstract Base Class (`app/channels/base.py`) with two required methods — `validate_address(address)` and `send(payload) -> SendOutcome` — plus a class-level `channel_type` attribute. Concrete providers self-register via the `@register_provider` decorator (`app/channels/registry.py`); the dispatcher resolves them via `get_provider(channel)`.

**Why:**
- ABC over Protocol → runtime enforcement (`TypeError` on missing methods); a `__init_subclass__` hook also fails fast if `channel_type` is omitted.
- A single `SendPayload` (with optional `subject`/`html_body`/`title`/`data`) avoids per-channel signatures while still serving channel-specific fields.
- Failure classification is the provider's responsibility (`RetryableProviderError` vs `NonRetryableProviderError`) — only the provider knows what an upstream's specific error code means. The dispatcher stays generic.
- Registry pattern decouples the dispatcher from concrete classes — zero dispatcher changes when adding a channel.

**Adding a new channel requires:**
1. Subclass `ChannelProvider`, set `channel_type`, implement `validate_address` and `send`.
2. Decorate the subclass with `@register_provider`.
3. Add the channel name to the worker's `-Q` list in `docker-compose.yml`.
4. (Optional) add channel-specific config fields to `Settings`.

**Note on the request-side template reference:** PRD §1.1 / §2.3 mention a request shape of "content OR template reference with variables". The implementation deliberately does **not** expose a `template_id` field on `POST /notifications`. Instead, the dispatcher resolves the active `Template` by `(notification_type, channel)` lazily at delivery time. Rationale: a single notification may fan out to multiple channels, each needing its own template (PRD §2.2: "different template per channel for same notification type") — a single request-level `template_id` cannot express that. The richer, channel-aware lookup is performed in `app/services/dispatcher.py::_load_active_template`. Callers control templating purely through `notification_type` + per-channel `Template` rows.

**Note on extended status enumerations:** The PRD §2 lists `NotificationStatus` as `{received, processing, completed, failed}` and `DeliveryStatus` as `{queued, sending, delivered, failed}`. The implementation extends both enums with two additional states required by other PRD sections: `NotificationStatus.CANCELLED` (required by §3.5 — cancel scheduled notification) and `DeliveryStatus.PERMANENTLY_FAILED` + `DeliveryStatus.CANCELLED` (required by §4.1 — lifecycle must distinguish exhausted retries from transient failures). These extensions are additive and backwards-compatible; the PRD §2 states are a subset.

---

## 2. Retry Strategy

**Approach chosen:** Two-axis exception model + Celery autoretry with exponential backoff + jitter, with a hard PERMANENTLY_FAILED transition once retries are exhausted.

**Retryable vs non-retryable classification** (raised by the provider — only it knows what an upstream's specific error code means):

| Channel | Retryable (`RetryableProviderError`) | Non-retryable (`NonRetryableProviderError`) |
|---|---|---|
| Email   | Simulated transient bounce (5xx-equivalent), rate-limit | Hard bounce, malformed address |
| SMS     | Carrier 5xx, network timeout | Carrier-rejected, malformed E.164 number, empty body |
| Push    | FCM/APNs 5xx, transient timeout | Invalid/expired device token, missing title, empty body |
| Webhook | HTTP 408, 425, 429, 500, 502, 503, 504; network/timeout/DNS errors | Other 4xx, non-http(s) URL, URL > 2048 chars |

**Retry timing:** **exponential backoff with jitter**, capped.
- Implemented via Celery `retry_backoff=True` + `retry_backoff_max=600` + `retry_jitter=True` on the `_DeliveryTask` base in [app/tasks/deliver.py](app/tasks/deliver.py).
- Wait sequence (base from `RETRY_BACKOFF_BASE_SECONDS`, default 2): ~2s → ~4s → ~8s → ~16s → ~32s … capped at 600s. Jitter randomizes within ±50% to avoid thundering-herd retries when many tasks fail simultaneously.
- _Why exponential + jitter (not fixed delay)?_ Transient outages tend to be correlated across many tasks; a fixed delay would synchronize retries and re-melt whatever just recovered. Exponential growth gives the upstream room to breathe; jitter de-synchronizes.

**Max retry attempts per channel:** uniformly `MAX_RETRY_ATTEMPTS` (default **5**, configurable per env). Same cap for every channel because the dominant variable (transient-failure rate) is upstream-specific but the cost of a mis-tuned per-channel cap is hard to reason about under load. A single global cap is operationally simpler; per-channel overrides can be added by widening the Settings shape in a future sprint.

**When `PERMANENTLY_FAILED` is set:**
1. **Immediately** when the provider raises `NonRetryableProviderError` (Celery propagates → `_DeliveryTask.on_failure` → `mark_delivery_permanently_failed`). Saves N pointless retries against an address that is structurally invalid.
2. **After `max_retries`** of `RetryableProviderError` (Celery exhausts autoretry → same `on_failure` path).

The `on_failure` hook ALSO recomputes the parent `Notification.status` so a single failed channel does not leave the notification stuck in PROCESSING when its siblings have all settled.

**Transactional layout** (the production-grade bit): the dispatcher splits a single attempt into THREE independent transactions —
  - **TX1 (claim):** `QUEUED/FAILED → SENDING`, `attempts++`, `last_attempt_at=now`. COMMITS so the SENDING state is durable even if the worker crashes mid-attempt.
  - **Provider call OUTSIDE any TX** so long network IO never holds a Postgres connection.
  - **TX2 (persist outcome):** either DELIVERED + `delivered_at` + `provider_response`, or FAILED + `error_message`, then re-raise so Celery handles retry / `on_failure`.

After every TX2 the parent Notification's status is recomputed (PROCESSING / COMPLETED / FAILED) so the aggregate state is monotonic per attempt.

---

## 3. Preference Resolution Order

**Approach chosen:** A pure function `resolve_channels_for_notification` in [app/services/preference_resolver.py](app/services/preference_resolver.py) computes the channel list. Side-effect free: the caller (`app/services/notifications.py`) decides what to do with the result (create Deliveries, mark filtered, etc.).

**Resolution pipeline (deterministic, top-down):**

1. **`channels_override`** (PRD §4.7) — if present, BYPASSES the entire pipeline. Rationale: override is an admin/security escape hatch (password reset, fraud alert) that must succeed even if the user has globally disabled that channel.
2. **Missing prefs / paused user** (PRD §3.5) — return empty channel list; notification is marked `COMPLETED` with `filtered_by_pause=true` in the structured log. Not a 4xx — the API call itself was valid.
3. **Global `enabled_channels`** — start from this allow-list. An empty list means "user has not opted in to any channel" → no deliveries.
4. **`per_type_preferences[notification_type]` (intersection only)** — if set, narrow the candidate list to `enabled_channels ∩ per_type`. Per-type preferences narrow; they NEVER widen. This prevents an old per-type entry from re-enabling a globally disabled channel.
5. **Quiet hours** (PRD §4.3) — if "now" (in `quiet_hours_timezone`) falls inside `[start, end)`, drop ALL channels UNLESS the notification has `priority=HIGH`. Wrap-around (e.g. 22:00→07:00) is handled correctly: when `start > end`, the window spans midnight, and a time is inside if it is `>= start` OR `< end`. **Edge case: `start == end`** is treated as "always quiet" (a 24-hour block), not as "no quiet hours". Callers who want to disable quiet hours should either omit the fields entirely or set `quiet_hours_start=null`. Setting both to the same value is an intentional "block all" configuration.
6. **Frequency caps** (PRD §4.6) — fixed-window counters in Redis (`freqcap:{user_id}:{window}:{bucket_id}`, TTL = window length). Caps are evaluated AFTER quiet hours so the simpler check happens first. **HIGH-priority bypass is OFF by default** (`FREQUENCY_CAP_HIGH_PRIORITY_BYPASS=false`): combined with the unconditional quiet-hours bypass, an unconstrained HIGH bypass would turn any caller able to set `priority=high` into an unbounded firehose. Operators who control who can mint HIGH can flip the flag to `true` to opt back into the bypass (mirroring the quiet-hours bypass). Cap state is **fail-CLOSED by default** (`RATE_LIMITER_FAIL_OPEN=false`): a Redis outage drops the send and emits a `freqcap.redis_unavailable` warning, because lifting caps for every user simultaneously during an outage is catastrophic on an alerting path. Operators on a marketing-only deployment can opt into fail-open. Trade-off: fixed-window allows up to `2*N` deliveries across a `2*window` boundary; documented and accepted as the price of O(1) checks. Capped notifications are **dropped** (not queued) — queueing them would defeat the purpose of the cap (smoothing user-facing volume) and queueing-with-deferral overlaps with §4.8 scheduling.
7. **Webhook URL gate** — `WEBHOOK` is dropped (with a `preferences.webhook_dropped_no_url` log) if the user has no `webhook_url` configured, since shipping a webhook delivery without a destination is unrecoverable.

**Conflicts resolved by:** strict precedence — override > paused > enabled > per-type narrowing > quiet hours > address availability. There is no merging; each step either passes the candidate list through or replaces it with `[]`.

**Worked example:**
- User prefs: `enabled_channels=[email, sms]`, `per_type_preferences={"marketing": [email]}`, `quiet_hours=22:00–07:00 Asia/Jerusalem`, `is_paused=False`.
- Request: `notification_type=marketing`, `priority=NORMAL`, sent at 04:00 local time.
- Step 1: no override → continue.
- Step 2: not paused → continue.
- Step 3: candidates = `[email, sms]`.
- Step 4: per-type narrows to `[email]`.
- Step 5: 04:00 is inside quiet hours, priority is NORMAL (not HIGH) → drop everything.
- Result: `channels=[]`, notification marked `COMPLETED` with `filtered_by_quiet_hours=true`.
- Same request with `priority=HIGH`: step 5 bypassed → `channels=[email]` → email delivery enqueued.

---

## 4. Multi-Channel Coordination

**Approach chosen:** Each resolved channel becomes an INDEPENDENT `Delivery` row with its own Celery task on a dedicated per-channel queue (`email`, `sms`, `push`, `webhook`). All channels for a single notification execute IN PARALLEL — there is no sequencing between them.

**Why parallel (over sequential):**
- Latency: a slow webhook acknowledgement should never delay an SMS that the user is waiting on.
- Backpressure isolation: a queue backlog on `webhook` (e.g. a flapping customer endpoint) does not block `email` workers — Celery prefetch is 1 and queues are independent.
- Operational scaling: each queue's worker count can be scaled independently in `docker-compose.yml` (`-Q email,sms` for one fleet, `-Q webhook` for another).

**Top-level `Notification.status` aggregation rule** (`_recompute_notification_status` in [app/services/dispatcher.py](app/services/dispatcher.py)):
- Any non-terminal Delivery (QUEUED / SENDING / FAILED-but-retrying) → `PROCESSING`.
- All Deliveries terminal AND ≥1 `DELIVERED` → `COMPLETED`.
- All Deliveries terminal AND zero `DELIVERED` → `FAILED`.

**Failure isolation (PRD §4.4):** because each channel is its own task with its own retry counter, a permanent SMS bounce does not impact the email retry loop. The notification reaches `COMPLETED` as soon as ANY channel succeeds; remaining failed channels surface in their per-Delivery `error_message` for observability without polluting the top-level status.

**Concurrency safety:** the dispatcher's 3-transaction layout (claim → external call → persist outcome) means two workers attempting the same delivery would both flip to `SENDING` and increment `attempts` — but Celery's `acks_late=True` + per-queue prefetch=1 + the unique task ID on the broker prevent that fan-out at the source. A future hardening would add `SELECT … FOR UPDATE SKIP LOCKED` on the claim TX to make this safe even if a duplicate task ever lands.

---

## 5. One Thing I Would Do Differently With More Time

**Integration tests against real Postgres + Redis + Celery, instead of unit tests with monkey-patched seams.**

The current 6 mandatory tests (PRD §7.5) all run with zero infrastructure: FastAPI `dependency_overrides` swap the DB session for a `SimpleNamespace`, Celery task `delay`/`apply_async` are monkey-patched, the rate-limiter `check_and_consume` is patched to allow-all. This is fast (`pytest -q` finishes under 2 s) and CI-friendly, but it leaves real risk uncovered:

1. **The 3-TX dispatcher** (claim → external call → persist outcome) is the single most failure-sensitive piece of code in the system, yet none of its locking semantics (`SELECT … FOR UPDATE SKIP LOCKED` on the scheduler claim, `acks_late=True` on the worker, the at-least-once vs at-most-once trade-off) is exercised under contention. A second worker racing the same delivery would only be caught by a real broker.
2. **Alembic migrations** are never executed in tests. A column-rename migration that breaks the ORM mapping would only surface on `docker compose up`.
3. **The Postgres-specific column types** (`JSONB`, `UUID(as_uuid=True)`, partial unique index on Templates) have no SQLite equivalent, so I deliberately avoided an in-memory ORM test layer rather than ship a fragile dual-dialect type adapter.
4. **The rate limiter's fail-open semantics** are documented (DECISIONS.md §3 step 6) but never observed — a Redis-down test against a real Redis container would prove the dispatch path stays alive.

**What I would build, given another day:**

- A `tests/integration/` suite using `pytest-docker` (or `testcontainers-python`) to spin up the real `db`, `redis`, and a one-shot Celery worker; each test runs `alembic upgrade head` against an ephemeral Postgres schema and tears it down.
- A focused chaos test that flips `email_transient_failure_rate=1.0` for N attempts, then to `0.0`, and asserts the Delivery row reaches `DELIVERED` after exactly N+1 attempts with the correct backoff intervals — proving the retry loop end-to-end against real Celery.
- A concurrency test that fires two `dispatch_due_notifications` ticks in parallel against the same backlog and asserts no Delivery is ever enqueued twice.

**Why I shipped without this:** time-boxed exam scope. The 6 unit tests cover the *contracts* the production code exposes — provider classification, resolver branching, queue routing, template rendering, status aggregation, and the request-acceptance shape — which is what an interviewer needs to see in a code-review pass. Full integration coverage is a "next iteration" expense, not a "minimum viable submission" one, and being explicit about the gap (per PRD §7.6: "Be explicit about simplifications — honesty is valued") is itself part of the deliverable.

Other smaller items I'd revisit, in priority order:
- **Per-channel address columns on `UserPreferences`** (email / phone / device_token) instead of the current Sprint-3 simplification of using `request.recipient_contact` for non-webhook channels.
- **Sliding-window rate limiter** (Redis sorted-sets) to eliminate the 2N-overshoot at fixed-window boundaries — currently documented in §3 step 6 as an accepted trade-off.
- **Real OpenAPI examples + Postman collection** committed to the repo so reviewers can `curl` the system without reading the README.

