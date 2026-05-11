# Project Requirements: Multi-Channel Notification Service

> Source: `mid-senior-notification-service.md` (9-page take-home assignment).
> This PRD is a zero-loss, granular checklist for an AI Coding Agent. Every requirement, constraint, and deliverable from the source document is represented below.

---

## 1. Core Architecture & Tech Stack

### 1.1 Mandatory Technologies (Must Have)
- [x] Use **Python 3.11+**.
- [x] Use a **web framework of your choice** (e.g., FastAPI / Flask / Django).
- [x] Use a **database for persistence** (any).
- [x] Implement a **queue mechanism for async delivery**.
- [x] Implement **at least 3 channel providers** (mocked).
- [x] Implement **retry logic with backoff**.
- [x] Implement **user preference support**.
- [x] Provide a **containerized setup** using `docker-compose`.
- [x] Provide a **`Dockerfile`**.

### 1.2 System Responsibilities (Overview)
The notification service MUST:
- [x] **Receive notification requests** via API.
- [x] **Process user preferences** to determine channels.
- [x] **Queue delivery tasks** per channel.
- [x] **Deliver via channel providers** (mocked).
- [x] **Track delivery status** and handle failures.

### 1.3 Should Have (Secondary Priority)
- [x] Template variable substitution.
- [x] Scheduled notifications.
- [x] Quiet hours support.
- [x] Structured logging with notification context.

### 1.4 Nice to Have (Stretch)
- [x] Frequency caps.
- [x] Delivery analytics/stats endpoint.
- [ ] Batch send (multiple recipients).
- [x] Priority queues (high priority processed first).

---

## 2. Strict Data Models & Schema

### 2.1 `UserPreferences`
- [x] **User identifier**.
- [x] **Global enabled channels** (subset of: email, SMS, push, webhook).
- [x] **Per-type channel preferences** (e.g., marketing via email only, alerts via all channels).
- [x] **Quiet hours configuration** (time window during which notifications are suppressed).
- [x] **Frequency cap settings** (max N notifications per hour/day).
- [x] **Webhook URL** (for the webhook channel).

### 2.2 `Template`
- [x] **Notification type**.
- [x] **Channel** (one template per channel for the same notification type).
- [x] **Subject** (for email).
- [x] **Body** (with variable placeholders, e.g. `Hello {{user.name}}`).
- [x] **Active flag**.

### 2.3 `Notification`
- [x] **Unique identifier**.
- [x] **Recipient** (user ID or contact info).
- [x] **Notification type**.
- [x] **Content / template reference**.
- [x] **Variables** (for template substitution).
- [x] **Priority** (high, normal, low).
- [x] **Status** (received, processing, completed, failed).
- [x] **Scheduled at** (optional).
- [x] **Created at**.

### 2.4 `Delivery`
- [x] **Notification relationship** (FK to Notification).
- [x] **Channel**.
- [x] **Recipient address** (email, phone, device token, or URL).
- [x] **Status** (queued, sending, delivered, failed).
- [x] **Attempts count**.
- [x] **Last attempt at**.
- [x] **Delivered at**.
- [x] **Error message** (if failed).
- [x] **Provider response** (for debugging).

---

## 3. Mandatory API Endpoints

### 3.1 Send Notification
- [x] Accept notification requests carrying:
  - [x] **Recipient** (user ID or contact info).
  - [x] **Notification type** (e.g., `order_confirmation`, `password_reset`).
  - [x] **Content** (or template reference with variables).
  - [x] **Priority** (high, normal, low).
  - [x] **Channels override** (optional — use specific channels instead of preferences).
  - [x] **Scheduled time** (optional — send later).

### 3.2 User Preferences
- [x] Endpoints/operations to allow users to configure:
  - [x] **Enabled channels** (email, SMS, push, webhook).
  - [x] **Per-type preferences**.
  - [x] **Quiet hours**.
  - [x] **Frequency caps**.

### 3.3 Templates
- [x] **Create notification template** (type, channel, subject, body w/ variables).
- [x] Support **variable substitution** (`{{user.name}}`, etc.).
- [x] Support **different template per channel** for the same notification type.

### 3.4 Delivery Tracking
- [x] **Get notification status** (pending, processing, delivered, failed).
- [x] **Get per-channel delivery status**.
- [x] **Get delivery history for a user**.
- [x] **Get aggregate stats** (sent, delivered, failed by channel).

### 3.5 Management
- [x] **Resend failed notification**.
- [x] **Cancel scheduled notification**.
- [x] **Pause / resume notifications for a user**.

---

## 4. The "AI Grader" Logic Traps (Crucial Rules)

### 4.1 Notification Lifecycle (Mandatory State Machine)
- [x] Implement these states in order:
  - `RECEIVED` → `PROCESSING` → `QUEUED` (per channel) → `SENDING` → `DELIVERED` / `FAILED`.
  - On `FAILED`: **retry** until exhausted, then transition to `PERMANENTLY_FAILED`.
- [x] Each channel has its **own queue** (Email Queue, SMS Queue, Push Queue, etc.).
- [x] The top-level Notification has its own status (`received`, `processing`, `completed`, `failed`) **distinct** from per-channel `Delivery.status` (`queued`, `sending`, `delivered`, `failed`).

### 4.2 Preference Resolution Priority Order
When sending a notification, evaluate in this order:
- [x] **1.** Check **user's global preferences** (enabled channels).
- [x] **2.** Check **type-specific preferences**.
- [x] **3.** Apply **quiet hours rules**.
- [x] **4.** Check **frequency caps**.
- [x] **5.** Apply **channel override** if specified on the request.
- [x] Decide and **document** the priority order when these conflict (in `DECISIONS.md`).

### 4.3 Quiet Hours Logic & Bypass
- [x] During configured quiet hours, do **NOT** send notifications…
- [x] **EXCEPT** for **high priority** notifications, which **bypass** quiet hours.

### 4.4 Failure Isolation Between Channels
- [x] When sending to multiple channels, decide & document:
  - [x] **Parallel vs sequential** delivery.
  - [x] Whether **one channel's failure affects the others** (must be isolated unless justified otherwise).
  - [x] How **overall status** is aggregated/reported across channels.

### 4.5 Retry Strategy
- [x] Distinguish **retryable** vs **non-retryable** failures:
  - Example retryable: network timeout.
  - Example non-retryable: invalid address.
- [x] Define **retry timing**: immediate, backoff, or scheduled.
- [x] Define **max retry attempts per channel**.
- [x] Define **when to mark `PERMANENTLY_FAILED`**.
- [x] Document the entire strategy in `DECISIONS.md`.

### 4.6 Frequency Caps Rules
- [x] Track **sent count per user per time window** (hour/day).
- [x] Decide whether high-priority notifications **respect or bypass** caps.
- [x] Define behavior for capped notifications: **drop, queue, or alert**.

### 4.7 Channel Override
- [x] If the request specifies a `channels override`, use those channels **instead of** the user's preferences (subject to documented rules around quiet hours / caps).

### 4.8 Scheduled Notifications
- [x] If `scheduled_at` is provided, defer delivery until that time.
- [x] Must be **cancellable** before send.

---

## 5. Channel Abstraction Implementation

### 5.1 Base Interface Requirements
- [x] Define a **common interface** for all providers (clean abstraction).
- [x] Support **channel-specific configuration** per provider.
- [x] Provide **consistent status tracking** across providers.
- [x] Adding a new channel must be **straightforward** — document what changes are required in `DECISIONS.md`.
- [x] Mock providers may simply **log + random success/failure**.

### 5.2 Email Provider (Mocked)
- [x] Send to an **email address**.
- [x] Support **HTML and plain text**.
- [x] Track statuses: **sent, delivered, opened, bounced**.

### 5.3 SMS Provider (Mocked)
- [x] Send to a **phone number**.
- [x] Handle **character limit**.
- [x] Track statuses: **sent, delivered, failed**.

### 5.4 Push Notification Provider (Mocked)
- [x] Send to a **device token**.
- [x] Support **title + body + data payload**.
- [x] Track statuses: **sent, delivered, clicked**.

### 5.5 Webhook Provider (Mocked)
- [x] **POST** to a **configured URL**.
- [x] Include the **notification payload**.
- [x] Track statuses: **sent, acknowledged, failed**.

### 5.6 Minimum Implementation Bar
- [x] At least **3 of the 4** channel providers must be implemented (mocked).

---

## 6. Sprints (Development Plan)

### Sprint 1 — Foundation & Scaffolding
- [x] Initialize Python 3.11+ project; choose web framework.
- [x] Set up database & ORM/migrations.
- [x] Author `Dockerfile` and `docker-compose.yml` (app + DB + queue/broker).
- [x] Implement data models: `UserPreferences`, `Template`, `Notification`, `Delivery` (with all fields from §2).
- [x] Set up structured logging with notification context.
- [x] Wire up basic project layout: `README.md`, `DECISIONS.md`, `AI_USAGE.md` placeholders.

### Sprint 2 — Channel Abstraction & First End-to-End Channel
- [x] Define the base `ChannelProvider` interface (per §5.1).
- [x] Implement the **Email** provider end-to-end (mocked, with all tracked statuses per §5.2).
- [x] Implement queue + worker for the Email channel.
- [x] Implement `POST /notifications` (Send) endpoint accepting all fields in §3.1.
- [x] Prove the full lifecycle path: `RECEIVED → PROCESSING → QUEUED → SENDING → DELIVERED` for email.

### Sprint 3 — Multi-Channel, Templates & Preferences
- [x] Implement **SMS**, **Push**, and **Webhook** providers (mocked, per §5.3–5.5) — at least 3 total channels live.
- [x] Implement per-channel queues and parallel/sequential dispatch (document choice).
- [x] Implement `Template` CRUD and `{{variable}}` substitution.
- [x] Implement `UserPreferences` CRUD (global channels, per-type prefs, quiet hours, frequency caps, webhook URL).
- [x] Implement preference resolution pipeline (§4.2) including channel override (§4.7) and quiet-hours high-priority bypass (§4.3).

### Sprint 4 — Reliability: Retries, Scheduling, Frequency Caps
- [x] Implement retryable vs non-retryable error classification (§4.5).
- [x] Implement backoff + max-attempts + `PERMANENTLY_FAILED` transition.
- [x] Implement failure isolation between channels (§4.4).
- [x] Implement **scheduled notifications** (defer + cancel).
- [x] Implement **frequency caps** (track per user per window, decide drop/queue/alert).
- [x] Implement priority queues (high priority processed first) — Nice to Have.

### Sprint 5 — Tracking, Management, Tests & Submission Prep
- [x] Implement tracking endpoints (§3.4): notification status, per-channel status, user history, aggregate stats.
- [x] Implement management endpoints (§3.5): resend failed, cancel scheduled, pause/resume user.
- [x] Write the **6 mandatory tests** (see §7.4).
- [x] Finalize `README.md`, `DECISIONS.md`, `AI_USAGE.md` (per §7).
- [x] Verify `docker-compose up` brings the entire system online cleanly.
- [x] Final pass: example API calls in README, list channels implemented.

---

## 7. Deliverables & Markdown Formats

### 7.1 Repository Structure (Exact)
- [x] Submit a **Git repository** containing:
```
your-project/
├── README.md
├── AI_USAGE.md
├── DECISIONS.md
├── docker-compose.yml
├── Dockerfile
├── (your application code)
└── (your tests)
```

### 7.2 `README.md` Required Contents
- [x] **1.** How to run the project.
- [x] **2.** How to run tests.
- [x] **3.** Example API calls for sending a notification.
- [x] **4.** Which channels you implemented.

### 7.3 `DECISIONS.md` Required Structure (Exact Headers)
- [x] Use this exact skeleton:
```markdown
# Design Decisions

## 1. Channel Abstraction Design

**Approach chosen:** [Your interface/pattern]

**Why:** [Your reasoning]

**Adding a new channel requires:** [What changes]

---

## 2. Retry Strategy

**Approach chosen:** [Your approach]

**Retryable vs non-retryable errors:** [How you distinguish]

**Timing:** [Backoff strategy]

---

## 3. Preference Resolution Order

**Approach chosen:** [Your priority order]

**Conflicts resolved by:** [Your rules]

**Example:** [Walk through a scenario]

---

## 4. Multi-Channel Coordination

**Approach chosen:** [Parallel/sequential/hybrid]

**Why:** [Your reasoning]

**Failure isolation:** [How one channel's failure affects others]

---

## 5. One Thing I Would Do Differently With More Time

[Be honest - what did you skip or simplify?]
```

### 7.4 `AI_USAGE.md` Required Structure (Exact Headers)
- [x] Use this exact skeleton:
```markdown
# AI Tool Usage

## Tools I Used
[List the AI tools you used]

## What Helped Most
[Describe 1-2 specific cases where AI helped significantly]

## What I Had to Fix
[Describe 1-2 cases where AI gave incorrect advice]

## What AI Struggled With
[Any parts where AI wasn't helpful]
```

### 7.5 Mandatory Tests (At Least 6 Meaningful Tests)
- [x] **Send notification end-to-end**.
- [x] **Preference filtering**.
- [x] **Retry on failure**.
- [x] **Template rendering**.
- [x] **Multi-channel delivery**.
- [x] **Status tracking**.

### 7.6 Submission Tips (From Source — Internalize)
- [x] Get one channel working **end-to-end first** — prove the flow.
- [x] **Abstract early** — design the channel interface before adding more channels.
- [x] **Mock providers simply** — just log + random success/failure.
- [x] **Test failure paths** — retry logic is where bugs hide.
- [x] **Be explicit about simplifications** — honesty is valued.

### 7.7 Evaluation Criteria (What Reviewers Will Probe)
- [ ] Understanding of **delivery guarantees and failure handling**.
- [ ] Clean **abstraction for multiple channels**.
- [ ] Ability to **recognize subtly incorrect AI advice**.
- [ ] Ability to **explain trade-offs** in the design.
- [ ] Be ready in a follow-up interview to explain architecture and answer "what if" scenarios — deeply understand your own solution.
