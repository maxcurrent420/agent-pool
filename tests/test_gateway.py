import sys

import pytest

from gateway import (
    DiscordGatewayAdapter, FakeGatewayAdapter, GatewayAdapter,
)


def _manifest():
    return {"type": "repo_manifest", "repo_id": "r", "version": 1,
            "signer_key_id": "agent:m", "signature": "ab" * 64}


def test_fake_gateway_all_five_methods():
    gw = FakeGatewayAdapter()
    assert isinstance(gw, GatewayAdapter)
    refs = [
        gw.post_manifest("pool-1", _manifest()),
        gw.post_notice("pool-1", "repo v1 accepted"),
        gw.open_ticket_thread("pool-1", {"ticket_id": "t-1", "title": "Fix"}),
        gw.set_ticket_state("fake:open_ticket_thread:3", "claimed"),
        gw.post_buttons("fake:open_ticket_thread:3", "Approve merge?", ["approve", "discard"]),
    ]
    assert [c.action for c in gw.calls] == [
        "post_manifest", "post_notice", "open_ticket_thread",
        "set_ticket_state", "post_buttons"]
    assert all(r.ref.startswith("fake:") for r in refs)
    assert gw.calls[0].payload["manifest"]["version"] == 1


def test_discord_adapter_delegates_to_gateway_send():
    sent = []

    def fake_gateway_send(action, payload):
        sent.append((action, payload))
        return f"discord-thread-{len(sent)}"

    gw = DiscordGatewayAdapter(fake_gateway_send)
    ref = gw.post_manifest("pool-1", _manifest())
    assert ref.platform == "discord" and ref.ref == "discord-thread-1"
    gw.post_notice("pool-1", "hello")
    gw.open_ticket_thread("pool-1", {"ticket_id": "t-9"})
    gw.set_ticket_state("discord-thread-3", "review")
    gw.post_buttons("discord-thread-3", "Approve?", ["approve"])
    assert [a for a, _ in sent] == [
        "post_manifest", "post_notice", "open_ticket_thread",
        "set_ticket_state", "post_buttons"]


def test_discord_adapter_imports_no_platform_module():
    assert "discord" not in sys.modules, "plugin must not import discord directly"
    source = __file__.rsplit("/tests/", 1)[0] + "/gateway.py"
    assert "import discord" not in open(source).read()


def test_coordination_payload_size_guard():
    gw = FakeGatewayAdapter()
    with pytest.raises(ValueError, match="exceeds"):
        gw.post_notice("pool-1", "x" * 5000)


def test_discord_adapter_requires_callable():
    with pytest.raises(TypeError):
        DiscordGatewayAdapter(None)
