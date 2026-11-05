"""GatewayAdapter: the plugin's ONLY path to a chat platform.

The plugin never calls a platform API (Discord/Telegram/Slack) directly.
It talks to this interface; the Hermes gateway host supplies the transport
(e.g. the Discord adapter's forum-post capability). Discord impl first;
Telegram/Slack are later subclasses over the same interface.

Coordination payloads are small signed JSON (< 4 KB) — repo bytes never
travel this path (they go over Iroh via the transport plugin).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

MAX_COORDINATION_BYTES = 4096


@dataclass
class GatewayRef:
    ref: str
    platform: str = "fake"


@dataclass
class GatewayCall:
    action: str
    payload: dict


class GatewayAdapter(ABC):
    """Gateway-independent coordination surface."""

    platform = "base"

    @abstractmethod
    def post_manifest(self, pool_id: str, manifest: dict) -> GatewayRef:
        """Post a signed repo manifest to the pool's coordination channel."""

    @abstractmethod
    def post_notice(self, pool_id: str, text: str, kind: str = "notice") -> GatewayRef:
        """Post a short plain notice (no repo bytes)."""

    @abstractmethod
    def open_ticket_thread(self, pool_id: str, ticket: dict) -> GatewayRef:
        """Open a forum/thread view for a ticket; returns its ref."""

    @abstractmethod
    def set_ticket_state(self, ticket_ref: str, state: str) -> GatewayRef:
        """Update a ticket thread's state (tag/label) in the gateway."""

    @abstractmethod
    def post_buttons(self, ticket_ref: str, prompt: str, buttons) -> GatewayRef:
        """Post an approve/edit/discard-style button prompt on a thread."""


def _check_coordination_size(payload: dict) -> None:
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_COORDINATION_BYTES:
        raise ValueError(
            f"coordination payload {size} bytes exceeds {MAX_COORDINATION_BYTES} byte limit")


class FakeGatewayAdapter(GatewayAdapter):
    """In-memory gateway for tests and offline/dry-run flows."""

    platform = "fake"

    def __init__(self):
        self.calls: list = []
        self._seq = 0

    def _record(self, action: str, payload: dict) -> GatewayRef:
        _check_coordination_size(payload)
        self._seq += 1
        self.calls.append(GatewayCall(action=action, payload=payload))
        return GatewayRef(ref=f"fake:{action}:{self._seq}", platform=self.platform)

    def post_manifest(self, pool_id, manifest):
        return self._record("post_manifest", {"pool_id": pool_id, "manifest": manifest})

    def post_notice(self, pool_id, text, kind="notice"):
        return self._record("post_notice", {"pool_id": pool_id, "kind": kind, "text": text})

    def open_ticket_thread(self, pool_id, ticket):
        return self._record("open_ticket_thread", {"pool_id": pool_id, "ticket": ticket})

    def set_ticket_state(self, ticket_ref, state):
        return self._record("set_ticket_state", {"ticket_ref": ticket_ref, "state": state})

    def post_buttons(self, ticket_ref, prompt, buttons):
        return self._record("post_buttons",
                            {"ticket_ref": ticket_ref, "prompt": prompt, "buttons": list(buttons)})


class DiscordGatewayAdapter(GatewayAdapter):
    """Discord via the Hermes gateway host — NOT a direct Discord API client.

    ``send`` is supplied by the host and routes to the gateway's Discord
    adapter (e.g. its forum-post capability). Signature:
    ``send(action: str, payload: dict) -> str`` returning a message/thread ref.
    Phase 0 spike B showed the core adapter's forum helper is private, so the
    host must expose it; this class deliberately imports no ``discord`` module.
    """

    platform = "discord"

    def __init__(self, send):
        if not callable(send):
            raise TypeError("DiscordGatewayAdapter requires a callable gateway send()")
        self._send = send

    def _dispatch(self, action: str, payload: dict) -> GatewayRef:
        _check_coordination_size(payload)
        ref = self._send(action, payload)
        return GatewayRef(ref=str(ref), platform=self.platform)

    def post_manifest(self, pool_id, manifest):
        return self._dispatch("post_manifest", {"pool_id": pool_id, "manifest": manifest})

    def post_notice(self, pool_id, text, kind="notice"):
        return self._dispatch("post_notice", {"pool_id": pool_id, "kind": kind, "text": text})

    def open_ticket_thread(self, pool_id, ticket):
        return self._dispatch("open_ticket_thread", {"pool_id": pool_id, "ticket": ticket})

    def set_ticket_state(self, ticket_ref, state):
        return self._dispatch("set_ticket_state", {"ticket_ref": ticket_ref, "state": state})

    def post_buttons(self, ticket_ref, prompt, buttons):
        return self._dispatch("post_buttons",
                              {"ticket_ref": ticket_ref, "prompt": prompt, "buttons": list(buttons)})
