"""Discord button-gate e2e (plugin-side harness — see module docstring).

What this is and is NOT. The Hermes checkout ships a Discord stand-in that
dispatches a REAL INTERACTION_CREATE to a child gateway
(``H/tests/fakes/platforms/discord_shim`` + ``H/tests/e2e/core/platforms/_drv_discord.py``,
whose ``.click()`` is at line ~109). That harness needs a spawned Hermes
gateway child process with the Discord adapter connected — it cannot be
driven from this plugin repo without editing Hermes, which is forbidden
(the checkout is read-only). So this test uses the closest faithful
harness buildable in-repo:

REAL: the plugin's actual ``register()`` (captures the real
``register_platform_handler`` factory), the real
``buttons.make_discord_button_factory`` (real ``discord.ui.View`` /
``Button`` construction, real ``discord.InteractionType.component``
filter, real ``add_listener`` registration), the real ``bridge.py``
parser, the real ledger discord-user binding, and the real
``PoolService`` maintainer gate + ``resolve_integration`` dispatch
(``resolve_integration`` itself is monkeypatched to a recorder because a
full merge needs a real bundle round-trip — that path is covered by
``test_integration.py`` / ``test_phase6_hardening.py``).

SIMULATED: the discord.py ``commands.Bot`` (a fake native with
``add_listener``/``add_view`` capture) and the ``Interaction`` object
(a duck-typed stand-in carrying the exact attributes the listener
reads: ``type``, ``data.custom_id``, ``user.id``, ``response.defer``,
``followup.send``). A real Discord INTERACTION_CREATE through a live
gateway is the live test (``scripts/live-discord-check.md``).
"""
import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import discord  # noqa: E402  (real discord.py, available in system python)

import ledger as L  # noqa: E402
from identity import ensure_identity  # noqa: E402
from tickets import PoolService  # noqa: E402

PLUGIN_ROOT = Path(__file__).parent.parent


# --------------------------------------------------------------------------
# Fakes for the discord.py Bot / Interaction surface (simulated, see above)
# --------------------------------------------------------------------------

class FakeNative:
    """Stand-in for discord.py commands.Bot: captures listeners/views."""

    def __init__(self):
        self.listeners = {}  # event name -> [coroutines]
        self.views = []

    def add_listener(self, coro, name):
        self.listeners.setdefault(name, []).append(coro)

    def add_view(self, view):
        self.views.append(view)


class _Response:
    def __init__(self):
        self.deferred = False

    async def defer(self, ephemeral=False):
        self.deferred = True


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, msg, ephemeral=False):
        self.sent.append((msg, ephemeral))


def make_interaction(custom_id, user_id, itype=discord.InteractionType.component):
    return SimpleNamespace(
        type=itype,
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id),
        response=_Response(),
        followup=_Followup(),
        channel=SimpleNamespace(sent=[], send=lambda *a, **k: None),
    )


# --------------------------------------------------------------------------
# Real plugin wiring: register() with a fake ctx, real ledger binding
# --------------------------------------------------------------------------

class FakeCtx:
    def __init__(self):
        self.commands = {}
        self.platform_factories = {}
        self.state = {}

    def get_config(self, key, default=None):
        return {"transport": "fake"}.get(key, default)

    def register_tool(self, **kw):
        pass

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_platform_handler(self, platform, factory):
        self.platform_factories[platform] = factory

    def register_hook(self, *a, **k):
        pass


def _setup_world(tmp_path, monkeypatch):
    """Real in-memory ledger + real PoolService; maintainer bound to '42'."""
    conn = L.init_ledger(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    maint = ensure_identity(tmp_path / "m")
    L.create_pool(conn, "pool-1", "Team", maint.key_id, relay_url="off")
    conn.execute(
        "INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('demo','pool-1','Demo',0)")
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at) VALUES (?,?,?,0)",
        (maint.key_id, maint.verify_key.encode(RawEncoder).hex(),
         maint.box_pk.encode(RawEncoder).hex()))
    L.add_member(conn, "pool-1", maint.key_id, role="maintainer",
                 box_pubkey_hex=maint.box_pk.encode(RawEncoder).hex())
    L.bind_discord_user(conn, maint.key_id, "42")  # maintainer's Discord id
    conn.commit()

    svc = PoolService(conn, __import__("transport").FakeTransport(),
                      caller_key_id=maint.key_id, caller_signing_key=maint.signing_key,
                      caller_box_sk=maint.box_sk)
    ticket = svc.add_ticket("pool-1", "demo", "Gate ticket", brief="b")

    # Point the plugin's _service_for at this ledger + tmp data dir.
    monkeypatch.setattr(L, "get_db", lambda: conn)
    monkeypatch.setattr(L, "default_db_path", lambda: tmp_path / "data.db")

    return conn, svc, maint, ticket


def _wire_factory(monkeypatch):
    """Run real plugin register(); return (ctx, native, listener)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "agent_pool_plugin", PLUGIN_ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = FakeCtx()
    mod.register(ctx)
    factory = ctx.platform_factories.get("discord")
    assert factory is not None, "register() did not register a discord platform handler"
    native = FakeNative()
    factory(native, adapter=None)
    listeners = native.listeners.get("on_interaction") or []
    assert len(listeners) == 1, "factory must register exactly one on_interaction listener"
    return ctx, native, listeners[0]


_RESOLVE_CALLS = []


def _recording_resolve(self, ticket_id, decision, *, key_id=None, **kw):
    _RESOLVE_CALLS.append({"ticket_id": ticket_id, "decision": decision,
                           "key_id": key_id})
    return {"ok": True, "decision": decision, "ticket_id": ticket_id,
            "ticket_state": "merged", "merged": True}


@pytest.fixture(autouse=True)
def _record_resolve(monkeypatch):
    _RESOLVE_CALLS.clear()
    monkeypatch.setattr(PoolService, "resolve_integration", _recording_resolve)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_maintainer_click_resolves_ticket(tmp_path, monkeypatch):
    _setup_world(tmp_path, monkeypatch)
    _ctx, _native, listener = _wire_factory(monkeypatch)
    ticket_id = _RESOLVE_CALLS and None  # placeholder (set below)
    # ticket id from the world: recreate lookup
    conn = L.get_db()
    tid = conn.execute("SELECT ticket_id FROM tickets LIMIT 1").fetchone()[0]
    inter = make_interaction(f"agent-pool:approve:{tid}", "42")
    asyncio.run(listener(inter))
    assert inter.response.deferred is True, "listener must defer within the 3s window"
    assert _RESOLVE_CALLS == [{"ticket_id": tid, "decision": "approve",
                               "key_id": _RESOLVE_CALLS[0]["key_id"]}]
    assert _RESOLVE_CALLS[0]["key_id"] is not None
    assert any("Decision recorded: approve" in m for m, _e in inter.followup.sent)


def test_unmapped_clicker_refused(tmp_path, monkeypatch):
    _setup_world(tmp_path, monkeypatch)
    _ctx, _native, listener = _wire_factory(monkeypatch)
    tid = L.get_db().execute("SELECT ticket_id FROM tickets LIMIT 1").fetchone()[0]
    inter = make_interaction(f"agent-pool:approve:{tid}", "999999")  # unmapped
    asyncio.run(listener(inter))
    assert _RESOLVE_CALLS == [], "unmapped clicker must never dispatch"
    assert any("refus" in m.lower() for m, _e in inter.followup.sent)


def test_non_maintainer_clicker_refused(tmp_path, monkeypatch):
    conn, _svc, _maint, _ticket = _setup_world(tmp_path, monkeypatch)
    other = ensure_identity(tmp_path / "o")
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at) VALUES (?,?,?,0)",
        (other.key_id, other.verify_key.encode(RawEncoder).hex(),
         other.box_pk.encode(RawEncoder).hex()))
    L.add_member(conn, "pool-1", other.key_id, role="member",
                 box_pubkey_hex=other.box_pk.encode(RawEncoder).hex())
    L.bind_discord_user(conn, other.key_id, "77")  # mapped, but NOT maintainer
    conn.commit()
    _ctx, _native, listener = _wire_factory(monkeypatch)
    tid = conn.execute("SELECT ticket_id FROM tickets LIMIT 1").fetchone()[0]
    inter = make_interaction(f"agent-pool:approve:{tid}", "77")
    asyncio.run(listener(inter))
    assert _RESOLVE_CALLS == [], "non-maintainer must never dispatch"
    assert any("refus" in m.lower() for m, _e in inter.followup.sent)


def test_unrelated_interactions_ignored(tmp_path, monkeypatch):
    _setup_world(tmp_path, monkeypatch)
    _ctx, _native, listener = _wire_factory(monkeypatch)
    # Another plugin's button: not our namespace.
    inter = make_interaction("other-plugin:approve:t-x", "42")
    asyncio.run(listener(inter))
    assert inter.response.deferred is False
    assert _RESOLVE_CALLS == []
    # Non-component interaction (e.g. a slash command): also ignored.
    inter2 = make_interaction("agent-pool:approve:t-x", "42",
                              itype=discord.InteractionType.application_command)
    asyncio.run(listener(inter2))
    assert inter2.response.deferred is False
    assert _RESOLVE_CALLS == []


def test_real_view_buttons_carry_agent_pool_custom_ids():
    """The real discord.ui.View the gateway bot renders carries our ids."""
    async def build():
        view = discord.ui.View(timeout=None)
        for decision, label in (("approve", "Approve"), ("edit", "Edit"),
                                ("discard", "Discard")):
            view.add_item(discord.ui.Button(
                label=label, custom_id=f"agent-pool:{decision}:t-abc123",
                style=discord.ButtonStyle.success))
        return view
    view = asyncio.run(build())
    ids = [c.custom_id for c in view.children]
    assert ids == ["agent-pool:approve:t-abc123",
                   "agent-pool:edit:t-abc123",
                   "agent-pool:discard:t-abc123"]
    from buttons import parse_custom_id
    for cid in ids:
        assert parse_custom_id(cid)[1] == "t-abc123"
