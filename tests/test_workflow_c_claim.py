"""WO-004: Workflow C — contributor-originated claim with the Tor link in
the response, plus remote member enrollment.

Two services share one ledger in these tests (the maintainer gateway
hosts the pool; the contributor gateway's key arrives via enrollment,
exactly as over Discord). The contributor-side script flow is
scripts/contributor-claim.py; the live two-bot proof is the operator's
restart step.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity  # noqa: E402
from ledger import add_member, init_ledger, sync_whitelist  # noqa: E402
from tickets import PoolService  # noqa: E402
from transport import FakeTransport  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
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
         ident.box_pk.encode(RawEncoder).hex()))
    conn.commit()


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello workflow-c\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path):
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "c")
    for ident in (maint, contrib):
        _insert_identity(conn, ident)
    transport = FakeTransport()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    contrib_svc = PoolService(conn, transport, caller_key_id=contrib.key_id,
                              caller_signing_key=contrib.signing_key,
                              caller_box_sk=contrib.box_sk)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    sync_whitelist(conn, POOL, [MEMBER_USER])
    # v1 accepted by the maintainer fast path (no enrollment yet).
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), source={"user_id": OWNER})
    ticket = maint_svc.add_ticket(POOL, REPO, "Work",
                                  source={"user_id": OWNER})
    return {"conn": conn, "transport": transport,
            "maint_svc": maint_svc, "contrib_svc": contrib_svc,
            "contrib": contrib, "ticket_id": ticket["ticket_id"]}


def test_claim_reply_carries_tor_link_and_manifest(tmp_path):
    """Bot #1's claim reply carries transfer_ticket + manifest_hash +
    the accepted manifest (trimmed only when over the reply guard)."""
    w = world(tmp_path)
    # Enroll the contributor's real gateway key (public material only).
    keys = w["contrib_svc"].my_keys()
    assert keys["key_id"] == w["contrib"].key_id
    enrolled = w["maint_svc"].enroll_member(
        POOL, keys["key_id"], keys["box_pubkey_hex"], keys["verify_key_hex"],
        discord_user_id=MEMBER_USER, source={"user_id": OWNER})
    assert enrolled["ok"] is True
    assert enrolled["rewrapped_repos"] == [REPO]
    # Rewrap gave the new member a DEK entry for the accepted version.
    from ledger import get_latest_accepted_manifest
    manifest = get_latest_accepted_manifest(w["conn"], REPO)
    assert w["contrib"].key_id in manifest["wrapped_keys"]

    # The contributor claims through its OWN gateway (its enrolled key);
    # the maintainer gateway never presents its key for a contributor —
    # enrollment bound MEMBER_USER -> contributor key, and the gateway
    # identity is not a fallback.
    claim = w["contrib_svc"].claim(w["ticket_id"],
                                   source={"user_id": MEMBER_USER})
    assert claim["transfer_ticket"], "claim reply must carry the Tor link"
    assert claim["manifest_hash"]
    assert claim["repo_id"] == REPO
    # The link actually fetches the accepted bytes for the enrolled
    # contributor (transport double here; Iroh-over-Tor live).
    dest = tmp_path / "dl"
    out = w["contrib_svc"].fetch_repo_download(
        claim["manifest"], dest=dest, source={"user_id": MEMBER_USER})
    assert "clone_dir" in out and "bundle_file" in out
    assert Path(out["clone_dir"]).exists()
    assert out["content_hash"] == claim["manifest"]["content_hash"]


def test_claim_without_enrollment_still_fails_closed(tmp_path):
    """A gateway hosting no pool answers claim closed (unknown ticket)."""
    conn = memdb()
    ident = ensure_identity(tmp_path / "x")
    _insert_identity(conn, ident)
    svc = PoolService(conn, FakeTransport(), caller_key_id=ident.key_id,
                      caller_signing_key=ident.signing_key,
                      caller_box_sk=ident.box_sk)
    with pytest.raises(ValueError, match="unknown ticket"):
        svc.claim("t-does-not-exist", source={"user_id": MEMBER_USER})


def test_enroll_validates_key_material(tmp_path):
    w = world(tmp_path)
    with pytest.raises(ValueError, match="not hex"):
        w["maint_svc"].enroll_member(POOL, "agent:x", "zz", "00" * 32,
                                     source={"user_id": OWNER})
    with pytest.raises(ValueError, match="32 bytes"):
        w["maint_svc"].enroll_member(POOL, "agent:x", "00" * 16, "00" * 32,
                                     source={"user_id": OWNER})


def test_enroll_owner_only(tmp_path):
    w = world(tmp_path)
    keys = w["contrib_svc"].my_keys()
    with pytest.raises(PermissionError):
        w["contrib_svc"].enroll_member(
            POOL, keys["key_id"], keys["box_pubkey_hex"],
            keys["verify_key_hex"], source={"user_id": MEMBER_USER})


def test_contributor_claim_script_prints_claim_text():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "contributor-claim.py"),
         "t-123", "--as", "agent:contrib"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "/claim t-123 --as agent:contrib"


def test_registered_equals_declared_for_changed_surfaces():
    manifest = yaml.safe_load(open(REPO_ROOT / "plugin.yaml"))
    path = REPO_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("agent_pool_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class FakeCtx:
        def __init__(self):
            self.tools, self.commands = {}, {}
            self.config = {"transport": "fake"}

        def register_tool(self, name, toolset, schema, handler):
            self.tools[name] = handler

        def register_command(self, name, handler, description="",
                             args_hint="", argument_mode=None):
            self.commands[name] = handler

        def get_config(self, key, default=None):
            return self.config.get(key, default)

    ctx = FakeCtx()
    mod.register(ctx)
    assert set(ctx.commands) == set(manifest["provides_commands"])
    # Changed surfaces are subcommands on existing commands (no new
    # commands, no new tools).
    assert set(ctx.commands) == {
        "pool", "fetch", "task", "add-repo", "task_add", "task_list",
        "claim", "release", "push", "audit"}
    assert set(ctx.tools) == {"pool_status", "pool_report_usage"}


# --- Task 4: bound-key-only claim, unbound non-maintainer hard-fails --------

def _enroll_contrib(w):
    keys = w["contrib_svc"].my_keys()
    return w["maint_svc"].enroll_member(
        POOL, keys["key_id"], keys["box_pubkey_hex"], keys["verify_key_hex"],
        discord_user_id=MEMBER_USER, source={"user_id": OWNER})


def test_maintainer_gateway_cannot_claim_for_unbound_contributor(tmp_path):
    """The live defect (A1): maintainer gateway + contributor Discord id +
    no key_id must HARD FAIL — never bind the contributor to the
    maintainer's key."""
    w = world(tmp_path)
    # MEMBER_USER is whitelisted by world() but has NO binding.
    assert w["conn"].execute(
        "SELECT key_id FROM pool_memberships WHERE discord_user_id=?",
        (MEMBER_USER,)).fetchone() is None
    with pytest.raises(PermissionError, match="has no bound key"):
        w["maint_svc"].claim(w["ticket_id"], source={"user_id": MEMBER_USER})
    # Nothing was bound and nothing was claimed.
    assert w["conn"].execute(
        "SELECT key_id FROM pool_memberships WHERE discord_user_id=?",
        (MEMBER_USER,)).fetchone() is None
    assert w["conn"].execute(
        "SELECT state FROM tickets WHERE ticket_id=?",
        (w["ticket_id"],)).fetchone()[0] == "open"


def test_bound_contributor_claims_with_own_key(tmp_path):
    w = world(tmp_path)
    _enroll_contrib(w)
    w["contrib_svc"].claim(w["ticket_id"], source={"user_id": MEMBER_USER})
    ticket = w["conn"].execute(
        "SELECT claimant_key_id FROM tickets WHERE ticket_id=?",
        (w["ticket_id"],)).fetchone()
    assert ticket[0] == w["contrib"].key_id
    claim = w["conn"].execute(
        "SELECT key_id FROM claims WHERE ticket_id=?",
        (w["ticket_id"],)).fetchone()
    assert claim[0] == w["contrib"].key_id


def test_maintainer_id_claims_with_gateway_key(tmp_path):
    w = world(tmp_path)
    w["maint_svc"].claim(w["ticket_id"], source={"user_id": OWNER})
    ticket = w["conn"].execute(
        "SELECT claimant_key_id FROM tickets WHERE ticket_id=?",
        (w["ticket_id"],)).fetchone()
    assert ticket[0] == w["maint_svc"]._caller_key_id


def test_bound_user_presenting_different_key_refused(tmp_path):
    w = world(tmp_path)
    _enroll_contrib(w)
    with pytest.raises(PermissionError):
        w["contrib_svc"].claim(w["ticket_id"], key_id="agent:other-key",
                               source={"user_id": MEMBER_USER})
