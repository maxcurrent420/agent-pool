"""Contributor add-repo build on a gateway that hosts NO pool.

The contributor's build must use the enrollment gateway-record context
(pool id + maintainer + member wrap targets published by enroll_member)
instead of a locally hosted pool. The deliver path is maintainer-side
and untouched. Without a recorded enrollment context the build still
fails closed (PoolNotLocalError).
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
from ledger import init_ledger
from tickets import PoolService, PoolNotLocalError
from transport import FakeTransport

OWNER = "owner-1"
POOL = "pool-1"
REPO = "demo"


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello contributor\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path):
    """Two ledgers: maintainer hosts the pool; contributor hosts none."""
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "c")
    transport = FakeTransport()

    mconn = memdb()
    maint_svc = PoolService(mconn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)

    # Enrollment on the maintainer ledger (owner-only, public keys only).
    enrolled = maint_svc.enroll_member(
        POOL, contrib.key_id,
        contrib.box_pk.encode(RawEncoder).hex(),
        contrib.verify_key.encode(RawEncoder).hex(),
        discord_user_id="contrib-user",
        source={"user_id": OWNER})

    # Contributor ledger: separate DB, NO hosted pool.
    cconn = memdb()
    contrib_svc = PoolService(cconn, transport, caller_key_id=contrib.key_id,
                              caller_signing_key=contrib.signing_key,
                              caller_box_sk=contrib.box_sk)
    return {"mconn": mconn, "cconn": cconn, "maint": maint,
            "contrib": contrib, "maint_svc": maint_svc,
            "contrib_svc": contrib_svc, "enrolled": enrolled,
            "src": _git_src(tmp_path / "src")}


def _world(tmp_path):
    return world(tmp_path)


def test_contributor_build_without_hosted_pool(tmp_path):
    w = _world(tmp_path)
    ctx = w["enrolled"]["pool_context"]
    assert ctx["pool_id"] == POOL
    assert ctx["maintainer_key_id"] == w["maint"].key_id
    member_ids = {m["key_id"] for m in ctx["members"]}
    assert w["contrib"].key_id in member_ids
    assert w["maint"].key_id in member_ids
    # Contributor records the enrollment context on its own ledger.
    w["contrib_svc"].record_pool_context(POOL, ctx)

    bundle = w["contrib_svc"].add_repo_build_bundle(
        str(w["src"]), REPO, pool_id=POOL, version=1)
    assert bundle["manifest"]["state"] == "submitted"
    assert bundle["deliver_command"].startswith("/add-repo --deliver ")
    assert f"--repo {REPO}" in bundle["deliver_command"]
    assert json.loads(bundle["manifest_json"]) == bundle["manifest"]
    # DEK wrapped for every member in the enrollment context.
    wrapped = bundle["manifest"]["wrapped_keys"]
    assert w["maint"].key_id in wrapped
    assert w["contrib"].key_id in wrapped
    # No ledger write on the contributor side.
    assert w["cconn"].execute(
        "SELECT COUNT(*) FROM repo_versions").fetchone()[0] == 0


def test_contributor_build_pool_id_none_uses_single_gateway_record(tmp_path):
    w = _world(tmp_path)
    w["contrib_svc"].record_pool_context(POOL, w["enrolled"]["pool_context"])
    bundle = w["contrib_svc"].add_repo_build_bundle(
        str(w["src"]), REPO, version=1)
    assert bundle["manifest"]["state"] == "submitted"
    assert bundle["deliver_command"].startswith("/add-repo --deliver ")


def test_contributor_build_without_context_fails_closed(tmp_path):
    w = _world(tmp_path)
    with pytest.raises(PoolNotLocalError):
        w["contrib_svc"].add_repo_build_bundle(str(w["src"]), REPO, pool_id=POOL)
