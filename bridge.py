"""Button-click -> ``resolve_integration`` bridge (Phase 6).

What this is, honestly: the plugin posts approve/edit/discard buttons via
``GatewayAdapter.post_buttons`` (Discord renders them through the Hermes
gateway). For a click to *reach* the plugin, the host gateway must deliver
the interaction to the plugin as a ``gateway_platform_event`` hook call.

Verified against the Hermes source in this checkout (Hermes Agent v0.22.7,
``gateway/run_adapters.py::_handle_gateway_platform_event`` and
``website/docs/user-guide/features/hooks.md#gateway_platform_event``):
the ``gateway_platform_event`` hook fires ONLY for the normalized per-event
contracts — ``reaction`` (telegram), ``message_edited``, ``message_deleted``,
``thread_created``, ``thread_renamed``. **Discord button interactions
(component clicks / ``custom_id`` payloads) are NOT part of the normalized
contract in this Hermes version** — the raw-SDK capability
(``gateway.raw_events``, tracked upstream as #64228) does not exist yet.
So no plugin-side code can receive a real Discord button click from the
host today; claiming otherwise would be a lie.

What IS built here (the narrowest correct seam):

1. :func:`parse_button_event` — a strict parser for the button *value*
   strings the plugin itself posts (``approve:<ticket_id>`` etc.). It
   accepts the shapes a host could plausibly deliver: a component-style
   payload (``custom_id``/``value`` + ``user_id``/``author_id``), a
   message-text payload (``text`` containing the button value), or a bare
   value string. Anything else returns ``None`` (ignored, fail closed).

2. :func:`dispatch_button_event` — matches the parsed decision to its
   ticket, resolves the clicking author to a pool ``key_id`` through a
   host-supplied identity map (``{platform_user_id: key_id}`` — the plugin
   has no way to know Discord user ids), checks the clicker IS the pool
   maintainer, and dispatches to ``PoolService.resolve_integration``.
   Unresolvable or non-maintainer clickers are refused — never dispatched.

3. :func:`make_platform_event_handler` — the ``gateway_platform_event``
   hook handler registered by ``register()``. It runs the parser over
   every hook payload (today: only the normalized events above, which
   parse to ``None`` and are ignored — zero behaviour change) and, if a
   future Hermes version normalizes button interactions into this hook,
   the bridge lights up with no plugin change. It never raises (hook
   contract: observer failures must not break the gateway).

HOST CONTRACT (what the host must do — in this Hermes version, a human or
the operator drives the decision through the plugin's own slash command):

    /integrate approve <ticket_id>
    /integrate edit <ticket_id> --edit-file PATH --edit-content TEXT
    /integrate discard <ticket_id> --reason "..."

which calls exactly ``PoolService.resolve_integration`` with the same
maintainer check. The buttons in Discord are a visual prompt; the command
is the actionable control until a raw-interaction seam ships. If/when the
host can deliver interactions, it must call::

    dispatch_button_event(service, platform, event_type, payload,
                          identity_map={discord_user_id: agent_pool_key_id})

with the clicking user's Discord id, and the maintainer check + dispatch
above is the single code path.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

DECISIONS = ("approve", "edit", "discard")
_BUTTON_RE = re.compile(r"^(approve|edit|discard):(t-[A-Za-z0-9_-]+)$")
# Button values are embedded in prompt text like "approve:t-a1b2c3d4".
_TEXT_RE = re.compile(r"\b(approve|edit|discard):(t-[A-Za-z0-9_-]+)\b")


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
    The clicker must resolve to the ticket's pool maintainer; anything
    else is refused with ``PermissionError``. ``resolve_kwargs`` (e.g.
    ``work_dir``, ``edit_files``) are forwarded to ``resolve_integration``.
    """
    parsed = parse_button_event(payload)
    if parsed is None:
        return {"ok": False, "dispatched": False,
                "reason": "not an agent-pool button event"}
    ticket_id, decision = parsed["ticket_id"], parsed["decision"]
    ticket = service.show_ticket(ticket_id)
    pool_id = ticket["pool_id"]
    maintainer = None
    for pool in service._conn.execute(
            "SELECT maintainer_key_id FROM pools WHERE pool_id=?", (pool_id,)):
        maintainer = pool[0]
    clicker_key = None
    if isinstance(payload, dict):
        for author_id in _author_ids(payload):
            if identity_map and author_id in identity_map:
                clicker_key = identity_map[author_id]
                break
    if clicker_key is None:
        raise PermissionError(
            "button click refused: clicking author could not be resolved "
            "to a pool key_id (host must supply identity_map)")
    if maintainer is not None and clicker_key != maintainer:
        raise PermissionError(
            "button click refused: only the pool maintainer may decide "
            f"integration (clicker {clicker_key!r} is not the maintainer)")
    result = service.resolve_integration(
        ticket_id, decision, key_id=clicker_key, **resolve_kwargs)
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
