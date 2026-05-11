"""
ETag / If-Match helpers for optimistic concurrency control.

Every mutable resource (UserPreferences, Template) carries a `version_id`
integer column managed by SQLAlchemy. We expose it to clients as an HTTP
`ETag` and require a matching `If-Match` header on PUT/PATCH/DELETE.

Mismatch -> 412 Precondition Failed (per RFC 9110 §13.1.1).
Missing  -> 428 Precondition Required (per RFC 6585 §3) so callers cannot
           accidentally perform a blind write.

The ETag value is a strong validator (no `W/` prefix): it is derived from
a server-generated monotonic integer and changes only on a real mutation.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Response, status

# Public so routes can build the value: `_format_etag(prefs.version_id)`.
def format_etag(version_id: int) -> str:
    """Render a version integer as a strong ETag value."""
    return f'"{version_id}"'


def parse_if_match(if_match_header: str) -> int:
    """
    Parse an `If-Match` header value into an integer version.

    Accepts both quoted (`"7"`) and unquoted (`7`) forms; rejects weak
    validators (`W/"7"`) because we cannot prove semantic equivalence
    across server versions.
    """
    raw = if_match_header.strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match header is required for this operation.",
        )
    if raw.startswith("W/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Weak ETags are not accepted; use a strong validator.",
        )
    raw = raw.strip('"')
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed If-Match header: {if_match_header!r}",
        ) from exc


def require_if_match(
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> int:
    """
    FastAPI dependency: extracts and validates the If-Match header.

    Returns the parsed integer version. Raises 428 if missing.
    """
    if if_match is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail=(
                "If-Match header is required to mutate this resource. "
                "GET the resource first and quote its ETag."
            ),
        )
    return parse_if_match(if_match)


def check_version_or_412(current: int, expected: int) -> None:
    """Raise 412 unless the current row version matches the client's expectation."""
    if current != expected:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=(
                f"ETag mismatch: resource is at version {current}, "
                f"client supplied If-Match for version {expected}."
            ),
        )


def attach_etag(response: Response, version_id: int) -> None:
    """Attach the strong ETag header to a response."""
    response.headers["ETag"] = format_etag(version_id)
