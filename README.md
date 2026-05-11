# Multi-Channel Notification Service

A production-shaped notification service that delivers messages over Email, SMS, Push, and Webhook channels with per-channel queues, retry-with-backoff, scheduling, frequency caps, user preferences, quiet-hours, templates, and a full per-channel state machine.

Built end-to-end against the take-home spec in `mid-senior-notification-service.md`. Design rationale lives in `DECISIONS.md`; AI tooling notes live in `AI_USAGE.md`.

---

## 1. How to Run

### Prerequisites
- Docker + Docker Compose
- Free TCP ports `8000` (API), `5432` (Postgres), `6379` (Redis)

### Boot the entire stack

```bash
cp .env.example .env          # required — edit values as needed
docker compose up --build
```

This starts five services:

| Service     | Purpose                                                          |
| ----------- | ---------------------------------------------------------------- |
| `db`        | Postgres 16 (notifications + deliveries + preferences)           |
| `redis`     | Celery broker (DB 0) + result backend (DB 1) + cap counters (DB 2)|
| `api`       | FastAPI on `:8000`. Runs `alembic upgrade head` on start.        |
| `worker`    | Celery worker draining `priority,email,sms,push,webhook,scheduler,default` (priority queue first). |
| `beat`      | Celery Beat — scans for due scheduled notifications every `SCHEDULED_SCAN_INTERVAL_SECONDS`. |

Health check:

```bash
curl http://localhost:8000/healthz
# {"status":"ok"}
```

OpenAPI docs: `http://localhost:8000/docs`.

### Tear down

```bash
docker compose down -v   # -v drops the Postgres volume too
```

### Configuration

All tunables live in `.env` (copied from `.env.example`). Key variables:

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `MAX_RETRY_ATTEMPTS` | `5` | Max retries per Delivery before PERMANENTLY_FAILED |
| `RETRY_BACKOFF_BASE_SECONDS` | `2` | Base for exponential backoff (doubles each retry, jittered) |
| `FREQUENCY_CAP_HIGH_PRIORITY_BYPASS` | `true` | High-priority notifications bypass frequency caps |
| `SCHEDULED_SCAN_INTERVAL_SECONDS` | `30` | How often Celery Beat polls for due scheduled notifications |
| `EMAIL_BOUNCE_RATE` | `0.05` | Fraction of email sends that simulate a hard bounce |
| `EMAIL_TRANSIENT_FAILURE_RATE` | `0.05` | Fraction of email sends that simulate a retryable transient error |
| `SMS_TRANSIENT_FAILURE_RATE` | `0.05` | Fraction of SMS sends that simulate a retryable carrier error |
| `WEBHOOK_TRANSIENT_FAILURE_RATE` | `0.05` | Fraction of webhook POSTs that simulate a retryable 5xx |
| `DEFAULT_FREQUENCY_CAP_PER_HOUR` | `0` | Global default hourly cap (`0` = disabled) |
| `DEFAULT_FREQUENCY_CAP_PER_DAY` | `0` | Global default daily cap (`0` = disabled) |

---

## 2. How to Run Tests

The 6 mandatory tests (PRD §7.5) run with **zero infrastructure** — no Postgres, no Redis, no Celery. They use FastAPI `dependency_overrides`, monkey-patched Celery `delay`/`apply_async`, and a stubbed rate-limiter. See `DECISIONS.md` §5 for the trade-off.

```bash
python -m venv .venv
.\.venv\Scripts\activate          # Windows PowerShell
# source .venv/bin/activate       # Linux / macOS
pip install -r requirements.txt
pytest -q
```

Expected output:

```
....................                                                     [100%]
20 passed in ~2s
```

The 6 mandatory test files map to PRD §7.5 as follows:

| Test file                                  | PRD §7.5 mandatory test    |
| ------------------------------------------ | -------------------------- |
| `tests/test_send_notification_endpoint.py` | Send notification end-to-end |
| `tests/test_preference_filtering.py`       | Preference filtering         |
| `tests/test_retry_on_failure.py`           | Retry on failure             |
| `tests/test_template_rendering.py`         | Template rendering           |
| `tests/test_multi_channel_delivery.py`     | Multi-channel delivery       |
| `tests/test_status_tracking.py`            | Status tracking              |

---

## 3. Example API Calls

### 3.1 Create a user preferences profile

```bash
curl -X PUT http://localhost:8000/users/u1/preferences \
  -H "Content-Type: application/json" \
  -d '{
    "enabled_channels": ["email", "sms"],
    "per_type_preferences": {"alerts": ["sms"]},
    "quiet_hours_start": "22:00",
    "quiet_hours_end":   "07:00",
    "quiet_hours_timezone": "UTC",
    "frequency_caps": {"per_hour": 10, "per_day": 50},
    "webhook_url": null,
    "is_paused": false
  }'
```

### 3.2 Create a per-channel template

```bash
curl -X POST http://localhost:8000/templates \
  -H "Content-Type: application/json" \
  -d '{
    "notification_type": "welcome",
    "channel": "email",
    "subject": "Welcome {{user.name}}",
    "body":    "Hello {{user.name}}, your account is ready.",
    "is_active": true
  }'
```

### 3.3 Send a notification (immediate)

```bash
curl -X POST http://localhost:8000/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "recipient_user_id": "u1",
    "notification_type": "welcome",
    "content": "Hello {{user.name}}",
    "variables": {"user": {"name": "Ada"}},
    "priority": "normal"
  }'
# 202 Accepted
# {
#   "id": "…uuid…",
#   "status": "processing",
#   "scheduled_at": null,
#   "created_at": "2026-…"
# }
```

### 3.4 Send a scheduled notification + cancel it

```bash
# Schedule
curl -X POST http://localhost:8000/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "recipient_user_id": "u1",
    "notification_type": "reminder",
    "content": "Don'\''t forget!",
    "scheduled_at": "2026-12-31T23:00:00Z",
    "priority": "low"
  }'
# Returns id, status="received"

# Cancel before it fires
curl -X POST http://localhost:8000/notifications/<id>/cancel
# 200 OK, status="cancelled"
```

### 3.5 High-priority send (bypasses quiet hours and frequency caps)

```bash
curl -X POST http://localhost:8000/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "recipient_user_id": "u1",
    "notification_type": "security_alert",
    "content": "Login from a new device.",
    "priority": "high",
    "channels_override": ["email", "sms"]
  }'
```

### 3.6 Track status

```bash
curl http://localhost:8000/notifications/<id>
curl http://localhost:8000/notifications/<id>/deliveries
curl "http://localhost:8000/users/u1/notifications?limit=20&offset=0"
curl "http://localhost:8000/stats/deliveries?since=2026-01-01T00:00:00Z"
```

### 3.7 Resend failed deliveries (management)

Resend re-enqueues every `Delivery` in state `failed` or `permanently_failed` for the given notification. Attempt counts are **reset to 0** so the full retry budget (`MAX_RETRY_ATTEMPTS`) is available again. Frequency caps are **not re-evaluated** — the original send already consumed the cap slot. Returns `202` with the list of re-queued `Delivery` objects.

```bash
curl -X POST http://localhost:8000/notifications/<id>/resend
# 202 Accepted
# [{"id": "…", "channel": "email", "status": "queued", …}, …]
```

### 3.8 Batch send (multiple recipients)

Fan-out the same notification to N recipients. Each recipient produces an independent `Notification` (so per-user preferences, quiet hours and frequency caps still apply). Per-recipient `variables` shallow-merge over batch-level `variables`. Failures are **isolated per recipient** — the response is `207 Multi-Status` with one result row per recipient.

```bash
curl -X POST http://localhost:8000/notifications/batch \
  -H "Content-Type: application/json" \
  -d '{
    "notification_type": "welcome",
    "content": "Hello {{user.name}}",
    "variables": {"campaign": "spring2026"},
    "priority": "normal",
    "recipients": [
      {"recipient_user_id": "u1", "variables": {"user": {"name": "Ada"}}},
      {"recipient_user_id": "u2", "variables": {"user": {"name": "Linus"}}},
      {"recipient_contact": "guest@example.com", "variables": {"user": {"name": "Guest"}}}
    ]
  }'
# 207 Multi-Status
# {
#   "accepted": 3,
#   "failed":   0,
#   "results": [
#     {"index": 0, "recipient_user_id": "u1", "notification_id": "…", "status": "processing"},
#     {"index": 1, "recipient_user_id": "u2", "notification_id": "…", "status": "processing"},
#     {"index": 2, "recipient_contact": "guest@example.com", "notification_id": "…", "status": "processing"}
#   ]
# }
```

### 3.9 Pause / resume a user

Dedicated management endpoints toggle `is_paused` without touching any other preference field.

```bash
# Pause — all subsequent notifications for u1 are silently dropped
curl -X POST http://localhost:8000/users/u1/pause
# 200 OK — returns full UserPreferencesResponse with is_paused=true

# Resume
curl -X POST http://localhost:8000/users/u1/resume
# 200 OK — returns full UserPreferencesResponse with is_paused=false
```

> **Note:** `channels_override` requests bypass the paused check (it is an admin/security escape hatch). All other sends for a paused user return no deliveries and the notification is marked `completed` with `filtered_by_pause=true` in the structured log.

---

## 4. Channels Implemented

All four channels listed in PRD §5 are implemented as mocked providers (PRD §5.6 minimum bar is 3 of 4):

| Channel   | Module                  | Address shape    | Tracked statuses (per spec)            |
| --------- | ----------------------- | ---------------- | -------------------------------------- |
| Email     | `app/channels/email.py`   | RFC-5321 address | sent, delivered, opened, bounced       |
| SMS       | `app/channels/sms.py`     | E.164 phone       | sent, delivered, failed                |
| Push      | `app/channels/push.py`    | device token      | sent, delivered, clicked               |
| Webhook   | `app/channels/webhook.py` | https URL         | sent, acknowledged, failed             |

Failure simulation rates are tunable via env vars (see `.env.example`) so tests and demos can pin them deterministically.

---

## 5. Project Layout (top-level)

```
py_exam/
├── README.md            ← this file
├── AI_USAGE.md          ← AI tooling reflection (PRD §7.4)
├── DECISIONS.md         ← design decisions (PRD §7.3)
├── PRD.md               ← spec & sprint checklist (internal)
├── docker-compose.yml   ← api + worker + beat + db + redis
├── Dockerfile           ← Python 3.11+ slim, non-root user
├── alembic/             ← schema migrations
├── app/                 ← application source
│   ├── api/             ← FastAPI routers (notifications, tracking, management, templates, preferences)
│   ├── channels/        ← provider abstraction + 4 mock implementations
│   ├── core/            ← config, db, logging, rate_limiter
│   ├── models/          ← SQLAlchemy models (Notification, Delivery, Template, UserPreferences)
│   ├── schemas/         ← Pydantic request/response schemas
│   ├── services/        ← notifications, dispatcher, preference_resolver, templates
│   └── tasks/           ← Celery tasks (deliver, scheduler)
└── tests/               ← 6 mandatory tests + conftest
```

---

## 6. Where to Read Next

- **Design rationale** (channel abstraction, retry strategy, preference resolution order, multi-channel coordination, what I'd do differently) → `DECISIONS.md`
- **AI tooling notes** → `AI_USAGE.md`
- **Spec & sprint checklist** → `PRD.md`
