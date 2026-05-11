"""
Test 4 — Template rendering (PRD §7.5 + §3.3).

Covers:
  - `{{user.name}}` substitution against nested dicts.
  - StrictUndefined: missing variables become a TemplateRenderError.
  - HTML autoescaping for the html-body path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.templates import (
    TemplateRenderError,
    render_html,
    render_template,
)


def _template(*, subject: str | None, body: str) -> object:
    """Lightweight stand-in for `app.models.Template` (only attrs used)."""
    return SimpleNamespace(subject=subject, body=body)


def test_substitutes_nested_variables() -> None:
    rendered = render_template(
        _template(subject="Welcome {{user.name}}", body="Hi {{user.name}}!"),
        {"user": {"name": "Ada"}},
    )
    assert rendered.subject == "Welcome Ada"
    assert rendered.body == "Hi Ada!"


def test_missing_variable_raises_render_error() -> None:
    with pytest.raises(TemplateRenderError):
        render_template(
            _template(subject=None, body="Hello {{user.name}}"),
            {"user": {}},  # `name` missing
        )


def test_html_body_escapes_user_input() -> None:
    """HTML path must autoescape `<script>` tags in user-supplied vars."""
    output = render_html("<p>Hi {{name}}</p>", {"name": "<script>alert(1)</script>"})
    assert "<script>" not in output
    assert "&lt;script&gt;" in output


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_ssti_attempt_raises_render_error() -> None:
    """
    A Server-Side Template Injection attack using Jinja2 attribute traversal
    (e.g. `{{ ''.__class__ }}`) must be blocked by the SandboxedEnvironment
    and surface as TemplateRenderError — never as a raw exception that leaks
    interpreter internals, and certainly never as a successful render.
    """
    with pytest.raises(TemplateRenderError):
        render_template(
            _template(subject=None, body="{{ ''.__class__.__mro__ }}"),
            {},
        )


def test_template_syntax_error_raises_render_error() -> None:
    """
    Malformed Jinja2 syntax (unclosed block) must raise TemplateRenderError,
    not propagate a raw Jinja2 exception to the caller.
    """
    with pytest.raises(TemplateRenderError):
        render_template(
            _template(subject=None, body="{% for x in %}"),  # invalid Jinja2
            {},
        )


def test_text_path_does_not_escape_html() -> None:
    """
    The plain-text render path (render_template) must NOT escape HTML entities.
    Autoescape is OFF for text bodies — only the html path (render_html) escapes.
    Escaping text bodies would corrupt plain-text email clients.
    """
    raw_html = "<b>hello</b>"
    rendered = render_template(
        _template(subject=None, body="{{tag}}"),
        {"tag": raw_html},
    )
    assert rendered.body == raw_html  # passed through verbatim
    assert "&lt;" not in rendered.body


def test_numeric_variable_is_rendered_as_string() -> None:
    """Integer/float variables should coerce to their string representation."""
    rendered = render_template(
        _template(subject="Order #{{order_id}}", body="Total: {{amount}}"),
        {"order_id": 42, "amount": 9.99},
    )
    assert rendered.subject == "Order #42"
    assert rendered.body == "Total: 9.99"


def test_deeply_nested_variable_access() -> None:
    """Three levels of nesting — {{a.b.c}} — must resolve without error."""
    rendered = render_template(
        _template(subject=None, body="{{order.shipping.city}}"),
        {"order": {"shipping": {"city": "Tel Aviv"}}},
    )
    assert rendered.body == "Tel Aviv"


def test_missing_outer_variable_key_raises_render_error() -> None:
    """
    When the top-level variable is completely absent from the context
    (not just a missing nested key) StrictUndefined must still raise.
    """
    with pytest.raises(TemplateRenderError):
        render_template(
            _template(subject=None, body="Hello {{user.name}}"),
            {},  # `user` key is entirely missing
        )
