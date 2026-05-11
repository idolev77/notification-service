# Multi-Channel Notification Service — Architectural Summary & Interview Prep

> **Role:** Lead Software Architect  
> **Purpose:** Primary preparation material for the oral technical interview.

---

## Table of Contents

1. [Executive Architectural Overview](#1-executive-architectural-overview)
2. [The File System Map](#2-the-file-system-map)
3. [Deep Dive into Key Components](#3-deep-dive-into-key-components)
4. [Critical Interview Prep Scenarios](#4-critical-interview-prep-scenarios)
5. [Summary of Deliverables](#5-summary-of-deliverables)

---

## 1. Executive Architectural Overview

### High-Level Request Lifecycle

```
HTTP POST /notifications
        │
        ▼
┌─────────────────────────────┐
│ FastAPI (app/api/notifications.py)
│ • Validate Pydantic schema
│ • Persist Notification (status=RECEIVED)
│ • Return 202 Accepted        │  ← decoupled response
└──────────────┬───────────────┘
               │
               ▼
┌─────────────────────────────┐
│ services/notifications.py    │
│ • Load UserPreferences      │
│ • Call preference_resolver  │  ← override → paused → enabled →
│   (returns ChannelType[])   │     per-type → quiet hours →
│ • Render template per chan. │     freq cap → webhook URL gate
│ • Create Delivery rows      │
│   (status=QUEUED)           │
│ • Enqueue Celery task per   │
│   channel (priority queue   │
│   if HIGH)                  │
└──────────────┬───────────────┘
               │  (Redis broker)
               ▼
┌─────────────────────────────┐
│ Celery worker (per channel) │
│ tasks/deliver.py            │
│ ├─ TX1: claim → SENDING     │  (durable across crashes)
│ ├─ provider.send() OUTSIDE  │  (no DB conn held)
│ │   any DB tx               │
│ └─ TX2: persist outcome     │
│         + recompute parent  │
└──────────────┬───────────────┘
               │
               ▼
   DELIVERED ─┬─ Notification.status
              │   ├─ any non-terminal → PROCESSING
   FAILED →   │   ├─ ≥1 DELIVERED      → COMPLETED
   retry      │   └─ 0 DELIVERED       → FAILED
   (exp+jitter)
              ▼
   PERMANENTLY_FAILED (after max_retries OR NonRetryable)
```

### Spec-Driven Development

- The spec (`mid-senior-notification-service.md`) was decomposed into a granular checklist (`PRD.md`) cross-referenced with mandatory items (`§4.1` state machine, `§4.2` resolution order, `§5` channels, `§7.5` mandatory tests).
- Each sprint pulled directly from PRD line items; commits map back to PRD sections.
- The 6 mandatory test files in `tests/` align 1:1 with PRD §7.5.

### Production Readiness

- **State durability:** 3-transaction dispatcher ensures `SENDING` is committed before any provider IO.
- **At-least-once semantics:** Celery `task_acks_late=True` + `task_reject_on_worker_lost=True`.
- **Failure isolation:** per-channel queues, per-channel Delivery rows, independent retry counters.
- **Containerized:** 5-service `docker-compose` (db/redis/api/worker/beat), Postgres healthchecks gating dependents, non-root user in `Dockerfile`, Alembic migrations on boot.
- **Observability:** structured logging with bound notification context (`bind_notification_context`).
- **Backpressure & priority:** worker `-Q priority,email,sms,push,webhook,scheduler,default` with prefetch=1.

---

## 2. The File System Map

### Root Files

| File | Responsibility |
|------|----------------|
| `README.md` | Run instructions, test instructions, example `curl`s, channel matrix. |
| `DECISIONS.md` | Per PRD §7.3: channel abstraction, retry strategy, preference resolution, multi-channel coordination, "what I'd do differently". |
| `AI_USAGE.md` | Per PRD §7.4: tools used, what helped, what I had to fix, what AI struggled with. |
| `PRD.md` | Internal zero-loss spec checklist with sprint plan. |
| `mid-senior-notification-service.md` | Original take-home assignment. |
| `requirements.txt` | Pinned Python deps (FastAPI, SQLAlchemy, Celery, Redis, Alembic, structlog). |
| `Dockerfile` | Python 3.11-slim, non-root user, single image used by api/worker/beat. |
| `docker-compose.yml` | 5-service stack with healthchecks; documents queue order significance. |
| `alembic.ini` | Alembic config pointing at `alembic/` migration tree. |

### Migrations — `alembic/`

| File | Responsibility |
|------|----------------|
| `alembic/env.py` | Wires Alembic to the SQLAlchemy `Base.metadata`; uses async-safe sync engine. |
| `alembic/script.py.mako` | Template Alembic uses to generate new revision files. |
| `alembic/versions/0001_initial_schema.py` | Creates `notifications`, `deliveries`, `user_preferences`, `templates` with JSONB, UUIDs, partial unique index on (notification_type, channel) where `is_active`. |
| `alembic/versions/0002_per_channel_addresses.py` | Adds per-channel address columns (email/phone/device_token) on `user_preferences`. |

### Application Bootstrap — `app/`

| File | Responsibility |
|------|----------------|
| `app/main.py` | FastAPI app factory; mounts routers; registers `/healthz`; configures structured logging. |
| `app/worker.py` | Celery app definition: per-channel `Queue`s, JSON serialization, `acks_late`, `prefetch=1`, Beat schedule for scheduled-notifications scan. |

### HTTP Surface — `app/api/`

| File | Responsibility |
|------|----------------|
| `app/api/notifications.py` | `POST /notifications` (accept + enqueue), `POST /notifications/{id}/cancel`. |
| `app/api/preferences.py` | `GET/PUT/DELETE /users/{user_id}/preferences`. |
| `app/api/templates.py` | `POST/GET/DELETE /templates` with per-channel uniqueness. |
| `app/api/tracking.py` | `GET /notifications/{id}`, `/deliveries`, `/users/{id}/notifications`, `/stats/deliveries`. |
| `app/api/management.py` | `POST /notifications/{id}/resend` — re-enqueues failed deliveries. |

### Channel Abstraction — `app/channels/`

| File | Responsibility |
|------|----------------|
| `app/channels/base.py` | `ChannelProvider` ABC, `SendPayload`, `SendOutcome`, `RetryableProviderError`/`NonRetryableProviderError`. Defines the entire provider contract. |
| `app/channels/registry.py` | `@register_provider` decorator + `get_provider(channel)` resolver; decouples dispatcher from concrete classes. |
| `app/channels/email.py` | Mock email provider; RFC-5321 validation; tracks sent/delivered/opened/bounced; tunable failure rates. |
| `app/channels/sms.py` | Mock SMS; E.164 validation; carrier-style retryable vs rejected classification. |
| `app/channels/push.py` | Mock push; device-token validation; FCM/APNs-style 5xx vs invalid-token classification. |
| `app/channels/webhook.py` | Mock webhook POST; HTTP 408/425/429/5xx → retryable; other 4xx → non-retryable. |

### Cross-Cutting Core — `app/core/`

| File | Responsibility |
|------|----------------|
| `app/core/config.py` | Pydantic `Settings`: DB URL, Redis URLs (DBs 0/1/2), retry caps, frequency-cap defaults, failure-rate knobs. |
| `app/core/db.py` | SQLAlchemy engine + `session_scope()` context manager (commit/rollback). |
| `app/core/logging.py` | `structlog` configuration + `bind_notification_context`/`clear_notification_context` for per-task contextual logs. |
| `app/core/rate_limiter.py` | Redis fixed-window frequency cap: two-phase MGET → enforce → INCR+EXPIRE pipeline; **fail-open** on Redis errors. |

### Domain Models — `app/models/`

| File | Responsibility |
|------|----------------|
| `app/models/base.py` | Declarative `Base`, UUID PK mixin, timestamp mixin. |
| `app/models/enums.py` | `ChannelType`, `NotificationPriority`, `NotificationStatus`, `DeliveryStatus`. |
| `app/models/notification.py` | `Notification` ORM model with state machine column. |
| `app/models/delivery.py` | `Delivery` (FK→Notification), per-channel state, `attempts`, `provider_response` JSONB. |
| `app/models/template.py` | `Template`: per-(type, channel) row with partial unique index. |
| `app/models/user_preferences.py` | `UserPreferences` with JSONB columns and quiet-hours fields. |

### Pydantic Schemas — `app/schemas/`

| File | Responsibility |
|------|----------------|
| `app/schemas/notifications.py` | Request/response shapes for `/notifications`. |
| `app/schemas/preferences.py` | Validates `quiet_hours_*`, frequency caps, channel enums. |
| `app/schemas/templates.py` | Template CRUD shapes. |
| `app/schemas/tracking.py` | Tracking/stats response models. |

### Business Services — `app/services/`

| File | Responsibility |
|------|----------------|
| `app/services/notifications.py` | Orchestrator: persists Notification, calls resolver, creates Deliveries, enqueues per-channel tasks (priority routing for HIGH). |
| `app/services/dispatcher.py` | The 3-TX delivery executor (claim → external call → persist outcome → recompute parent). |
| `app/services/preference_resolver.py` | Pure-function resolution pipeline (override → paused → enabled → per-type → quiet hours → cap → webhook URL). |
| `app/services/templates.py` | Jinja2 rendering with `TemplateRenderError`; `render_template` and `render_html`. |

### Async Workers — `app/tasks/`

| File | Responsibility |
|------|----------------|
| `app/tasks/deliver.py` | Per-channel Celery tasks (`deliver_email/sms/push/webhook`) + `_DeliveryTask` base with autoretry + on_failure → PERMANENTLY_FAILED. |
| `app/tasks/scheduler.py` | Beat-driven `dispatch_due_notifications`: scans Notifications with `scheduled_at <= now` and enqueues them. |

### Tests — `tests/`

| File | PRD §7.5 Mapping |
|------|------------------|
| `tests/conftest.py` | Shared fixtures: `dependency_overrides`, monkey-patched `delay`/`apply_async`, allow-all rate limiter stub. |
| `tests/test_send_notification_endpoint.py` | End-to-end accept path |
| `tests/test_preference_filtering.py` | Resolver branches |
| `tests/test_retry_on_failure.py` | Retryable vs non-retryable transitions |
| `tests/test_template_rendering.py` | Jinja substitution + missing-var handling |
| `tests/test_multi_channel_delivery.py` | Parallel deliveries + status aggregation |
| `tests/test_status_tracking.py` | Tracking endpoints |

---

## 3. Deep Dive into Key Components

### 3.1 Channel Abstraction

The pattern is an **Abstract Base Class + self-registration registry**.

**`ChannelProvider` ABC** (`app/channels/base.py`) defines a minimal contract:
- `channel_type: ChannelType` — class attribute validated by `__init_subclass__`, fails fast if forgotten.
- `validate_address(address)` — pre-flight rejection, raises `NonRetryableProviderError` on bad input.
- `send(payload: SendPayload) -> SendOutcome` — performs the delivery attempt.

**`SendPayload`** carries channel-superset fields (`subject`, `html_body`, `title`, `data`) so the dispatcher **never branches on channel type**. Each provider reads only the fields it needs.

**Failure classification lives in the provider** — only the provider knows what HTTP 422 means for its specific upstream. The dispatcher stays channel-agnostic.

```
RetryableProviderError    → Celery autoretry with backoff
NonRetryableProviderError → on_failure hook → PERMANENTLY_FAILED immediately
```

**Self-registration registry** (`app/channels/registry.py`):
- `@register_provider` decorator on any `ChannelProvider` subclass auto-registers it.
- `get_provider(channel)` resolves at dispatch time.
- **Zero dispatcher changes when adding a channel.**

#### Adding a New Channel — 4 Steps

1. Subclass `ChannelProvider`, set `channel_type`, implement `validate_address` and `send`.
2. Decorate with `@register_provider`.
3. Add a `Queue` in `app/worker.py` and a `task_routes` entry.
4. Add the queue name to `-Q` list in `docker-compose.yml`.

---

### 3.2 Preference Resolution Engine

Implemented as a **side-effect-free pure function** `resolve_channels_for_notification` in `app/services/preference_resolver.py`. Strict precedence — no merging, each step either passes the candidate list through or replaces it with `[]`.

#### Resolution Pipeline

| Step | Rule | Effect |
|------|------|--------|
| **1** | `channels_override` present | **Bypass entire pipeline.** Use override verbatim (admin escape hatch for password reset / fraud alert). |
| **2** | Missing prefs OR `is_paused=True` | Return `[]`, log `filtered_by_pause`. Not a 4xx — the API call was valid. |
| **3** | Global `enabled_channels` | Start candidate list (the allow-list). |
| **4** | `per_type_preferences[notification_type]` | **Intersect** with enabled — per-type narrows, never widens. Prevents old per-type entries from re-enabling globally disabled channels. |
| **5** | Quiet hours window | Drop ALL channels **unless** `priority=HIGH`. Wrap-around (22:00→07:00) handled. `start == end` = "always quiet". |
| **6** | Frequency caps (Redis) | Drop ALL **unless** HIGH and `frequency_cap_high_priority_bypass=true`. **Fail-open** on Redis outage. |
| **7** | Webhook URL gate | Drop `WEBHOOK` if `user_pref.webhook_url is None`. |

#### Worked Example

- **User prefs:** `enabled_channels=[email, sms]`, `per_type_preferences={"marketing": [email]}`, `quiet_hours=22:00–07:00 Asia/Jerusalem`, `is_paused=False`.
- **Request:** `notification_type=marketing`, `priority=NORMAL`, sent at 04:00 local time.

| Step | Result |
|------|--------|
| 1 — no override | continue |
| 2 — not paused | continue |
| 3 — enabled | candidates = `[email, sms]` |
| 4 — per-type narrows | candidates = `[email]` |
| 5 — quiet hours, NORMAL priority | **drop everything** → `[]` |
| **Result** | `channels=[]`, `filtered_by_quiet_hours=true` |

Same request with `priority=HIGH` → step 5 bypassed → `channels=[email]` → email delivery enqueued.

---

### 3.3 Reliability & Retry Logic

#### Two-Axis Exception Model

```python
class _DeliveryTask(Task):
    autoretry_for       = (RetryableProviderError,)   # NonRetryable propagates → on_failure
    retry_backoff       = True                        # exponential
    retry_backoff_max   = 600                         # cap one wait at 10 min
    retry_jitter        = True                        # ±50% randomization
    max_retries         = settings.max_retry_attempts # default 5
    acks_late           = True
```

#### Retryable vs Non-Retryable Classification

| Channel | Retryable | Non-Retryable |
|---------|-----------|---------------|
| **Email** | Transient bounce (5xx-equiv), rate-limit | Hard bounce, malformed address |
| **SMS** | Carrier 5xx, network timeout | Carrier-rejected, malformed E.164, empty body |
| **Push** | FCM/APNs 5xx, transient timeout | Invalid/expired device token, missing title, empty body |
| **Webhook** | HTTP 408/425/429/500/502/503/504, network/DNS errors | Other 4xx, non-http(s) URL, URL > 2048 chars |

#### Backoff Sequence (base=2s)

```
Attempt 1: ~2s wait
Attempt 2: ~4s
Attempt 3: ~8s
Attempt 4: ~16s
Attempt 5: ~32s
(capped at 600s, ±50% jitter on each)
```

Jitter de-synchronizes retries so thousands of concurrent failures don't all hit the upstream simultaneously after recovery.

#### PERMANENTLY_FAILED — Two Paths

1. **Immediately** on `NonRetryableProviderError` — saves N pointless retries against a structurally invalid address.
2. **After `max_retries`** of `RetryableProviderError` — Celery exhausts autoretry → same `on_failure` path.

Both paths call `mark_delivery_permanently_failed` which **also recomputes the parent Notification status** so no delivery can leave the parent stuck in `PROCESSING`.

#### 3-Transaction Dispatcher Layout

```
TX1 (claim):
  Delivery: QUEUED/FAILED → SENDING
  attempts++, last_attempt_at=now
  COMMIT ← durable across crashes

  ← no DB connection held →

Provider call (outside any TX, long network IO)

TX2-success:
  status=DELIVERED, delivered_at=now, provider_response=...
  recompute Notification.status
  COMMIT

TX2-failure:
  status=FAILED, error_message=...
  recompute Notification.status
  COMMIT, then re-raise → Celery autoretry or on_failure
```

---

### 3.4 Async Strategy

| Component | Technology | Config |
|-----------|-----------|--------|
| Broker | Redis DB 0 | `celery_broker_url` |
| Result backend | Redis DB 1 | `celery_result_backend` |
| Cap counters | Redis DB 2 | isolated key namespace |
| Queues | `priority`, `email`, `sms`, `push`, `webhook`, `scheduler`, `default` | one per channel |
| Worker order | `-Q priority,email,sms,push,webhook,scheduler,default` | `priority` first |
| Prefetch | `worker_prefetch_multiplier=1` | no starvation of siblings |
| Serialization | JSON only | no pickle security issues |
| Semantics | `task_acks_late=True` + `task_reject_on_worker_lost=True` | at-least-once |

**Failure isolation:** a flapping webhook queue backlog cannot block email throughput — each channel has its own queue, its own worker pool slice, its own retry counter.

**Crash safety:** TX1 commits `SENDING` before provider IO. If a worker is killed mid-attempt, the task is requeued (acks_late), picks up from `SENDING` state, and retries correctly.

---

## 4. Critical Interview Prep Scenarios

### "What happens if the SMS provider is down but Email is up?"

Each channel is an **independent Delivery row on an independent Celery queue**. The execution is parallel:

- **Email task** on `email` queue: completes → `Delivery.status=DELIVERED`.
- **SMS task** on `sms` queue: raises `RetryableProviderError` → Celery retries with exponential backoff (~2s → 4s → 8s → 16s → 32s, ±jitter) up to `max_retries` → `on_failure` → `PERMANENTLY_FAILED`.

`_recompute_notification_status` runs after **every TX2**:
- As soon as email delivers, rule "≥1 DELIVERED + all terminal" → parent `Notification.status=COMPLETED`.
- The SMS error is preserved in `Delivery.error_message` for observability without polluting the parent status.
- **Failure of one channel never affects another** (PRD §4.4 — each has its own task, its own retry counter, its own queue).

---

### "How did you ensure High-Priority notifications bypass Quiet Hours?"

In `app/services/preference_resolver.py`, step 5:

```python
in_quiet = _is_in_quiet_hours(user_pref, now or datetime.now(tz=ZoneInfo("UTC")))
is_high  = notification.priority is NotificationPriority.HIGH
if in_quiet and not is_high:
    return ResolutionResult(channels=[], filtered_by_quiet_hours=True)
```

The `not is_high` short-circuit means **HIGH-priority skips the quiet-hours filter entirely**. The same flag bypasses frequency caps (step 6) when `frequency_cap_high_priority_bypass=true`.

Both bypasses express the same operational principle: **"this notification is critical enough to override user comfort settings"**.

On the Celery side, HIGH-priority deliveries are routed to the dedicated `priority` queue, which the worker drains **first** per the `-Q` ordering.

---

### "How would you scale this to 1 million notifications/hour?"

~278 notifications/sec sustained. Strategy in layers:

#### Layer 1 — API Tier
- Stateless FastAPI behind a load balancer; horizontal scale on RPS.
- The accept path is **O(1)** (insert + enqueue), no provider IO inline.

#### Layer 2 — Worker Tier
- Scale per-queue independently:
  ```bash
  docker-compose scale worker_email=20 worker_sms=10 worker_webhook=30
  ```
- Because queues are isolated, a webhook backlog doesn't force email scaling.

#### Layer 3 — Postgres
- Primary bottleneck is `Delivery` inserts.
- Mitigations: connection pooling (already via SQLAlchemy), partition `deliveries` by `created_at` month, move `provider_response` JSONB to cold-store after 30 days.

#### Layer 4 — Redis Broker
- Single-node Redis handles ~100k ops/sec.
- At 1M/hour with ~5 ops per notification: need **Redis Cluster** or partition by channel.

#### Layer 5 — Hot-Path Improvements
- Batch inserts via `COPY` for Delivery rows.
- Sliding-window sorted-set rate limiter (eliminates the 2N-overshoot at fixed-window boundaries).
- `SELECT ... FOR UPDATE SKIP LOCKED` on scheduler claim for multiple Beat replicas.
- Dead-letter queues per channel + circuit breakers per provider.

#### Layer 6 — Observability for Autoscaling
- Queue-depth metrics → autoscale trigger.
- Per-provider p95 latency → circuit-breaker thresholds.

---

### "Why did you choose this specific database/framework over others?"

#### PostgreSQL — Why Not MySQL or MongoDB?

| Requirement | Why Postgres wins |
|-------------|-------------------|
| `per_type_preferences`, `frequency_caps`, `provider_response` stored as JSONB and **queried into** | JSONB with GIN indexing. MySQL JSON lacks GIN; MongoDB loses relational FK integrity. |
| `Template (notification_type, channel) WHERE is_active` — partial unique index | Postgres native. MySQL has no partial index support. |
| Timezone-aware timestamps throughout | Postgres native `TIMESTAMPTZ`. |
| Simple, boring ops at this scale | Postgres is battle-tested and operationally predictable. |

#### FastAPI — Why Not Flask or Django?

| Feature | Impact |
|---------|--------|
| Async-native | Matches IO-bound fan-out endpoints. |
| Pydantic schema validation | Free at request boundary, reused as response models. |
| Auto-generated OpenAPI docs | Reviewers can explore the API at `/docs` without reading README. |
| `dependency_overrides` | Powers all 6 mandatory tests with **zero infrastructure** (no Postgres, no Redis). |
| Flask would require | Hand-rolled validation, no DI for testing. |
| Django would require | Heavy ORM + migration conventions I don't need here. |

#### Celery + Redis — Why Not RQ or a hand-rolled queue?

| Feature | Impact |
|---------|--------|
| Per-queue routing | Maps 1:1 to PRD §4.1 (one queue per channel). |
| `autoretry_for` + `retry_backoff` | Implements PRD §4.5 retry strategy without hand-rolled code. |
| `acks_late` + `reject_on_worker_lost` | At-least-once delivery guaranteed by the broker. |
| Beat scheduler | Implements PRD §4.8 scheduled notifications without a separate cron service. |
| RQ would require | Hand-rolled exponential backoff. |

#### SQLAlchemy 2.x + Alembic

- Typed ORM with declarative models that maps cleanly to PRD §2 entities.
- Alembic gives versioned schema executed as `alembic upgrade head` on container boot — no manual `CREATE TABLE` scripts.

---

## 5. Summary of Deliverables

### Required Documentation Files

| File | PRD Reference | Exact Headers | Status |
|------|---------------|---------------|--------|
| `README.md` | PRD §7.2 | "How to Run", "How to Run Tests", "Example API Calls", "Channels Implemented" | ✅ All present |
| `DECISIONS.md` | PRD §7.3 | "Channel Abstraction Design", "Retry Strategy", "Preference Resolution Order", "Multi-Channel Coordination", "One Thing I Would Do Differently With More Time" | ✅ All five verbatim |
| `AI_USAGE.md` | PRD §7.4 | "Tools I Used", "What Helped Most", "What I Had to Fix", "What AI Struggled With" | ✅ All four verbatim |

### Technical Deliverables

| Deliverable | PRD Reference | Status |
|-------------|---------------|--------|
| Containerized stack | §1.1 mandatory | ✅ `Dockerfile` + `docker-compose.yml`, 5 services, healthchecks |
| 4 channel providers (min: 3) | §5 | ✅ Email, SMS, Push, Webhook — all queue-routed |
| State machine (`RECEIVED→PROCESSING→QUEUED→SENDING→DELIVERED/PERMANENTLY_FAILED`) | §4.1 | ✅ Implemented in `models/enums.py` + `dispatcher.py` |
| Preference resolution priority order | §4.2 | ✅ 7-step pure-function pipeline |
| Quiet hours + HIGH bypass | §4.3 | ✅ `_is_in_quiet_hours` + `not is_high` gate |
| Failure isolation between channels | §4.4 | ✅ Independent queues, tasks, retry counters |
| Retry with backoff + PERMANENTLY_FAILED | §4.5 | ✅ `_DeliveryTask` base + `on_failure` hook |
| Frequency caps | §4.6 (nice-to-have) | ✅ Redis fixed-window, fail-open |
| Channel override | §4.7 | ✅ Step 1 of resolver (bypass all) |
| Scheduled notifications + cancel | §4.8 | ✅ Celery Beat + `dispatch_due_notifications` + `POST /{id}/cancel` |
| Template variable substitution | §1.3 | ✅ Jinja2 `render_template` / `render_html` |
| Priority queues | §1.4 nice-to-have | ✅ `priority` queue, HIGH routes there |
| 6 mandatory tests | §7.5 | ✅ 6/6 files, 20 assertions, run in ~2s with zero infra |

### Test Coverage Map

| Test File | PRD §7.5 Mandatory Test |
|-----------|-------------------------|
| `tests/test_send_notification_endpoint.py` | Send notification end-to-end |
| `tests/test_preference_filtering.py` | Preference filtering |
| `tests/test_retry_on_failure.py` | Retry on failure |
| `tests/test_template_rendering.py` | Template rendering |
| `tests/test_multi_channel_delivery.py` | Multi-channel delivery |
| `tests/test_status_tracking.py` | Status tracking |

All headers in `DECISIONS.md` and `AI_USAGE.md` match the assignment's exact wording verbatim — no renaming or paraphrasing.

---

*Document generated from live codebase analysis — May 2026.*
