"""
User preferences endpoints (PRD §3.2).

Routes:
  - GET /users/{user_id}/preferences
  - PUT /users/{user_id}/preferences          (upsert; full replacement)
  - DELETE /users/{user_id}/preferences

WHY upsert via PUT instead of POST + PATCH:
  - Preferences have a single canonical row per user (user_id is the PK);
    the resource path therefore identifies it deterministically.
  - PUT semantics (idempotent, full-replacement) match the request shape
    we send back to the client on GET — round-tripping just works.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Response, status
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api._etag import attach_etag, check_version_or_412, parse_if_match
from app.core.db import get_db_session
from app.core.logging import get_logger
from app.models import UserPreferences
from app.models.enums import ChannelType
from app.schemas.preferences import (
    UserPreferencesResponse,
    UserPreferencesUpsertRequest,
)

router = APIRouter(prefix="/users/{user_id}/preferences", tags=["preferences"])

_logger = get_logger(__name__)

# user_id is opaque from upstream IDP; bound length to match DB column.
_USER_ID_PATH = Path(min_length=1, max_length=128)

# Sentinel `If-Match: *` means "match any existing version" — used by clients
# that want to refuse to CREATE (only update) or to assert "row must exist".
# Per RFC 9110 §13.1.1, `*` matches any existing representation but does NOT
# match a missing one. We honor it on UPDATE; on CREATE we still 412 if the
# client sent a concrete version.
_IF_MATCH_WILDCARD = "*"


@router.get("", response_model=UserPreferencesResponse)
def get_user_preferences(
    response: Response,
    user_id: str = _USER_ID_PATH,
    db: Session = Depends(get_db_session),
) -> UserPreferencesResponse:
    prefs = db.get(UserPreferences, user_id)
    if prefs is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No preferences exist for user {user_id!r}.",
        )
    attach_etag(response, prefs.version_id)
    return _to_response(prefs)


@router.put("", response_model=UserPreferencesResponse)
def upsert_user_preferences(
    request: UserPreferencesUpsertRequest,
    response: Response,
    user_id: str = _USER_ID_PATH,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db_session),
) -> UserPreferencesResponse:
    """
    Create or fully replace the preferences row for a user.

    Concurrency contract:
      - **Update existing row:** caller MUST send `If-Match: "<version>"`
        matching the current `version_id`. Stale value -> 412. Missing -> 428.
        `If-Match: *` is also accepted (matches any existing version).
      - **Create new row:** caller MAY send `If-Match: *` (or omit the
        header). Sending a concrete version like `"3"` for a non-existent
        row yields 412 because the precondition cannot be satisfied.
    """
    prefs = db.get(UserPreferences, user_id)
    is_create = prefs is None

    if is_create:
        if if_match is not None and if_match.strip().strip('"') not in (
            _IF_MATCH_WILDCARD,
            "",
        ):
            raise HTTPException(
                status_code=status.HTTP_412_PRECONDITION_FAILED,
                detail=(
                    "If-Match references a specific version but the "
                    "resource does not yet exist."
                ),
            )
        prefs = UserPreferences(user_id=user_id)
        db.add(prefs)
    else:
        if if_match is None:
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail=(
                    "If-Match header is required to update an existing "
                    "preferences row. GET it first and quote its ETag."
                ),
            )
        if if_match.strip().strip('"') != _IF_MATCH_WILDCARD:
            check_version_or_412(prefs.version_id, parse_if_match(if_match))

    # Persist enums as their string values — JSONB stores plain JSON.
    prefs.enabled_channels = [c.value for c in request.enabled_channels]
    prefs.per_type_preferences = {
        ntype: [c.value for c in channels]
        for ntype, channels in request.per_type_preferences.items()
    }
    prefs.quiet_hours_start = request.quiet_hours_start
    prefs.quiet_hours_end = request.quiet_hours_end
    prefs.quiet_hours_timezone = request.quiet_hours_timezone
    prefs.frequency_caps = request.frequency_caps
    prefs.webhook_url = request.webhook_url
    prefs.email_address = request.email_address
    prefs.phone_number = request.phone_number
    prefs.device_token = request.device_token
    prefs.is_paused = request.is_paused

    try:
        db.commit()
    except StaleDataError as exc:
        # Race: someone else updated the row between our SELECT and UPDATE.
        # SQLAlchemy's version_id_col detected the mismatch and aborted.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Preferences row was modified concurrently; reload and retry.",
        ) from exc
    db.refresh(prefs)
    attach_etag(response, prefs.version_id)
    _logger.info(
        "preferences.upsert",
        user_id=user_id,
        created=is_create,
        new_version=prefs.version_id,
    )
    return _to_response(prefs)


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_user_preferences(
    user_id: str = _USER_ID_PATH,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db_session),
) -> Response:
    """Delete a user's preferences. Requires `If-Match` to prevent racing."""
    prefs = db.get(UserPreferences, user_id)
    if prefs is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No preferences exist for user {user_id!r}.",
        )
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required to delete this resource.",
        )
    if if_match.strip().strip('"') != _IF_MATCH_WILDCARD:
        check_version_or_412(prefs.version_id, parse_if_match(if_match))

    db.delete(prefs)
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Preferences row was modified concurrently; reload and retry.",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Internal serialization helper
# ---------------------------------------------------------------------------

def _to_response(prefs: UserPreferences) -> UserPreferencesResponse:
    return UserPreferencesResponse(
        user_id=prefs.user_id,
        enabled_channels=[ChannelType(c) for c in prefs.enabled_channels],
        per_type_preferences={
            ntype: [ChannelType(c) for c in channels]
            for ntype, channels in prefs.per_type_preferences.items()
        },
        quiet_hours_start=prefs.quiet_hours_start,
        quiet_hours_end=prefs.quiet_hours_end,
        quiet_hours_timezone=prefs.quiet_hours_timezone,
        frequency_caps=prefs.frequency_caps,
        webhook_url=prefs.webhook_url,
        email_address=prefs.email_address,
        phone_number=prefs.phone_number,
        device_token=prefs.device_token,
        is_paused=prefs.is_paused,
    )


# ---------------------------------------------------------------------------
# Pause / Resume — dedicated endpoints (PRD §3.5).
# ---------------------------------------------------------------------------
# WHY a dedicated endpoint instead of "PUT /preferences with is_paused=true":
#   - PUT is a full-replacement operation and requires an If-Match header.
#     Forcing an operator to GET the current ETag, parse it, and replay the
#     entire preferences document just to flip a single boolean is a
#     terrible UX for what should be a single-call admin action ("pause Alice
#     right now, she's getting alert-spammed").
#   - The pause flag is a semantic action ("pause this user"), not a
#     stateful resource edit. Modeling it as POST keeps the intent clear
#     in audit logs and access-control rules.
#   - We still take a row-level lock so a concurrent PUT cannot resurrect
#     the row mid-flip. No If-Match required because the action is
#     idempotent: pausing an already-paused user is a no-op.

def _set_paused(
    *, user_id: str, paused: bool, db: Session
) -> UserPreferences:
    """
    Atomically flip the `is_paused` flag, locking the row so a concurrent
    PUT /preferences cannot race us. Auto-creates an empty preferences row
    if one does not yet exist (so an operator can preemptively pause a new
    user before they have a configured profile).
    """
    from sqlalchemy import select as _select

    prefs = db.execute(
        _select(UserPreferences)
        .where(UserPreferences.user_id == user_id)
        .with_for_update()
    ).scalar_one_or_none()

    if prefs is None:
        prefs = UserPreferences(user_id=user_id, is_paused=paused)
        db.add(prefs)
    else:
        prefs.is_paused = paused

    try:
        db.commit()
    except StaleDataError as exc:  # extremely unlikely under FOR UPDATE
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Preferences row changed concurrently; retry.",
        ) from exc
    db.refresh(prefs)
    return prefs


@router.post("/pause", response_model=UserPreferencesResponse)
def pause_user(
    response: Response,
    user_id: str = _USER_ID_PATH,
    db: Session = Depends(get_db_session),
) -> UserPreferencesResponse:
    """Pause all notifications for a user. Idempotent."""
    prefs = _set_paused(user_id=user_id, paused=True, db=db)
    attach_etag(response, prefs.version_id)
    _logger.info("preferences.paused", user_id=user_id)
    return _to_response(prefs)


@router.post("/resume", response_model=UserPreferencesResponse)
def resume_user(
    response: Response,
    user_id: str = _USER_ID_PATH,
    db: Session = Depends(get_db_session),
) -> UserPreferencesResponse:
    """Resume notifications for a user. Idempotent."""
    prefs = _set_paused(user_id=user_id, paused=False, db=db)
    attach_etag(response, prefs.version_id)
    _logger.info("preferences.resumed", user_id=user_id)
    return _to_response(prefs)
