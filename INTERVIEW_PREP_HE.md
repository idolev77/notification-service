# שירות התראות רב-ערוצי — סיכום ארכיטקטורה ומדריך הכנה לראיון

> **תפקיד:** אדריכל תוכנה ראשי  
> **מטרה:** חומר הכנה ראשי לראיון טכני בעל-פה.

---

## תוכן עניינים

1. [סקירה ארכיטקטורית מנהלים](#1-סקירה-ארכיטקטורית-מנהלים)
2. [מפת מערכת הקבצים](#2-מפת-מערכת-הקבצים)
3. [צלילה עמוקה לרכיבים המרכזיים](#3-צלילה-עמוקה-לרכיבים-המרכזיים)
4. [תרחישי הכנה לראיון](#4-תרחישי-הכנה-לראיון)
5. [סיכום תוצרים](#5-סיכום-תוצרים)

---

## 1. סקירה ארכיטקטורית מנהלים

### זרימת חיי הבקשה מקצה לקצה

```
HTTP POST /notifications
        │
        ▼
┌─────────────────────────────┐
│ FastAPI (app/api/notifications.py)
│ • אימות Pydantic Schema     │
│ • שמירת Notification        │
│   (status=RECEIVED)         │
│ • החזרת 202 Accepted        │  ← תגובה מנותקת
└──────────────┬───────────────┘
               │
               ▼
┌─────────────────────────────┐
│ services/notifications.py    │
│ • טעינת UserPreferences     │
│ • קריאה ל-preference_resolver│  ← override → paused → enabled →
│   (מחזיר ChannelType[])     │     per-type → quiet hours →
│ • רינדור תבנית לכל ערוץ    │     freq cap → webhook URL gate
│ • יצירת שורות Delivery      │
│   (status=QUEUED)           │
│ • העמסת משימת Celery        │
│   לכל ערוץ (priority queue  │
│   אם HIGH)                  │
└──────────────┬───────────────┘
               │  (Redis broker)
               ▼
┌─────────────────────────────┐
│ Celery worker (לכל ערוץ)   │
│ tasks/deliver.py            │
│ ├─ TX1: claim → SENDING     │  (עמיד לקריסות)
│ ├─ provider.send() מחוץ     │  (ללא חיבור DB פתוח)
│ │   לכל TX                  │
│ └─ TX2: שמירת תוצאה        │
│         + עדכון status הורה │
└──────────────┬───────────────┘
               │
               ▼
   DELIVERED ─┬─ Notification.status
              │   ├─ יש non-terminal  → PROCESSING
   FAILED →   │   ├─ ≥1 DELIVERED    → COMPLETED
   ניסיון חוזר│   └─ 0 DELIVERED     → FAILED
   (exp+jitter)
              ▼
   PERMANENTLY_FAILED (אחרי max_retries או NonRetryable)
```

### פיתוח מונחה-מפרט (Spec-Driven Development)

- המפרט (`mid-senior-notification-service.md`) פורק לרשימת משימות גרנולרית (`PRD.md`) עם הפניה לכל דרישה חובה.
- כל ספרינט הוזן ישירות מסעיפי ה-PRD — קומיטים ממופים לסעיפים.
- 6 קובצי הטסטים בתיקיית `tests/` ממופים 1:1 לדרישות §7.5.

### מוכנות לסביבת ייצור

- **עמידות מצב:** 3 טרנזקציות בדיספאצ'ר — `SENDING` נשמר לפני כל I/O של הספק.
- **סמנטיקת at-least-once:** `task_acks_late=True` + `task_reject_on_worker_lost=True`.
- **בידוד כשלים:** תור נפרד, שורת Delivery נפרדת ומונה ניסיונות חוזרים נפרד לכל ערוץ.
- **קונטיינריזציה:** 5 שירותים ב-`docker-compose` (db/redis/api/worker/beat), healthchecks ל-Postgres, משתמש non-root ב-`Dockerfile`, הרצת Alembic בעלייה.
- **אבזרות (Observability):** לוגים מובנים עם קשר הקשר להתראה (`bind_notification_context`).
- **עדיפויות ולחץ נגד:** worker עם `-Q priority,email,sms,push,webhook,scheduler,default` ו-prefetch=1.

---

## 2. מפת מערכת הקבצים

### קבצי שורש

| קובץ | אחריות |
|------|--------|
| `README.md` | הוראות הרצה, הרצת טסטים, דוגמאות `curl`, טבלת ערוצים. |
| `DECISIONS.md` | לפי PRD §7.3: הפשטת ערוץ, אסטרטגיית ניסיונות חוזרים, סדר העדפות, תיאום רב-ערוצי, "מה הייתי עושה אחרת". |
| `AI_USAGE.md` | לפי PRD §7.4: כלים שנוצלו, מה עזר, מה תוקן, מה בינה מלאכותית התקשתה איתו. |
| `PRD.md` | רשימת משימות פנימית מלאה עם תכנית ספרינטים. |
| `mid-senior-notification-service.md` | מסמך המשימה המקורי. |
| `requirements.txt` | תלויות Python ממוקדות (FastAPI, SQLAlchemy, Celery, Redis, Alembic, structlog). |
| `Dockerfile` | Python 3.11-slim, משתמש non-root, תמונה אחת משותפת ל-api/worker/beat. |
| `docker-compose.yml` | 5-שירות עם healthchecks; מסמך את חשיבות סדר התורות. |
| `alembic.ini` | הגדרות Alembic המצביעות על עץ מיגרציות `alembic/`. |

### מיגרציות — `alembic/`

| קובץ | אחריות |
|------|--------|
| `alembic/env.py` | מחבר Alembic ל-`Base.metadata` של SQLAlchemy. |
| `alembic/script.py.mako` | תבנית ליצירת קובצי revision חדשים. |
| `alembic/versions/0001_initial_schema.py` | יצירת טבלאות `notifications`, `deliveries`, `user_preferences`, `templates` עם JSONB, UUID, ואינדקס חלקי ייחודי. |
| `alembic/versions/0002_per_channel_addresses.py` | הוספת עמודות כתובות לכל ערוץ ב-`user_preferences`. |

### אפליקציה — `app/`

| קובץ | אחריות |
|------|--------|
| `app/main.py` | יצרן אפליקציית FastAPI; הרכבת ראוטרים; רישום `/healthz`; הגדרת לוגים מובנים. |
| `app/worker.py` | הגדרת אפליקציית Celery: תורות לכל ערוץ, סריאליזציית JSON, `acks_late`, `prefetch=1`, לוח זמנים של Beat. |

### שכבת HTTP — `app/api/`

| קובץ | אחריות |
|------|--------|
| `app/api/notifications.py` | `POST /notifications` (קבלה + הכנסה לתור), `POST /notifications/{id}/cancel`. |
| `app/api/preferences.py` | `GET/PUT/DELETE /users/{user_id}/preferences`. |
| `app/api/templates.py` | `POST/GET/DELETE /templates` עם ייחודיות לכל ערוץ. |
| `app/api/tracking.py` | `GET /notifications/{id}`, `/deliveries`, `/users/{id}/notifications`, `/stats/deliveries`. |
| `app/api/management.py` | `POST /notifications/{id}/resend` — הכנסה מחדש לתור של deliveries שנכשלו. |

### הפשטת ערוץ — `app/channels/`

| קובץ | אחריות |
|------|--------|
| `app/channels/base.py` | `ChannelProvider` ABC, `SendPayload`, `SendOutcome`, `RetryableProviderError`/`NonRetryableProviderError`. מגדיר את כל חוזה הספק. |
| `app/channels/registry.py` | דקורטור `@register_provider` + רזולבר `get_provider(channel)`; מנתק את הדיספאצ'ר מהמחלקות הקונקרטיות. |
| `app/channels/email.py` | ספק דוא"ל מדומה; אימות RFC-5321; עוקב אחרי sent/delivered/opened/bounced. |
| `app/channels/sms.py` | ספק SMS מדומה; אימות E.164; סיווג retryable/non-retryable בסגנון carrier. |
| `app/channels/push.py` | ספק Push מדומה; אימות device-token; סיווג בסגנון FCM/APNs. |
| `app/channels/webhook.py` | ספק Webhook מדומה; HTTP 408/425/429/5xx → retryable; 4xx אחר → non-retryable. |

### ליבה חוצת-חתכים — `app/core/`

| קובץ | אחריות |
|------|--------|
| `app/core/config.py` | Pydantic `Settings`: כתובות DB ו-Redis, מגבלות ניסיונות חוזרים, ברירות מחדל לתדירות, מקדמי כשל. |
| `app/core/db.py` | מנוע SQLAlchemy + context manager `session_scope()` (commit/rollback). |
| `app/core/logging.py` | הגדרת `structlog` + `bind_notification_context`/`clear_notification_context` ללוגים עם הקשר. |
| `app/core/rate_limiter.py` | תדירות cap בחלון-קבוע ב-Redis: שני-שלבי MGET → אכיפה → pipeline של INCR+EXPIRE; **fail-open** בתקלת Redis. |

### מודלי דומיין — `app/models/`

| קובץ | אחריות |
|------|--------|
| `app/models/base.py` | Declarative `Base`, UUID PK mixin, timestamp mixin. |
| `app/models/enums.py` | `ChannelType`, `NotificationPriority`, `NotificationStatus`, `DeliveryStatus`. |
| `app/models/notification.py` | מודל ORM `Notification` עם עמודת מכונת מצבים. |
| `app/models/delivery.py` | `Delivery` (FK→Notification), מצב לכל ערוץ, `attempts`, `provider_response` JSONB. |
| `app/models/template.py` | `Template`: שורה לכל (type, channel) עם אינדקס חלקי ייחודי. |
| `app/models/user_preferences.py` | `UserPreferences` עם עמודות JSONB ושדות quiet-hours. |

### סכמות Pydantic — `app/schemas/`

| קובץ | אחריות |
|------|--------|
| `app/schemas/notifications.py` | צורות בקשה/תגובה עבור `/notifications`. |
| `app/schemas/preferences.py` | אימות `quiet_hours_*`, תדירות caps, enums של ערוצים. |
| `app/schemas/templates.py` | צורות CRUD לתבניות. |
| `app/schemas/tracking.py` | מודלי תגובה לעקיבה וסטטיסטיקות. |

### שירותים עסקיים — `app/services/`

| קובץ | אחריות |
|------|--------|
| `app/services/notifications.py` | מתזמר: שומר Notification, קורא לרזולבר, יוצר Deliveries, מכניס לתורות (ניתוב לתור priority ל-HIGH). |
| `app/services/dispatcher.py` | מבצע משלוח ב-3 טרנזקציות (claim → קריאה חיצונית → שמירת תוצאה → עדכון הורה). |
| `app/services/preference_resolver.py` | pipeline פונקציה טהורה (override → paused → enabled → per-type → quiet hours → cap → webhook URL). |
| `app/services/templates.py` | רינדור Jinja2 עם `TemplateRenderError`; `render_template` ו-`render_html`. |

### עובדים אסינכרוניים — `app/tasks/`

| קובץ | אחריות |
|------|--------|
| `app/tasks/deliver.py` | משימות Celery לכל ערוץ (`deliver_email/sms/push/webhook`) + בסיס `_DeliveryTask` עם autoretry + on_failure → PERMANENTLY_FAILED. |
| `app/tasks/scheduler.py` | `dispatch_due_notifications` מונע Beat: סורק התראות עם `scheduled_at <= now` ומכניס לתור. |

### טסטים — `tests/`

| קובץ | מיפוי PRD §7.5 |
|------|----------------|
| `tests/conftest.py` | fixtures משותפים: `dependency_overrides`, monkey-patch ל-`delay`/`apply_async`, stub rate limiter. |
| `tests/test_send_notification_endpoint.py` | מסלול קבלה מקצה לקצה |
| `tests/test_preference_filtering.py` | ענפי הרזולבר |
| `tests/test_retry_on_failure.py` | מעברי retryable vs non-retryable |
| `tests/test_template_rendering.py` | המרת Jinja + טיפול במשתנה חסר |
| `tests/test_multi_channel_delivery.py` | deliveries מקבילים + צבירת status |
| `tests/test_status_tracking.py` | endpoint-י עקיבה |

---

## 3. צלילה עמוקה לרכיבים המרכזיים

### 3.1 הפשטת ערוץ (Channel Abstraction)

התבנית היא **Abstract Base Class + רישום-עצמי (registry)**.

**`ChannelProvider` ABC** (`app/channels/base.py`) מגדיר חוזה מינימלי:
- `channel_type: ChannelType` — attribute מחלקתי; `__init_subclass__` נכשל מיידית אם נשכח.
- `validate_address(address)` — בדיקה מוקדמת; זורק `NonRetryableProviderError` על קלט פגום.
- `send(payload: SendPayload) -> SendOutcome` — מבצע את ניסיון המשלוח.

**`SendPayload` אחיד** נושא שדות על-קבוצה (`subject`, `html_body`, `title`, `data`) — הדיספאצ'ר **לעולם לא מסתעף לפי ערוץ**. כל ספק קורא רק את השדות הרלוונטיים לו.

**סיווג כשלים נמצא בספק** — רק הספק יודע מה משמעותו של HTTP 422 אצל הספק שלמעלה ממנו. הדיספאצ'ר נשאר אגנוסטי לערוץ:

```
RetryableProviderError    → Celery autoretry עם backoff
NonRetryableProviderError → הוק on_failure → PERMANENTLY_FAILED מיידי
```

**Registry רישום-עצמי** (`app/channels/registry.py`):
- דקורטור `@register_provider` על כל תת-מחלקה — רישום אוטומטי.
- `get_provider(channel)` פותר בזמן ה-dispatch.
- **אפס שינויים בדיספאצ'ר בעת הוספת ערוץ.**

#### הוספת ערוץ חדש — 4 צעדים בלבד

1. ירוש מ-`ChannelProvider`, הגדר `channel_type`, מימוש `validate_address` ו-`send`.
2. קשט עם `@register_provider`.
3. הוסף `Queue` ב-`app/worker.py` ורשומת `task_routes`.
4. הוסף את שם התור לרשימת `-Q` ב-`docker-compose.yml`.

---

### 3.2 מנוע פתרון העדפות (Preference Resolution)

מומש כ**פונקציה טהורה ללא תופעות לוואי** `resolve_channels_for_notification` ב-`app/services/preference_resolver.py`. קדימות קשיחה — אין מיזוג; כל צעד או מעביר את רשימת המועמדים הלאה או מחזיר `[]`.

#### Pipeline הפתרון

| צעד | כלל | אפקט |
|-----|-----|------|
| **1** | `channels_override` קיים | **עוקף את כל ה-pipeline.** שימוש ב-override כפשוטו (חלון מנהלים לאיפוס סיסמה / התראת אבטחה). |
| **2** | אין UserPreferences או `is_paused=True` | מחזיר `[]`, לוג `filtered_by_pause`. לא 4xx — הבקשה עצמה תקינה. |
| **3** | `enabled_channels` גלובלי | התחל רשימת מועמדים (רשימת allowed). |
| **4** | `per_type_preferences[notification_type]` | **חיתוך** עם enabled — מצמצם בלבד, לעולם לא מרחיב. מונע from old per-type entries להחיות ערוצים שנוטרלו גלובלית. |
| **5** | חלון שעות שקט | הסרת כל הערוצים **אלא אם** `priority=HIGH`. עיטוף חצות מטופל. `start == end` = "תמיד שקט". |
| **6** | תדירות caps (Redis) | הסרת הכל **אלא אם** HIGH ו-`frequency_cap_high_priority_bypass=true`. **fail-open** בתקלת Redis. |
| **7** | שער webhook URL | הסרת `WEBHOOK` אם `user_pref.webhook_url is None`. |

#### דוגמה עם מספרים

- **העדפות משתמש:** `enabled_channels=[email, sms]`, `per_type_preferences={"marketing": [email]}`, `quiet_hours=22:00–07:00 Asia/Jerusalem`, `is_paused=False`.
- **בקשה:** `notification_type=marketing`, `priority=NORMAL`, נשלחת ב-04:00 שעון מקומי.

| צעד | תוצאה |
|-----|--------|
| 1 — אין override | המשך |
| 2 — לא מושהה | המשך |
| 3 — enabled | מועמדים = `[email, sms]` |
| 4 — per-type מצמצם | מועמדים = `[email]` |
| 5 — שעות שקט, עדיפות NORMAL | **הסרת הכל** → `[]` |
| **תוצאה** | `channels=[]`, `filtered_by_quiet_hours=true` |

אותה בקשה עם `priority=HIGH` → צעד 5 נעקף → `channels=[email]` → delivery דוא"ל מוכנס לתור.

---

### 3.3 אמינות ולוגיקת ניסיונות חוזרים (Reliability & Retry)

#### מודל חריגות דו-ציר

```python
class _DeliveryTask(Task):
    autoretry_for       = (RetryableProviderError,)   # NonRetryable מתפשט → on_failure
    retry_backoff       = True                        # אקספוננציאלי
    retry_backoff_max   = 600                         # מגבלת המתנה: 10 דקות
    retry_jitter        = True                        # ±50% אקראיות
    max_retries         = settings.max_retry_attempts # ברירת מחדל: 5
    acks_late           = True
```

#### טבלת סיווג כשלים

| ערוץ | Retryable | Non-Retryable |
|------|-----------|---------------|
| **דוא"ל** | bounce חולף (5xx-equiv), rate-limit | Hard bounce, כתובת פגומה |
| **SMS** | Carrier 5xx, timeout רשת | Carrier-rejected, E.164 פגום, גוף ריק |
| **Push** | FCM/APNs 5xx, timeout חולף | Device token לא תקף/פג תוקף, כותרת חסרה |
| **Webhook** | HTTP 408/425/429/500/502/503/504, שגיאות רשת/DNS | 4xx אחר, URL לא http(s), URL > 2048 תווים |

#### רצף Backoff (base=2 שניות)

```
ניסיון 1: ~2 שניות המתנה
ניסיון 2: ~4 שניות
ניסיון 3: ~8 שניות
ניסיון 4: ~16 שניות
ניסיון 5: ~32 שניות
(מוגבל ל-600 שניות, ±50% jitter בכל ניסיון)
```

ה-jitter מסנכרן ניסיונות חוזרים כך שאלפי כשלים מקבילים לא יפגעו בספק המתאושש בו-זמנית.

#### PERMANENTLY_FAILED — שני מסלולים

1. **מיידי** על `NonRetryableProviderError` — חוסך N ניסיונות מיותרים נגד כתובת פגומה מבנית.
2. **אחרי `max_retries`** של `RetryableProviderError` — Celery מיצה autoretry → אותו הוק `on_failure`.

שני המסלולים קוראים ל-`mark_delivery_permanently_failed` שגם **מחשב מחדש את status ה-Notification ההורה** כדי שאף delivery לא ישאיר את ההורה תקוע ב-`PROCESSING`.

#### פריסת 3 הטרנזקציות בדיספאצ'ר

```
TX1 (claim):
  Delivery: QUEUED/FAILED → SENDING
  attempts++, last_attempt_at=now
  COMMIT ← עמיד לקריסות

  ← אין חיבור DB פתוח →

קריאה לספק (מחוץ לכל TX, I/O רשת ארוך)

TX2-הצלחה:
  status=DELIVERED, delivered_at=now, provider_response=...
  חישוב מחדש של Notification.status
  COMMIT

TX2-כישלון:
  status=FAILED, error_message=...
  חישוב מחדש של Notification.status
  COMMIT, ואז re-raise → Celery autoretry או on_failure
```

---

### 3.4 אסטרטגיה אסינכרונית (Async Strategy)

| רכיב | טכנולוגיה | הגדרה |
|------|-----------|--------|
| Broker | Redis DB 0 | `celery_broker_url` |
| Result backend | Redis DB 1 | `celery_result_backend` |
| מונים תדירות | Redis DB 2 | namespace מבודד |
| תורות | `priority`, `email`, `sms`, `push`, `webhook`, `scheduler`, `default` | אחד לכל ערוץ |
| סדר worker | `-Q priority,email,sms,push,webhook,scheduler,default` | `priority` ראשון |
| Prefetch | `worker_prefetch_multiplier=1` | ללא הרעבה של אחים |
| סריאליזציה | JSON בלבד | ללא בעיות אבטחה של pickle |
| סמנטיקה | `task_acks_late=True` + `task_reject_on_worker_lost=True` | at-least-once |

**בידוד כשלים:** backlog גדול בתור webhook לא יחסום throughput של דוא"ל — כל ערוץ בעל תור נפרד, slice נפרד של workers ומונה ניסיונות חוזרים נפרד.

**עמידות לקריסה:** TX1 מ-commit ל-`SENDING` לפני I/O ספק. אם worker נהרג תוך כדי ניסיון, המשימה מוכנסת מחדש לתור (acks_late) ומשוחזרת מה-`SENDING` בצורה נכונה.

---

## 4. תרחישי הכנה לראיון

### "מה קורה אם ספק ה-SMS מושבת אבל ה-דוא"ל פועל?"

כל ערוץ הוא **שורת Delivery עצמאית על תור Celery עצמאי**. הביצוע מקבילי:

- **משימת דוא"ל** על תור `email`: מסתיימת → `Delivery.status=DELIVERED`.
- **משימת SMS** על תור `sms`: זורקת `RetryableProviderError` → Celery מנסה מחדש עם exponential backoff (≈2s → 4s → 8s → 16s → 32s, ±jitter) עד `max_retries` → `on_failure` → `PERMANENTLY_FAILED`.

`_recompute_notification_status` רץ אחרי **כל TX2**:
- ברגע שהדוא"ל מצליח, הכלל "≥1 DELIVERED + הכל terminal" → `Notification.status=COMPLETED`.
- שגיאת ה-SMS נשמרת ב-`Delivery.error_message` לצורכי observability מבלי לזהם את ה-status ההורה.
- **כישלון ערוץ אחד לא משפיע לעולם על ערוץ אחר** (PRD §4.4 — כל ערוץ בעל משימה, מונה ניסיונות ותור משלו).

---

### "איך וידאת שהתראות בעדיפות גבוהה עוקפות שעות שקט?"

ב-`app/services/preference_resolver.py`, צעד 5:

```python
in_quiet = _is_in_quiet_hours(user_pref, now or datetime.now(tz=ZoneInfo("UTC")))
is_high  = notification.priority is NotificationPriority.HIGH
if in_quiet and not is_high:
    return ResolutionResult(channels=[], filtered_by_quiet_hours=True)
```

תנאי `not is_high` מבטיח ש-**HIGH-priority עוקפת לחלוטין את מסנן שעות השקט**. אותו flag עוקף גם תדירות caps (צעד 6) כאשר `frequency_cap_high_priority_bypass=true`.

שני העקיפות מבטאים אותו עיקרון תפעולי: **"ההתראה הזו קריטית מספיק לעקוף הגדרות נוחות משתמש"**.

בצד Celery, deliveries בעדיפות HIGH מנותבים לתור הייעודי `priority`, שה-worker מרוקן **ראשון** לפי סדר `-Q`.

---

### "איך הייתם מרחיבים את המערכת ל-1 מיליון התראות לשעה?"

≈278 התראות/שנייה sustained. אסטרטגיה בשכבות:

#### שכבה 1 — שכבת API
- FastAPI stateless מאחורי load balancer; scale אופקי לפי RPS.
- מסלול הקבלה הוא **O(1)** (insert + enqueue), ללא I/O ספק inline.

#### שכבה 2 — שכבת Worker
- Scale לכל תור באופן עצמאי:
  ```bash
  docker-compose scale worker_email=20 worker_sms=10 worker_webhook=30
  ```
- backlog webhook לא מחייב scale של דוא"ל.

#### שכבה 3 — Postgres
- צוואר הבקבוק העיקרי: inserts של `Delivery`.
- פתרונות: connection pooling (כבר ב-SQLAlchemy), partition של `deliveries` לפי חודש `created_at`, העברת `provider_response` JSONB לאחסון קר אחרי 30 יום.

#### שכבה 4 — Redis Broker
- Redis single-node מטפל ב-≈100k ops/sec.
- ב-1M/שעה עם ≈5 ops לכל התראה: נדרש **Redis Cluster** או partitioning לפי ערוץ.

#### שכבה 5 — שיפורי Hot-Path
- Batch inserts דרך `COPY` לשורות Delivery.
- rate limiter בחלון-גולש (sorted-sets) לסילוק overshoot 2N בגבולות חלון-קבוע.
- `SELECT ... FOR UPDATE SKIP LOCKED` על claim של scheduler להרצת כמה replicas Beat.
- Dead-letter queues לכל ערוץ + circuit breakers לכל ספק.

#### שכבה 6 — Observability לסקיילינג אוטומטי
- מטריקות עומק תור → טריגר autoscale.
- חביון p95 לכל ספק → סף circuit-breaker.

---

### "למה בחרתם בדאטאבייס/פריימוורק הספציפי הזה?"

#### PostgreSQL — למה לא MySQL או MongoDB?

| דרישה | למה Postgres מנצח |
|-------|-------------------|
| `per_type_preferences`, `frequency_caps`, `provider_response` — מאוחסנים כ-JSONB ו**נשאלים לתוכם** | JSONB עם GIN indexing. MySQL JSON חסר GIN; MongoDB מאבד FK relational integrity. |
| `Template (notification_type, channel) WHERE is_active` — אינדקס חלקי ייחודי | Postgres native. MySQL חסר תמיכה באינדקסים חלקיים. |
| timestamps עם awareness לאזור זמן | Postgres native `TIMESTAMPTZ`. |
| תפעול פשוט ומשעמם בסקייל הזה | Postgres בקרב ומוכח תפעולית. |

#### FastAPI — למה לא Flask או Django?

| תכונה | השפעה |
|-------|--------|
| Async-native | מתאים ל-endpoints רב-I/O. |
| Pydantic validation | חינם בגבול הבקשה, משמש גם כמודלי תגובה. |
| OpenAPI docs אוטומטי | Reviewer יכול לחקור ב-`/docs` ללא README. |
| `dependency_overrides` | מאפשר 6 טסטים חובה עם **אפס תשתית** (ללא Postgres, ללא Redis). |

#### Celery + Redis — למה לא RQ או תור עצמי?

| תכונה | השפעה |
|-------|--------|
| ניתוב לכל תור | ממפה 1:1 ל-PRD §4.1 (תור אחד לכל ערוץ). |
| `autoretry_for` + `retry_backoff` | מממש PRD §4.5 ללא קוד hand-rolled. |
| `acks_late` + `reject_on_worker_lost` | at-least-once מובטח ע"י ה-broker. |
| Beat scheduler | מממש PRD §4.8 ללא שירות cron נפרד. |

---

## 5. סיכום תוצרים

### קבצי תיעוד נדרשים

| קובץ | הפניה PRD | כותרות מדויקות | סטטוס |
|------|-----------|----------------|--------|
| `README.md` | PRD §7.2 | "How to Run", "How to Run Tests", "Example API Calls", "Channels Implemented" | ✅ כולן קיימות |
| `DECISIONS.md` | PRD §7.3 | "Channel Abstraction Design", "Retry Strategy", "Preference Resolution Order", "Multi-Channel Coordination", "One Thing I Would Do Differently With More Time" | ✅ כל 5 המדויקות |
| `AI_USAGE.md` | PRD §7.4 | "Tools I Used", "What Helped Most", "What I Had to Fix", "What AI Struggled With" | ✅ כל 4 המדויקות |

### תוצרים טכניים

| תוצר | הפניה PRD | סטטוס |
|------|-----------|--------|
| Stack מקונטינר | §1.1 חובה | ✅ `Dockerfile` + `docker-compose.yml`, 5 שירותים, healthchecks |
| 4 ספקי ערוץ (מינימום: 3) | §5 | ✅ דוא"ל, SMS, Push, Webhook — כולם מנותבים לתורות |
| מכונת מצבים (`RECEIVED→PROCESSING→QUEUED→SENDING→DELIVERED/PERMANENTLY_FAILED`) | §4.1 | ✅ ב-`models/enums.py` + `dispatcher.py` |
| סדר קדימות בפתרון העדפות | §4.2 | ✅ pipeline פונקציה טהורה ב-7 צעדים |
| שעות שקט + עקיפה ל-HIGH | §4.3 | ✅ `_is_in_quiet_hours` + שער `not is_high` |
| בידוד כשלים בין ערוצים | §4.4 | ✅ תורות, משימות ומוני ניסיונות עצמאיים |
| Retry עם backoff + PERMANENTLY_FAILED | §4.5 | ✅ בסיס `_DeliveryTask` + הוק `on_failure` |
| תדירות caps | §4.6 (nice-to-have) | ✅ חלון-קבוע ב-Redis, fail-open |
| Channel override | §4.7 | ✅ צעד 1 של הרזולבר (עוקף הכל) |
| התראות מתוזמנות + ביטול | §4.8 | ✅ Celery Beat + `dispatch_due_notifications` + `POST /{id}/cancel` |
| החלפת משתני תבנית | §1.3 | ✅ Jinja2 `render_template` / `render_html` |
| תורות עדיפות | §1.4 nice-to-have | ✅ תור `priority`, HIGH מנותב לשם |
| 6 טסטים חובה | §7.5 | ✅ 6/6 קבצים, 20 assertions, פועל ב-≈2 שניות ללא תשתית |

### מפת כיסוי טסטים

| קובץ טסט | טסט חובה PRD §7.5 |
|----------|-------------------|
| `tests/test_send_notification_endpoint.py` | שליחת התראה מקצה לקצה |
| `tests/test_preference_filtering.py` | סינון העדפות |
| `tests/test_retry_on_failure.py` | ניסיונות חוזרים על כישלון |
| `tests/test_template_rendering.py` | רינדור תבנית |
| `tests/test_multi_channel_delivery.py` | משלוח רב-ערוצי |
| `tests/test_status_tracking.py` | עקיבת סטטוס |

כל הכותרות ב-`DECISIONS.md` וב-`AI_USAGE.md` תואמות את ניסוח המשימה המקורי **במדויק** — ללא שינוי שמות או פרפרזה.

---

*מסמך זה נוצר מניתוח קוד חי — מאי 2026.*
