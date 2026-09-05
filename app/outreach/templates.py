"""Load and render outreach templates.

Rendering is deliberately forgiving: a missing variable becomes a visible
`[[placeholder]]` marker rather than an exception, and any draft still holding
a `[...]` marker is flagged so it cannot be sent as-is.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import settings

log = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"\{([a-z0-9_]+)\}", re.I)
# Square-bracket instructions the human is meant to replace before sending
UNFILLED_RE = re.compile(r"\[[^\]]{4,}\]")


class TemplateError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _load(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        raise TemplateError(f"outreach templates not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load(reload: bool = False) -> dict:
    if reload:
        _load.cache_clear()
    return _load(str(settings.config_dir / "outreach_templates.yaml"))


def template_names() -> list[str]:
    return sorted((load().get("templates") or {}).keys())


def context_for(project: str | None = None, **extra) -> dict:
    """defaults <- project overrides <- per-prospect values."""
    data = load()
    ctx = dict(data.get("defaults") or {})
    if project:
        ctx.update((data.get("projects") or {}).get(project) or {})
    ctx.update({k: v for k, v in extra.items() if v is not None})
    ctx.setdefault("name_suffix", "")
    return ctx


def render(name: str, ctx: dict) -> tuple[str, str, list[str]]:
    """Render a template. Returns (subject, body, warnings)."""
    templates = load().get("templates") or {}
    tpl = templates.get(name)
    if not tpl:
        raise TemplateError(f"unknown template '{name}' (have: {', '.join(templates)})")
    return _render_pair(tpl, ctx)


def render_follow_up(index: int, ctx: dict) -> tuple[str, str, list[str]]:
    chain = load().get("follow_ups") or []
    if index >= len(chain):
        raise TemplateError(f"no follow-up #{index + 1} configured")
    return _render_pair(chain[index], ctx)


def _render_pair(tpl: dict, ctx: dict) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    subject = _fill(tpl.get("subject", ""), ctx, warnings)
    body = _fill(tpl.get("body", ""), ctx, warnings)

    unfilled = UNFILLED_RE.findall(body)
    if unfilled:
        warnings.append(f"{len(unfilled)} placeholder(s) still need editing")
    return subject.strip(), body.strip(), warnings


def _fill(text: str, ctx: dict, warnings: list[str]) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        # an empty string is a legitimate value (e.g. an optional name suffix);
        # only a genuinely absent variable is a warning
        if key in ctx and ctx[key] is not None:
            return str(ctx[key])
        warnings.append(f"missing variable: {key}")
        return f"[[{key}]]"

    return PLACEHOLDER_RE.sub(sub, text or "")


def needs_editing(body: str) -> bool:
    """True while a draft still contains bracketed instructions or gaps."""
    return bool(UNFILLED_RE.search(body or "")) or "[[" in (body or "")
