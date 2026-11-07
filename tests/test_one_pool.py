"""Phase 6: one pool per gateway (WO-017).

Second register refused; foreign ``--pool`` refused naming the hosted
pool; ``pool_gateways`` recorded on interaction (fetch_repo_list for a
foreign pool records role=interact, then refuses); list_projects never
aggregates across hosted pools (flagged follow-up 2).
"""
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))

import ledger as L
from ledger import create_pool, init_ledger
from tickets import PoolNotLocalError, PoolService


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def world():
    c = memdb()
    create_pool(c, "pool-1", "Team", "agent:maint", owner_discord_user_id="owner-1")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('demo','pool-1','Demo',0)")
    c.commit()
    svc = PoolService(c, caller_key_id="agent:maint",
                      caller_signing_key=SigningKey.generate())
    return c, svc


def test_second_register_refused():
    _c, svc = world()
    with pytest.raises(ValueError, match="one pool per gateway"):
        svc.register_pool("pool-2", "Second", discord_user_id="owner-2")


def test_create_pool_second_refused_at_ledger():
    c, _svc = world()
    with pytest.raises(ValueError, match="one pool per gateway"):
        create_pool(c, "pool-9", "Ninth", "agent:maint")


def test_foreign_pool_refused_naming_hosted_pool():
    _c, svc = world()
    with pytest.raises(PoolNotLocalError) as exc:
        svc._resolve_pool("pool-foreign")
    assert "pool-foreign is not hosted by this gateway" in str(exc.value)
    assert "pool-1" in str(exc.value)
    assert svc._resolve_pool(None) == "pool-1"
    assert svc._resolve_pool("pool-1") == "pool-1"


def test_pool_gateways_recorded_on_interaction():
    c, svc = world()
    with pytest.raises(PoolNotLocalError):
        svc.fetch_repo_list("pool-foreign", source={"user_id": "owner-1"})
    rows = L.list_gateways(c)
    assert len(rows) == 1
    assert rows[0]["pool_id"] == "pool-foreign"
    assert rows[0]["role"] == "interact"


def test_list_projects_no_pool_uses_single_hosted_pool():
    _c, svc = world()
    projects = svc.list_projects(source={"user_id": "owner-1"})
    assert [p["repo_id"] for p in projects] == ["demo"]


def test_list_projects_refuses_multi_host_db():
    """A pre-existing multi-pool DB is refused, never aggregated."""
    c, svc = world()
    # Bypass the create_pool guard to simulate a legacy multi-pool DB.
    c.execute("INSERT INTO pools (pool_id, name, maintainer_key_id,"
              " owner_discord_user_id, created_at)"
              " VALUES ('pool-2','Legacy','agent:maint','owner-1',0)")
    c.execute("INSERT INTO repos (repo_id, pool_id, name, created_at)"
              " VALUES ('legacy','pool-2','Legacy',0)")
    c.commit()
    with pytest.raises(PoolNotLocalError):
        svc.list_projects(source={"user_id": "owner-1"})
    # An explicit pool id still resolves within the multi-host DB only
    # if it names one pool — _resolve_pool refuses multiples too.
    with pytest.raises(PoolNotLocalError):
        svc.list_projects("pool-1", source={"user_id": "owner-1"})


def test_list_projects_refuses_when_no_pool_hosted():
    c = memdb()
    svc = PoolService(c, caller_key_id="agent:maint",
                      caller_signing_key=SigningKey.generate())
    with pytest.raises(PoolNotLocalError):
        svc.list_projects(source={"user_id": "owner-1"})


# --- Phase 2: gateway owner == pool maintainer -----------------------------

def test_owner_mismatch_refused():
    """Registering with a maintainer key that is not the gateway identity
    is refused with the exact message, and no pool row is written."""
    c = memdb()
    svc = PoolService(c, caller_key_id="agent:gateway",
                      caller_signing_key=SigningKey.generate())
    with pytest.raises(ValueError) as exc:
        svc.register_pool("pool-1", "Team", key_id="agent:other",
                          discord_user_id="owner-1")
    assert str(exc.value) == "register refused: maintainer must be the gateway owner"
    assert L.list_local_pools(c) == []
    # The gateway identity itself registers fine and is recorded as the
    # owner gateway.
    out = svc.register_pool("pool-1", "Team", discord_user_id="owner-1")
    assert out["maintainer"] == "agent:gateway"
    row = c.execute("SELECT owner_gateway_key_id FROM pools WHERE pool_id='pool-1'").fetchone()
    assert row[0] == "agent:gateway"


def test_cross_gateway_interaction_unaffected():
    """An agent on a different gateway (caller key != owner gateway key)
    can still fetch/claim against the remote pool: the owner check gates
    only maintaining/hosting verbs, not fetch/claim."""
    c, svc = world()
    ticket = svc.add_ticket("pool-1", "demo", "Work", source={"user_id": "owner-1"})
    # Whitelist a remote agent user (Phase 1 whitelist, not owner).
    L.authorize_user(c, "pool-1", "remote-user")
    remote = PoolService(c, caller_key_id="agent:remote-gateway",
                         caller_signing_key=SigningKey.generate())
    fetched = remote.fetch_repo_list("pool-1", source={"user_id": "remote-user"})
    assert fetched["ok"] is True and fetched["pool_id"] == "pool-1"
    claim = remote.claim(ticket["ticket_id"], source={"user_id": "remote-user"})
    assert claim["state"] == "active"
    assert claim["key_id"] == "agent:remote-gateway"
    # But the same remote caller cannot use owner verbs.
    with pytest.raises(PermissionError):
        remote.add_ticket("pool-1", "demo", "Remote ticket",
                          source={"user_id": "owner-1"})


def test_owner_gateway_key_rotation():
    """Gateway identity rotation: owner verbs are refused with the exact
    message until the pool is re-registered; the stored column is not
    silently updated."""
    c, svc = world()
    rotated = PoolService(c, caller_key_id="agent:new-gateway",
                          caller_signing_key=SigningKey.generate())
    with pytest.raises(PermissionError) as exc:
        rotated.freeze("pool-1", True, source={"user_id": "owner-1"})
    assert str(exc.value) == ("gateway identity changed: caller is not the "
                              "pool's owner gateway — re-register the pool to continue")
    row = c.execute("SELECT owner_gateway_key_id FROM pools WHERE pool_id='pool-1'").fetchone()
    assert row[0] == "agent:maint"
    # The original gateway identity still passes the owner check.
    assert svc.freeze("pool-1", True, source={"user_id": "owner-1"})["frozen"] is True


def test_push_deliver_non_maintainer_refused(tmp_path):
    """push_deliver from a non-owner gateway is refused with the exact
    message, even with a valid grant/bundle (real end-to-end setup)."""
    import subprocess

    from identity import ensure_identity
    from ledger import add_member, sync_whitelist
    from nacl.encoding import RawEncoder
    from transport import FakeTransport

    def _insert_identity(conn, ident):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
             ident.box_pk.encode(RawEncoder).hex()))
        conn.commit()

    src = tmp_path / "src"
    src.mkdir(parents=True)
    for cmd in (["git", "-C", str(src), "init", "-q"],
                ["git", "-C", str(src), "config", "user.email", "t@t.t"],
                ["git", "-C", str(src), "config", "user.name", "T"]):
        subprocess.run(cmd, check=True)
    (src / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "v1"], check=True)

    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        _insert_identity(conn, ident)
    transport = FakeTransport()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    member_svc = PoolService(conn, transport, caller_key_id=member.key_id,
                             caller_signing_key=member.signing_key,
                             caller_box_sk=member.box_sk)
    maint_svc.register_pool("pool-1", "Team", discord_user_id="owner-1")
    sync_whitelist(conn, "pool-1", ["member-1"])
    add_member(conn, "pool-1", member.key_id, role="member",
               box_pubkey_hex=member.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=member.verify_key.encode(RawEncoder).hex())
    maint_svc.add_repo("pool-1", "demo", str(src), display_name="Demo",
                       source={"user_id": "owner-1"})
    ticket = maint_svc.add_ticket("pool-1", "demo", "Work",
                                  source={"user_id": "owner-1"})
    member_svc.claim(ticket["ticket_id"], source={"user_id": "member-1"})
    grant = member_svc.push_repo("demo", ticket["ticket_id"],
                                 source={"user_id": "member-1"})
    bundle = member_svc.push_build_bundle(str(src), "demo")
    manifest = bundle["manifest"]
    with pytest.raises(PermissionError) as exc:
        member_svc.push_deliver(manifest["transfer_ticket"], grant["token"],
                                manifest, source={"user_id": "member-1"})
    assert str(exc.value) == "push deliver refused: only the pool maintainer gateway may accept"
    # Grant not consumed by the refused deliver; maintainer can still deliver.
    out = maint_svc.push_deliver(manifest["transfer_ticket"], grant["token"],
                                 manifest, source={"user_id": "member-1"})
    assert out["ok"] is True
