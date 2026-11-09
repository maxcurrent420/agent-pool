"""Tor persistence + agent-side approval gate (the three-part fix).

1. audit() persists the BLAKE3-verified bundle as ``bundle-v{N}.git``
   (mode 0600) into the pool checkout dir and reports ``bundle_path``.
2. ``scripts/pool-approve.py`` fetches the accepted bytes over Tor
   in-process after accept and lands them in the checkout dir.
3. add_repo_approve / push_approve (and the deny counterparts, which
   share the gate and are reachable from the same script) authorize
   agent-side callers (source=None) by gateway identity while the
   Discord path still requires the Discord owner.
"""
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))
from crypto import blake3_hash
from identity import ensure_identity
from ledger import (add_member, create_repo, init_ledger, create_pool,
                    sync_whitelist)
from tickets import PoolService
from transport import FakeTransport

OWNER = "owner-1"
MEMBER_USER = "member-1"
POOL = "pool-1"
REPO = "demo"


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _insert_identity(conn, ident):
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
        " VALUES (?,?,?,0)",
        (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
         ident.box_pk.encode(RawEncoder).hex()),
    )


def _whitelist(conn, pool_id, user_id, ident):
    sync_whitelist(conn, pool_id, [user_id])
    add_member(conn, pool_id, ident.key_id, role="member",
               box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello add-repo\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path, checkout=None):
    """Maintainer + contributor member; optionally a pre-registered repo
    row whose ``repos.path`` is the pool checkout dir."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        _insert_identity(conn, ident)
    conn.commit()
    transport = FakeTransport()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    member_svc = PoolService(conn, transport, caller_key_id=member.key_id,
                             caller_signing_key=member.signing_key,
                             caller_box_sk=member.box_sk)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    _whitelist(conn, POOL, MEMBER_USER, member)
    if checkout is not None:
        create_repo(conn, REPO, POOL, name=REPO, path=str(checkout))
    return {"conn": conn, "transport": transport,
            "maint_svc": maint_svc, "member_svc": member_svc,
            "src": _git_src(tmp_path / "src")}


def _owner_source():
    return {"user_id": OWNER}


def _member_source():
    return {"user_id": MEMBER_USER}


def _delivered(w):
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    out = w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest, source=_member_source())
    assert out["ok"] is True
    return manifest


# -- (a) audit-time persistence -------------------------------------------

def test_audit_persists_bundle_into_pool_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    prop = w["maint_svc"]._propagator(POOL)
    maintainer = prop._members[prop._maintainer_key_id]
    aud = prop.audit(manifest=manifest, maintainer=maintainer)
    bundle_path = Path(aud["bundle_path"])
    assert bundle_path == checkout / "bundle-v1.git"
    assert bundle_path.is_file()
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600
    assert blake3_hash(bundle_path.read_bytes()) == manifest["content_hash"]
    # Idempotent: re-persisting matching bytes keeps the same file.
    from propagation import pack_bundle
    again = prop._persist_verified_bundle(manifest, pack_bundle(w["src"]))
    assert again == aud["bundle_path"]
    assert blake3_hash(bundle_path.read_bytes()) == manifest["content_hash"]


def test_deliver_lands_bundle_in_checkout_dir(tmp_path):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    manifest = _delivered(w)
    bundle_path = checkout / "bundle-v1.git"
    assert bundle_path.is_file()
    assert stat.S_IMODE(bundle_path.stat().st_mode) == 0o600
    assert blake3_hash(bundle_path.read_bytes()) == manifest["content_hash"]


# -- (b) agent-side approval gate ------------------------------------------

def test_add_repo_approve_agent_side_by_gateway_identity(tmp_path):
    w = world(tmp_path)
    _delivered(w)
    # source=None, caller IS the owner gateway -> succeeds.
    out = w["maint_svc"].add_repo_approve(REPO, source=None)
    assert out["ok"] is True and out["state"] == "accepted"


def test_add_repo_approve_agent_side_wrong_gateway_refused(tmp_path):
    w = world(tmp_path)
    _delivered(w)
    # source=None, caller is NOT the owner gateway -> refused.
    with pytest.raises(PermissionError, match="owner gateway identity"):
        w["member_svc"].add_repo_approve(REPO, source=None)


def test_add_repo_approve_discord_path_unchanged(tmp_path):
    w = world(tmp_path)
    _delivered(w)
    # Discord source: non-owner user still refused (existing behavior).
    with pytest.raises(PermissionError, match="not authorized"):
        w["maint_svc"].add_repo_approve(REPO, source=_member_source())
    # Discord source + owner user still succeeds.
    out = w["maint_svc"].add_repo_approve(REPO, source=_owner_source())
    assert out["ok"] is True


def test_push_deny_agent_side_by_gateway_identity(tmp_path):
    """push_deny shares the gate and is reachable agent-side (--deny)."""
    w = world(tmp_path)
    _delivered(w)
    with pytest.raises(PermissionError, match="owner gateway identity"):
        w["member_svc"].add_repo_deny(REPO, source=None)
    out = w["maint_svc"].add_repo_deny(REPO, source=None)
    assert out["ok"] is True and out["state"] == "denied"


# -- (c) pool-approve.py e2e ------------------------------------------------

def _load_pool_approve():
    path = Path(__file__).parent.parent / "scripts" / "pool-approve.py"
    spec = importlib.util.spec_from_file_location("pool_approve_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pool_approve_script_fetches_accepted_bundle(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    manifest = _delivered(w)
    mod = _load_pool_approve()
    monkeypatch.setattr(mod, "build_service",
                        lambda data_dir=None: w["maint_svc"])
    rc = mod.main(["--repo", REPO])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["state"] == "accepted"
    fetched = out["fetched"]
    bundle_path = Path(fetched["bundle_file"])
    assert bundle_path == checkout / "bundle-v1.git"
    assert bundle_path.is_file()
    assert Path(fetched["clone_dir"]).is_dir()
    assert fetched["checkout_dir"] == str(checkout)
    assert blake3_hash(bundle_path.read_bytes()) == manifest["content_hash"]
    assert fetched["content_hash"] == manifest["content_hash"]
