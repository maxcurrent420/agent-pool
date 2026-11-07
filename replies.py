"""Content formatting: Hermes posts, plugin only returns strings.

The plugin never posts to Discord itself. Every slash-command handler returns
a markdown string produced by format_result(); the Hermes gateway delivers
it as its reply. This module is format-only: no adapter classes, no
``discord`` import, no platform client.
"""
from __future__ import annotations

import json

MAX_CONTENT_BYTES = 4096
# Back-compat name (old gateway.py guard).
MAX_COORDINATION_BYTES = MAX_CONTENT_BYTES


def check_content_size(payload) -> None:
    """Raise ValueError if the JSON encoding exceeds the 4 KB content guard.

    Kept as a content guard (coordination payloads stay small, repo bytes
    never flow through the gateway), not a transport guard.
    """
    if isinstance(payload, str):
        size = len(payload.encode("utf-8"))
    else:
        size = len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise ValueError(
            f"coordination payload {size} bytes exceeds {MAX_CONTENT_BYTES} byte limit")


def check_content_text(text: str) -> None:
    check_content_size(text)


def ticket_brief(ticket: dict) -> str:
    """One-line ticket brief: state + id + title."""
    state = ticket.get("state", "?")
    tid = ticket.get("ticket_id", "?")
    title = ticket.get("title", "")
    pool = ticket.get("pool_id", "")
    repo = ticket.get("repo_id", "")
    parts = [f"[state: {state}] {tid}: {title}"]
    if pool:
        parts[0] += f" (pool={pool}"
        if repo:
            parts[0] += f", repo={repo}"
        parts[0] += ")"
    brief = ticket.get("brief", "")
    if brief:
        parts.append(f"Brief: {brief[:500]}")
    return "\n".join(parts)


def format_result(payload) -> str:
    """Format a service result dict as the string Hermes posts.

    The command contract is a plain string (Discord renders markdown
    client-side). We emit indented JSON inside a plain string — the
    simplest content the gateway can post verbatim — with the ``notice``
    already folded in as a JSON field (D5). Content guard enforced.
    """
    if isinstance(payload, str):
        return payload
    if payload is None:
        return ""
    out = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    if len(out.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError("reply exceeds 4096-byte gateway limit")
    return out
