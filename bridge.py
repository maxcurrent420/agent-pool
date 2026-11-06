"""Button-click -> ``resolve_integration`` bridge (Phase 6).

Gateway-native (spec §4): the plugin does NOT post buttons itself. The
Hermes Discord gateway bot owns the message. The approve/edit/discard gate
is delivered by two paths that both land on the Hermes bot:

(a) ``buttons.py`` registers ``ctx.register_platform_handler("discord", factory)``
    — the gateway's own discord.py bot gets a listener + ``discord.ui.View``
    with ``custom_id = agent-pool:{decision}:{ticket_id}``. Button clicks are
    ``Component`` interactions routed by Hermes's bot to this listener.
(b) ``ctx.ask_choice`` inside ``/integrate`` — Hermes renders clarify
    buttons in the invoking chat and routes the tap back.

This module is the shared parsing/dispatch core both paths use:
``parse_button_event`` (custom_id / value / text shapes, ``agent-pool:``
prefix optional) and ``dispatch_button_event`` (clicker → owner check via
the whitelisted Discord id, then dispatch to
``PoolService.resolve_integration`` with that id as ``source``). Unmapped or
non-owner clickers are refused, never dispatched.

The ``gateway_platform_event`` hook (registered for compatibility) only
carries normalized events in Hermes v0.22.7 — Discord button interactions
are NOT delivered to it — so it parses to ``None`` and is behaviour-neutral
today. Details: ``buttons.py`` factory + spec §4.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

DECISIONS = ("approve", "edit", "discard")
_BUTTON_RE = re.compile(r"^(?:agent-pool:)?(approve|edit|discard):(t-[A-Za-z0-9_-]+)$")
# Button values are embedded in prompt text like "approve:t-a1b2c3d4".
_TEXT_RE = re.compile(r"\b(?:agent-pool:)?(approve|edit|discard):(t-[A-Za-z0-9_-]+)\b")


def _strip_prefix(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("agent-pool:"):
        raw = raw[len("agent-pool:"):]
    return raw


def parse_button_event(payload) -> dict | None:
    """Parse a platform-event payload into ``{decision, ticket_id}``.

    Returns ``None`` for anything that is not an agent-pool button click
    (including every currently-normalized ``gateway_platform_event``).
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        match = _BUTTON_RE.match(payload.strip())
        return {"decision": match.group(1), "ticket_id": match.group(2)} if match else None
    if not isinstance(payload, dict):
        return None
    # Component-style payloads: custom_id / value / data.custom_id.
    for key in ("custom_id", "value", "customId"):
        raw = payload.get(key)
        if isinstance(raw, str):
            match = _BUTTON_RE.match(raw.strip())
            if match:
                return {"decision": match.group(1), "ticket_id": match.group(2)}
    data = payload.get("data")
    if isinstance(data, dict):
        parsed = parse_button_event(data)
        if parsed:
            return parsed
    # Message-text payloads (message_edited carries `text` today).
    text = payload.get("text")
    if isinstance(text, str):
        match = _TEXT_RE.search(text)
        if match:
            return {"decision": match.group(1), "ticket_id": match.group(2)}
    return None


def _author_ids(payload: dict) -> list[str]:
    ids = []
    for key in ("user_id", "author_id", "userId", "authorId", "actor_id"):
        value = payload.get(key)
        if value is not None and str(value) != "":
            ids.append(str(value))
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("id", "user_id"):
            if user.get(key) is not None:
                ids.append(str(user[key]))
    author = payload.get("author")
    if isinstance(author, dict) and author.get("id") is not None:
        ids.append(str(author["id"]))
    return ids


def dispatch_button_event(service, platform, event_type, payload, *,
                          identity_map=None, **resolve_kwargs) -> dict:
    """Route a parsed button click to ``service.resolve_integration``.

    ``identity_map`` maps platform user ids to agent-pool ``key_id``s and
    is supplied by the host (the plugin cannot know platform user ids).
    Authorization is the whitelisted Discord user id only: the clicker's
    id must be the pool owner (``PoolService._require_owner``), and that
    id is forwarded as ``source``. ``identity_map`` is attribution only —
    crypto records WHO acted, it never decides access. Anything else is
    refused with ``PermissionError``. ``resolve_kwargs`` (e.g.
    ``work_dir``, ``edit_files``) are forwarded to ``resolve_integration``.
    """
    parsed = parse_button_event(payload)
    if parsed is None:
        return {"ok": False, "dispatched": False,
                "reason": "not an agent-pool button event"}
    ticket_id, decision = parsed["ticket_id"], parsed["decision"]
    ticket = service.show_ticket(ticket_id)
    pool_id = ticket["pool_id"]
    clicker_discord_id = None
    if isinstance(payload, dict):
        for author_id in _author_ids(payload):
            if str(author_id).strip():
                clicker_discord_id = str(author_id)
                break
    if clicker_discord_id is None:
        raise PermissionError(
            "button click refused: clicking author could not be resolved "
            "to a Discord user id (host must supply identity_map)")
    # Owner check is the whitelist's: the Discord id must be the pool
    # owner. Raises PermissionError otherwise (fail closed, unmapped too).
    service._require_owner(pool_id, {"user_id": clicker_discord_id})
    # Attribution only: invite-bound key for the event record.
    clicker_key = (identity_map or {}).get(clicker_discord_id)
    result = service.resolve_integration(
        ticket_id, decision, source={"user_id": clicker_discord_id},
        key_id=clicker_key, **resolve_kwargs)
    result = dict(result)
    result.update({"dispatched": True, "via": "button_bridge",
                   "platform": platform, "event_type": event_type,
                   "clicker_key_id": clicker_key})
    return result


def make_platform_event_handler(service_factory, identity_map=None, **resolve_kwargs):
    """Build the ``gateway_platform_event`` hook handler.

    ``service_factory`` is a zero-arg callable returning a ``PoolService``
    (built lazily per event — the hook must never raise and must not hold
    a stale service). Observer contract: always returns ``None``, never
    raises; refusals are logged, not propagated.
    """
    def _on_gateway_platform_event(platform, event_type, payload, **kwargs):
        try:
            parsed = parse_button_event(payload)
            if parsed is None:
                return None
            service = service_factory()
            dispatch_button_event(
                service, platform, event_type, payload,
                identity_map=identity_map, **resolve_kwargs)
        except Exception as exc:  # observer: log, never break the gateway
            logger.warning("agent-pool button bridge: event not dispatched: %s", exc)
        return None

    return _on_gateway_platform_event
