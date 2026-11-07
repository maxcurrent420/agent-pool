"""Phase 4 push write path (WO-015): grant -> push -> deliver -> complete.

Covers the plan acceptance: a contributor pushes a diff over
FakeTransport and the task completes on the pool gateway; a flagged
diff is refused and the task stays claimed; grants are one-time and
claimant-only; a frozen pool refuses with the exact message.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import FROZEN_MESSAGE, add_member, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport

OWNER = "owner-1"
MEMBER_USER = "member-1"
OTHER_USER = "other-1"
POOL = "pool-1"
REPO = "demo"


def _whitelist(conn, pool_id, user_id, ident):
    """Phase 1 stand-in for the removed invite method: authorize the
    Discord id via the whitelist sync and register the crypto member.
    Key attribution binds on the user's first claim."""
    sync_whitelist(conn, pool_id, [user_id])
    add_member(conn, pool_id, ident.key_id, role="member",
               box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_src(path: Path, files=None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    for name, content in (files or {"hello.txt": "hello push\n"}).items():
        (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def _commit_change(src: Path, name: str, content: str, msg="v2"):
    (src / name).write_text(content)
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", msg], check=True)


def _insert_identity(conn, ident):
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
        " VALUES (?,?,?,0)",
        (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
         ident.box_pk.encode(RawEncoder).hex()),
    )


def world(tmp_path):
    """Maint + member + other, whitelisted pool, repo v1, one claimed ticket."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    other = ensure_identity(tmp_path / "b")
    for ident in (maint, member, other):
        _insert_identity(conn, ident)
    conn.commit()
    transport = FakeTransport()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    member_svc = PoolService(conn, transport, caller_key_id=member.key_id,
                             caller_signing_key=member.signing_key,
                             caller_box_sk=member.box_sk)
    other_svc = PoolService(conn, transport, caller_key_id=other.key_id,
                            caller_signing_key=other.signing_key,
                            caller_box_sk=other.box_sk)
    maint_svc.register_pool(POOL, "Team", discord_user_id=OWNER)
    _whitelist(conn, POOL, MEMBER_USER, member)
    _whitelist(conn, POOL, OTHER_USER, other)
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                       source={"user_id": OWNER})
    ticket = maint_svc.add_ticket(POOL, REPO, "Improve hello", brief="Scope: hello.txt",
                                  source={"user_id": OWNER})
    member_svc.claim(ticket["ticket_id"], source={"user_id": MEMBER_USER})
    return {
        "conn": conn, "transport": transport,
        "maint": maint, "member": member, "other": other,
        "maint_svc": maint_svc, "member_svc": member_svc, "other_svc": other_svc,
        "src": src, "ticket_id": ticket["ticket_id"],
    }


def _member_source():
    return {"user_id": MEMBER_USER}


def _grant_and_bundle(w, src=None):
    grant = w["member_svc"].push_repo(REPO, w["ticket_id"], source=_member_source())
    bundle = w["member_svc"].push_build_bundle(src or w["src"], REPO)
    return grant, bundle


def test_push_grant_is_claimant_only(tmp_path):
    w = world(tmp_path)
    with pytest.raises(PermissionError, match="only the claimant may push"):
        w["other_svc"].push_repo(REPO, w["ticket_id"], source={"user_id": OTHER_USER})
    out = w["member_svc"].push_repo(REPO, w["ticket_id"], source=_member_source())
    assert out["ok"] is True and out["token"]
    row = w["conn"].execute("SELECT * FROM push_grants WHERE token=?",
                            (out["token"],)).fetchone()
    assert row is not None and row["ticket_id"] == w["ticket_id"] and row["consumed_at"] is None


def test_clean_push_completes_task(tmp_path):
    w = world(tmp_path)
    _commit_change(w["src"], "hello.txt", "hello push v2\n")
    grant, bundle = _grant_and_bundle(w)
    manifest = bundle["manifest"]
    assert manifest["state"] == "submitted"
    # No ledger write from building the bundle: only v1 accepted exists.
    rows = [tuple(r) for r in w["conn"].execute("SELECT version, state FROM repo_versions")]
    assert rows == [(1, "accepted")]

    out = w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                      manifest, source=_member_source())
    assert out["ok"] is True and out["state"] == "completed"
    assert out["version"] == 2 and out["dek_rotated"] is True
    ticket = w["maint_svc"].show_ticket(w["ticket_id"])
    assert ticket["state"] == "completed"
    # New accepted version on the chain; completed task off the available set.
    rows = [tuple(r) for r in w["conn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "accepted"), (2, "accepted")]
    avail = w["maint_svc"].fetch_repo_list(source={"user_id": OWNER})
    assert avail["repos"][0]["available_tasks"] == []
    assert avail["repos"][0]["accepted_version"] == 2


def test_flagged_push_refused_task_stays_claimed(tmp_path):
    w = world(tmp_path)
    _commit_change(w["src"], "evil.py", "result = eval(user_input)\n")
    grant, bundle = _grant_and_bundle(w)
    manifest = bundle["manifest"]
    out = w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                      manifest, source=_member_source())
    assert out["ok"] is False and out["verdict"] == "flagged" and out["hits"]
    ticket = w["maint_svc"].show_ticket(w["ticket_id"])
    assert ticket["state"] == "claimed"
    # Nothing accepted; a push_flagged event recorded the scan hits.
    rows = [tuple(r) for r in w["conn"].execute("SELECT version, state FROM repo_versions")]
    assert rows == [(1, "accepted")]
    ev = w["conn"].execute(
        "SELECT payload_json FROM events WHERE kind='push_flagged'").fetchone()
    assert ev is not None
    payload = json.loads(ev[0])
    assert payload["ticket_id"] == w["ticket_id"] and payload["hits"]
    # Grant NOT consumed by a flagged push: fixing the diff can redeliver.
    grant_row = w["conn"].execute("SELECT consumed_at FROM push_grants WHERE token=?",
                                  (grant["token"],)).fetchone()
    assert grant_row[0] is None


def test_grant_replay_refused(tmp_path):
    w = world(tmp_path)
    _commit_change(w["src"], "hello.txt", "hello push v2\n")
    grant, bundle = _grant_and_bundle(w)
    manifest = bundle["manifest"]
    out = w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                      manifest, source=_member_source())
    assert out["ok"] is True
    with pytest.raises(PermissionError, match="unknown or replayed"):
        w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                    manifest, source=_member_source())


def test_released_task_grant_refused_at_deliver(tmp_path):
    w = world(tmp_path)
    grant, bundle = _grant_and_bundle(w)
    w["member_svc"].release(w["ticket_id"], source=_member_source())
    with pytest.raises(PermissionError, match="grant expired"):
        w["maint_svc"].push_deliver(bundle["manifest"]["transfer_ticket"], grant["token"],
                                    bundle["manifest"], source=_member_source())


def test_other_claimant_deliver_refused(tmp_path):
    """A manifest from a non-claimant submitter cannot use the claimant's grant."""
    w = world(tmp_path)
    grant = w["member_svc"].push_repo(REPO, w["ticket_id"], source=_member_source())
    bundle = w["other_svc"].push_build_bundle(w["src"], REPO)
    with pytest.raises(PermissionError, match="not the grant claimant"):
        w["maint_svc"].push_deliver(bundle["manifest"]["transfer_ticket"], grant["token"],
                                    bundle["manifest"], source=_member_source())


def test_frozen_pool_refuses_push_with_exact_message(tmp_path):
    w = world(tmp_path)
    w["maint_svc"].freeze(POOL, True, source={"user_id": OWNER})
    with pytest.raises(Exception, match="pool frozen — contact the maintainer"):
        w["member_svc"].push_repo(REPO, w["ticket_id"], source=_member_source())
    w["maint_svc"].freeze(POOL, False, source={"user_id": OWNER})
    grant, bundle = _grant_and_bundle(w)
    w["maint_svc"].freeze(POOL, True, source={"user_id": OWNER})
    with pytest.raises(Exception, match="pool frozen — contact the maintainer"):
        w["maint_svc"].push_deliver(bundle["manifest"]["transfer_ticket"], grant["token"],
                                    bundle["manifest"], source=_member_source())


def test_push_deliver_verifies_signature_and_membership(tmp_path):
    w = world(tmp_path)
    grant, bundle = _grant_and_bundle(w)
    manifest = dict(bundle["manifest"])
    manifest["content_hash"] = "0" * 64  # tamper: signature must fail
    with pytest.raises(RuntimeError, match="signature does not verify"):
        w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                    manifest, source=_member_source())


def test_push_routes_over_configured_transport(tmp_path):
    """The bundle's transfer ticket is fetchable on the same transport the
    pool gateway uses at deliver time (FakeTransport here; Iroh+Tor in prod)."""
    w = world(tmp_path)
    grant, bundle = _grant_and_bundle(w)
    manifest = bundle["manifest"]
    assert w["transport"]._store.get(manifest["transfer_ticket"]) is not None
    out = w["maint_svc"].push_deliver(manifest["transfer_ticket"], grant["token"],
                                      manifest, source=_member_source())
    assert out["ok"] is True


def test_push_build_bundle_rejects_non_directory_repo_path(tmp_path):
    """Path contract: repo_path must be an existing directory on the
    agent's gateway (Discord never carries repo bytes)."""
    w = world(tmp_path)
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="push_build_bundle: repo_path is not an existing directory"):
        w["member_svc"].push_build_bundle(str(missing), REPO)
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("not a dir\n")
    with pytest.raises(ValueError, match="push_build_bundle: repo_path is not an existing directory"):
        w["member_svc"].push_build_bundle(str(a_file), REPO)


def test_push_build_bundle_returns_manifest_json(tmp_path):
    """The agent reads manifest_json from the result and carries it over
    Discord to /push repo --deliver --manifest."""
    w = world(tmp_path)
    bundle = w["member_svc"].push_build_bundle(w["src"], REPO)
    assert "manifest_json" in bundle
    assert json.loads(bundle["manifest_json"]) == bundle["manifest"]
    assert bundle["manifest"]["transfer_ticket"] in bundle["manifest_json"]
