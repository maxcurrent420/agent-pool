"""add_repo semantics: --path is required and the --link pointer form is
deleted (Phase 2 / WO-013) — passing repo_link is a TypeError, before any
write. All propagation runs through FakeTransport; the only real
subprocesses are ``git`` for fixture repos.

Also covers the add-repo link flow: contributor builds a bundle over the
transport, the maintainer gateway delivers (fetch + audit + record as
submitted), the owner approves (submitted -> accepted), and flagged
audits stay submitted.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))
from identity import ensure_identity
from ledger import add_member, init_ledger, create_pool, sync_whitelist
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


def _svc(c):
    create_pool(c, "pool-1", "Team", "agent:maint")
    return PoolService(c, caller_key_id="agent:maint",
                       caller_signing_key=SigningKey.generate())


def test_add_repo_requires_path():
    svc = _svc(memdb())
    with pytest.raises(ValueError, match="--path"):
        svc.add_repo("pool-1", "demo", "")


def test_add_repo_link_kwarg_rejected(tmp_path):
    """The --link branch is deleted: repo_link is an unexpected keyword."""
    svc = _svc(memdb())
    with pytest.raises(TypeError):
        svc.add_repo("pool-1", "demo", str(tmp_path),
                     repo_link="https://example/demo.git")


def test_add_repo_link_attempt_writes_nothing():
    """TypeError is raised before any write: no remote_repos / repo_versions
    rows for an add_repo that carries the deleted link kwarg."""
    c = memdb()
    svc = _svc(c)
    with pytest.raises(TypeError):
        svc.add_repo("pool-1", "demo", "", repo_link="https://example/demo.git")
    assert c.execute("SELECT COUNT(*) FROM remote_repos").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM repo_versions").fetchone()[0] == 0


def _whitelist(conn, pool_id, user_id, ident):
    sync_whitelist(conn, pool_id, [user_id])
    add_member(conn, pool_id, ident.key_id, role="member",
               box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())


def _insert_identity(conn, ident):
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
        " VALUES (?,?,?,0)",
        (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
         ident.box_pk.encode(RawEncoder).hex()),
    )


def _git_src(path: Path, files=None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    for name, content in (files or {"hello.txt": "hello add-repo\n"}).items():
        (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path):
    """Maintainer + contributor member + a bare checkout (no --path add)."""
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
    return {"conn": conn, "transport": transport,
            "maint_svc": maint_svc, "member_svc": member_svc,
            "src": _git_src(tmp_path / "src")}


def _member_source():
    return {"user_id": MEMBER_USER}


def _owner_source():
    return {"user_id": OWNER}


def test_add_repo_build_bundle_returns_deliver_command(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    assert bundle["manifest"]["state"] == "submitted"
    assert bundle["transfer_ticket"] if "transfer_ticket" in bundle else True
    assert bundle["manifest"]["transfer_ticket"] in bundle["deliver_command"]
    assert bundle["deliver_command"].startswith("/add-repo --deliver ")
    assert "--repo demo" in bundle["deliver_command"]
    assert json.loads(bundle["manifest_json"]) == bundle["manifest"]
    # No ledger write from the contributor side.
    assert w["conn"].execute("SELECT COUNT(*) FROM repo_versions").fetchone()[0] == 0


def test_add_repo_deliver_records_submitted(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    out = w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest, source=_member_source())
    assert out["ok"] is True and out["state"] == "submitted"
    assert out["verdict"] == "clean" and out["version"] == 1
    rows = [tuple(r) for r in w["conn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "submitted")]
    # No chain row for an unapproved version.
    assert w["conn"].execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 0
    assert "manifest" not in out and len(json.dumps(out)) < 4096


def test_add_repo_deliver_requires_manifest_and_ticket(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    with pytest.raises(ValueError, match="--manifest"):
        w["maint_svc"].add_repo_deliver(manifest["transfer_ticket"], {},
                                        source=_member_source())
    with pytest.raises(PermissionError, match="does not match"):
        w["maint_svc"].add_repo_deliver("wrong-ticket", manifest,
                                        source=_member_source())
    with pytest.raises(PermissionError, match="does not match"):
        w["maint_svc"].add_repo_deliver(manifest["transfer_ticket"], manifest,
                                        source=_member_source(), repo_id="other")


def test_add_repo_deliver_maintainer_gate_refuses_non_maintainer(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    with pytest.raises(PermissionError, match="only the pool maintainer gateway"):
        w["member_svc"].add_repo_deliver(manifest["transfer_ticket"], manifest,
                                         source=_member_source())


def test_add_repo_deliver_signature_refused(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = dict(bundle["manifest"])
    manifest["content_hash"] = "0" * 64  # tamper: signature must fail
    with pytest.raises(RuntimeError, match="signature does not verify"):
        w["maint_svc"].add_repo_deliver(manifest["transfer_ticket"], manifest,
                                        source=_member_source())


def test_add_repo_deliver_flagged_stays_submitted(tmp_path):
    w = world(tmp_path)
    (w["src"] / "evil.py").write_text("result = eval(user_input)\n")
    subprocess.run(["git", "-C", str(w["src"]), "add", "."], check=True)
    subprocess.run(["git", "-C", str(w["src"]), "commit", "-q", "-m", "evil"], check=True)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    out = w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest, source=_member_source())
    assert out["ok"] is False and out["verdict"] == "flagged"
    rows = [tuple(r) for r in w["conn"].execute(
        "SELECT version, state FROM repo_versions")]
    assert rows == [(1, "submitted")]
    assert w["conn"].execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 0
    ev = w["conn"].execute(
        "SELECT COUNT(*) FROM events WHERE kind='repo_submission_refused'").fetchone()
    assert ev[0] == 1


def test_add_repo_approve_promotes_to_accepted(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    w["maint_svc"].add_repo_deliver(manifest["transfer_ticket"], manifest,
                                    source=_member_source())
    out = w["maint_svc"].add_repo_approve(REPO, source=_owner_source())
    assert out["ok"] is True and out["state"] == "accepted"
    assert out["version"] == 1 and out["dek_rotated"] is True
    rows = [tuple(r) for r in w["conn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "accepted")]
    assert w["conn"].execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 1
    assert "manifest" not in out and len(json.dumps(out)) < 4096


def test_add_repo_approve_owner_only(tmp_path):
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    w["maint_svc"].add_repo_deliver(manifest["transfer_ticket"], manifest,
                                    source=_member_source())
    with pytest.raises(PermissionError, match="not authorized"):
        w["maint_svc"].add_repo_approve(REPO, source=_member_source())


def test_add_repo_resubmission_bumps_version_and_submissions_lists(tmp_path):
    w = world(tmp_path)
    b1 = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    w["maint_svc"].add_repo_deliver(b1["manifest"]["transfer_ticket"],
                                    b1["manifest"], source=_member_source())
    w["maint_svc"].add_repo_approve(REPO, source=_owner_source())
    (w["src"] / "hello.txt").write_text("hello v2\n")
    subprocess.run(["git", "-C", str(w["src"]), "add", "."], check=True)
    subprocess.run(["git", "-C", str(w["src"]), "commit", "-q", "-m", "v2"], check=True)
    b2 = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    assert b2["manifest"]["version"] == 2
    out = w["maint_svc"].add_repo_deliver(b2["manifest"]["transfer_ticket"],
                                          b2["manifest"], source=_member_source())
    assert out["version"] == 2 and out["state"] == "submitted"
    listed = w["maint_svc"].add_repo_submissions(source=_owner_source())
    assert [(s["repo_id"], s["version"]) for s in listed["submissions"]] == [(REPO, 2)]
    rows = [tuple(r) for r in w["conn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "accepted"), (2, "submitted")]
