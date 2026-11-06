"""Fetch replaces import (2109 Phase 2 acceptance).

``/pool fetch`` returns hosted data only; a foreign pool id is refused
naming the hosted pools; ``/work fetch`` decrypts+clones with the
requesting agent's OWN box key. There is no import, no peers, no anchors.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from ledger import create_pool, init_ledger
from tickets import PoolNotLocalError, PoolService
from transport import FakeTransport
from identity import ensure_identity

OWNER = "owner-1"


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path):
    """Hosted pool with one accepted repo version + members."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
             ident.box_pk.encode(RawEncoder).hex()))
    transport = FakeTransport()
    svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                      caller_signing_key=maint.signing_key,
                      caller_box_sk=maint.box_sk)
    from ledger import add_member
    svc.register_pool("pool-1", "Team", discord_user_id=OWNER)
    add_member(conn, "pool-1", member.key_id, role="member",
               box_pubkey_hex=member.box_pk.encode(RawEncoder).hex())
    svc.add_repo("pool-1", "demo", str(_git(tmp_path / "src")),
                 source={"user_id": OWNER})
    return conn, svc, maint, member, transport


# --- /pool fetch -------------------------------------------------------------


def test_pool_fetch_returns_hosted_projects_tickets_and_manifest(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    out = svc.pool_fetch(projects=True, tasks=True, source={"user_id": OWNER})
    assert out["pool_id"] == "pool-1"
    assert [p["repo_id"] for p in out["projects"]] == ["demo"]
    assert out["tickets"] == []
    # The manifest carries the transfer ticket, the wrapped keys and the
    # maintainer's identity — the artifact /work fetch consumes.
    out = svc.pool_fetch("demo", source={"user_id": OWNER})
    assert out["repo_id"] == "demo" and out["pool_id"] == "pool-1"
    manifest = json.loads(out["manifest_json"])
    from crypto import manifest_hash as _mh
    assert out["manifest_hash"] == _mh(manifest)
    assert out["transfer_ticket"] == manifest["transfer_ticket"]
    assert manifest["transfer_ticket"]
    assert maint.key_id in manifest["wrapped_keys"]
    assert manifest["signer_key_id"] == maint.key_id
    assert manifest["version"] == 1


def test_pool_fetch_never_returns_pending_tickets(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    ticket = svc.add_ticket("pool-1", "demo", "Pending", source={"user_id": OWNER})
    assert ticket["state"] == "pending"  # auto-approve off by default
    out = svc.pool_fetch(projects=True, tasks=True, source={"user_id": OWNER})
    assert out["tickets"] == []
    # The owner sees it through /task list with an explicit state filter.
    assert [t["ticket_id"] for t in svc.list_tickets(pool_id="pool-1",
                                                     state="pending",
                                                     source={"user_id": OWNER})] == [ticket["ticket_id"]]


def test_pool_fetch_foreign_pool_refused_naming_hosted_pools(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    with pytest.raises(PoolNotLocalError) as exc:
        svc.pool_fetch("demo", pool_id="pool-elsewhere", source={"user_id": OWNER})
    msg = str(exc.value)
    assert "pool-elsewhere is not hosted by this gateway" in msg
    assert "pool-1" in msg  # names its hosted pools


def test_pool_fetch_requires_authorized_user(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    with pytest.raises(PermissionError):
        svc.pool_fetch(projects=True, source={"user_id": "stranger"})


# --- /work fetch -------------------------------------------------------------


def test_work_fetch_decrypts_and_clones_with_own_box_key(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    manifest = json.loads(svc.pool_fetch("demo", source={"user_id": OWNER})["manifest_json"])
    # A second gateway hosting no pool at all: /work fetch runs on the
    # REQUESTING agent's own gateway with its own box key. (The transport
    # instance is shared here because FakeTransport is an in-memory
    # registry — production uses the real Iroh network.)
    other_conn = memdb()
    fetch_svc = PoolService(other_conn, transport,
                            caller_key_id=member.key_id,
                            caller_signing_key=member.signing_key,
                            caller_box_sk=member.box_sk)
    out = fetch_svc.work_fetch(manifest, dest=tmp_path / "fetched")
    assert out["clone_dir"]
    assert (Path(out["clone_dir"]) / "hello.txt").read_text() == "hello\n"


def test_work_fetch_refuses_agent_without_wrapped_key(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    manifest = json.loads(svc.pool_fetch("demo", source={"user_id": OWNER})["manifest_json"])
    outsider = ensure_identity(tmp_path / "nope")
    fetch_svc = PoolService(memdb(), FakeTransport(),
                            caller_key_id=outsider.key_id,
                            caller_signing_key=outsider.signing_key,
                            caller_box_sk=outsider.box_sk)
    # The wrapped-key ACL is the byte boundary: not invited, no download.
    with pytest.raises(PermissionError):
        fetch_svc.work_fetch(manifest, dest=tmp_path / "fetched")


def test_work_fetch_needs_a_manifest(tmp_path):
    conn, svc, maint, member, transport = world(tmp_path)
    ident = ensure_identity(tmp_path / "b")
    fetch_svc = PoolService(conn, FakeTransport(),
                            caller_key_id=ident.key_id,
                            caller_signing_key=ident.signing_key,
                            caller_box_sk=ident.box_sk)
    with pytest.raises(ValueError, match="--manifest"):
        fetch_svc.work_fetch({}, dest=tmp_path / "fetched")
