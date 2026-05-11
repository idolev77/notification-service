"""
Template CRUD endpoints (PRD §3.3).

Operations:
  - POST   /templates           create a new template
  - GET    /templates           list (optionally filtered by type/channel)
  - GET    /templates/{id}      fetch one
  - PATCH  /templates/{id}      partial update
  - DELETE /templates/{id}      delete

The "active" partial unique index in the DB enforces "one active template
per (type, channel)"; the API turns that constraint into a friendly 409.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api._etag import attach_etag, check_version_or_412, parse_if_match
from app.core.db import get_db_session
from app.core.logging import get_logger
from app.models import Template
from app.models.enums import ChannelType
from app.schemas.templates import (
    TemplateCreateRequest,
    TemplateResponse,
    TemplateUpdateRequest,
)

router = APIRouter(prefix="/templates", tags=["templates"])

_logger = get_logger(__name__)

_IF_MATCH_WILDCARD = "*"


@router.post(
    "",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_template(
    request: TemplateCreateRequest,
    response: Response,
    db: Session = Depends(get_db_session),
) -> TemplateResponse:
    template = Template(
        notification_type=request.notification_type,
        channel=request.channel,
        subject=request.subject,
        body=request.body,
        is_active=request.is_active,
    )
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An active template for ({request.notification_type!r}, "
                f"{request.channel.value!r}) already exists."
            ),
        )
    db.refresh(template)
    attach_etag(response, template.version_id)
    return _to_response(template)


@router.get("", response_model=list[TemplateResponse])
def list_templates(
    notification_type: str | None = Query(default=None, max_length=128),
    channel: ChannelType | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db_session),
) -> list[TemplateResponse]:
    stmt = select(Template).order_by(Template.id.desc())
    if notification_type is not None:
        stmt = stmt.where(Template.notification_type == notification_type)
    if channel is not None:
        stmt = stmt.where(Template.channel == channel)
    if is_active is not None:
        stmt = stmt.where(Template.is_active.is_(is_active))
    return [_to_response(t) for t in db.scalars(stmt).all()]


@router.get("/{template_id}", response_model=TemplateResponse)
def get_template(
    template_id: int,
    response: Response,
    db: Session = Depends(get_db_session),
) -> TemplateResponse:
    template = db.get(Template, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    attach_etag(response, template.version_id)
    return _to_response(template)


@router.patch("/{template_id}", response_model=TemplateResponse)
def update_template(
    template_id: int,
    request: TemplateUpdateRequest,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db_session),
) -> TemplateResponse:
    """Patch a template. Requires `If-Match` to prevent racing operators."""
    template = db.get(Template, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required to update this resource.",
        )
    if if_match.strip().strip('"') != _IF_MATCH_WILDCARD:
        check_version_or_412(template.version_id, parse_if_match(if_match))

    if request.subject is not None:
        template.subject = request.subject
    if request.body is not None:
        template.body = request.body
    if request.is_active is not None:
        template.is_active = request.is_active

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Activating this template would conflict with another active "
                   "template for the same (type, channel).",
        )
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Template was modified concurrently; reload and retry.",
        ) from exc
    db.refresh(template)
    attach_etag(response, template.version_id)
    return _to_response(template)


@router.delete(
    "/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_template(
    template_id: int,
    if_match: str | None = Header(default=None, alias="If-Match"),
    db: Session = Depends(get_db_session),
) -> Response:
    template = db.get(Template, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required to delete this resource.",
        )
    if if_match.strip().strip('"') != _IF_MATCH_WILDCARD:
        check_version_or_412(template.version_id, parse_if_match(if_match))
    db.delete(template)
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail="Template was modified concurrently; reload and retry.",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Internal serialization helper
# ---------------------------------------------------------------------------

def _to_response(template: Template) -> TemplateResponse:
    return TemplateResponse(
        id=template.id,
        notification_type=template.notification_type,
        channel=template.channel,
        subject=template.subject,
        body=template.body,
        is_active=template.is_active,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )
