# AI Tool Usage

## Tools I Used

- **GitHub Copilot Chat (Claude-class assistant)** in VS Code — primary pair-programmer for the entire build. Used as a Spec-Driven-Development partner: every sprint started by handing it the relevant PRD section and asking it to act on the spec, not on intuition.
- **GitHub Copilot inline completions** — for boilerplate (Pydantic field definitions, repetitive Celery task wrappers, dataclass scaffolding).

## What Helped Most

1. **Pre-implementation sprint planning via `PRD.md`.**
   Before writing a single line of code, I used the AI assistant to transform the original assignment (`mid-senior-notification-service.md`) into a zero-loss, sprint-structured checklist (`PRD.md`). Every requirement, constraint, and deliverable was broken down into 5 sprints with granular checkboxes. This meant that when I started coding, each sprint had a clearly scoped, independently verifiable set of tasks — no ambiguity about what "done" looked like. The AI was used as a spec-analysis partner here, not a code generator.

2. **The 3-transaction dispatcher layout in `app/services/dispatcher.py`.**
   I described the desired invariant out loud — "the SENDING state must be durable across a worker crash, the provider call must NOT hold a Postgres connection open, and the success/failure persistence must recompute the parent Notification's aggregate status atomically" — and the assistant produced the TX1 / external-call / TX2 split with `session_scope` boundaries on the first try. Hand-rolling that and getting the boundaries correct would have taken me an hour of debugging connection-pool exhaustion under load.


## What I Had to Fix

1. **Initial frequency-cap implementation tried `INCR` before checking the cap.**
   That would have charged the counter even for denied sends — a subtle off-by-one that lets users sneak one extra notification per window per cap-level. I caught it on review and rewrote the limiter as a two-phase "MGET → enforce → INCR+EXPIRE pipeline" so the counter only moves on accepted sends. Documented in `DECISIONS.md` §3 step 6.

2. **Suggested SQLAlchemy `JSON` over `JSONB` "for portability".**
   `JSONB` is the right choice because we genuinely query into the column (`per_type_preferences[notification_type]`) and need GIN-indexable values; `JSON` would have lost that. I overrode the suggestion and kept `JSONB`. The trade-off (no SQLite-based tests) is documented in `DECISIONS.md` §5.

## What AI Struggled With

1.  **Multi-file invariants that span code + config + docker-compose + Alembic.**
  When I added the `priority` and `scheduler` queues in Sprint 4, the code change was correct but the assistant initially missed the `docker-compose.yml` worker `-Q` ordering (`priority` MUST come first or it gets starved by the per-channel queues). I had to explicitly remind it that worker queue order in Celery is non-trivially significant — it isn't a property visible from any single file.

2.  **Quiet-hours wrap-around logic** on the first pass produced a correct `start < end` branch but an inverted `start > end` (overnight) branch. Reading an adversarial scenario aloud ("22:00 to 07:00, 'now' is 23:30 UTC") was needed before it produced the correct condition.

3.  **Knowing what NOT to over-engineer.** The assistant repeatedly tried to add per-channel circuit breakers, SLOs, dead-letter queues, and metrics scaffolding "for production". I held the line: an exam ships with the spec, not with everything I'd build for a Series-B startup. The implementation-discipline rule had to be enforced manually on every sprint.
