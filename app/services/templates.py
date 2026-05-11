"""
Template rendering service.

WHY Jinja2 over a hand-rolled regex on `{{var}}`:
  - Battle-tested escaping/sandbox guarantees.
  - Nested attribute access works out of the box (`{{user.name}}`),
    matching PRD §2.2 ("Body with variable placeholders, e.g. Hello
    {{user.name}}").
  - One dependency we already pull in via `requirements.txt`.

Sandboxing:
  - `SandboxedEnvironment` blocks access to dangerous Python attributes
    (anything starting with `_`, `__class__`, `mro`, etc.). Without this a
    malicious template author could read process memory.

Autoescape policy:
  - Bodies are NOT HTML by default → autoescape OFF for `body`.
  - Email subjects must never contain raw HTML, but they're plain text
    anyway → autoescape OFF (we don't render them as HTML).
  - HTML email bodies use a SEPARATE template render with autoescape ON
    (variables are escaped before insertion).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jinja2 import StrictUndefined
from jinja2.exceptions import TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from app.core.logging import get_logger
from app.models import Template

_logger = get_logger(__name__)


class TemplateRenderError(ValueError):
    """Raised when a template cannot be rendered with the supplied variables."""


# Two sandboxed environments — one per autoescape policy. Built once and
# reused; Jinja envs are thread-safe for rendering.
_TEXT_ENV = SandboxedEnvironment(
    autoescape=False,
    undefined=StrictUndefined,  # missing variables raise rather than render ""
    keep_trailing_newline=True,
)
_HTML_ENV = SandboxedEnvironment(
    autoescape=True,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


@dataclass(frozen=True, slots=True)
class RenderedTemplate:
    """The output of rendering a Template against a variables dict."""

    subject: str | None
    body: str
    html_body: str | None  # None when the template has no HTML variant


def render_template(template: Template, variables: dict[str, Any]) -> RenderedTemplate:
    """
    Render `template.subject` and `template.body` with the supplied vars.

    `variables` shape examples:
      {"user": {"name": "Ada"}, "order": {"id": 42}}

    Raises `TemplateRenderError` on missing variables or syntax errors —
    callers MUST catch and translate to a 4xx (this is user input).
    """
    safe_vars = variables or {}
    try:
        rendered_subject = (
            _TEXT_ENV.from_string(template.subject).render(**safe_vars)
            if template.subject
            else None
        )
        rendered_body = _TEXT_ENV.from_string(template.body).render(**safe_vars)
    except UndefinedError as exc:
        raise TemplateRenderError(f"Missing template variable: {exc}") from exc
    except TemplateError as exc:
        raise TemplateRenderError(f"Template error: {exc}") from exc

    return RenderedTemplate(
        subject=rendered_subject,
        body=rendered_body,
        html_body=None,  # HTML variant lives in a separate template row, by design
    )


def render_html(template_html_source: str, variables: dict[str, Any]) -> str:
    """
    Render an HTML body source string with autoescape ON.

    Used by the email path when an HTML template variant exists.
    Kept as a free function so non-template inline html_body strings can
    also be safely re-rendered if they contain `{{vars}}`.
    """
    try:
        return _HTML_ENV.from_string(template_html_source).render(**(variables or {}))
    except UndefinedError as exc:
        raise TemplateRenderError(f"Missing HTML template variable: {exc}") from exc
    except TemplateError as exc:
        raise TemplateRenderError(f"HTML template error: {exc}") from exc
