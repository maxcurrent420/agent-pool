"""Regression: deliver paths must register the repos row BEFORE auditing.

Live failure (maintainer gateway): ``add_repo_deliver`` ran
``prop.audit(...)`` — which INSERTs into ``audits`` — before
``L.upsert_repo(...)`` created the parent ``repos`` row. On a gateway
where the repo never went through the ``--path`` fast path, ``repos``
is empty and the audit INSERT dies with
``sqlite3.IntegrityError: FOREIGN KEY constraint failed``
(audits.repo_id -> repos(repo_id)).

SQLite only enforces FKs per-connection with ``PRAGMA foreign_keys=ON``.
The plugin now sets it in ``ledger.get_db()``/``init_ledger()``, but a
connection can still run unenforced (e.g. a test harness that opts out,
or remnants of older connections), which is exactly why the existing
deliver tests stayed green. These tests turn enforcement
ON and deliver with an EMPTY ``repos`` table; they fail (red) on the
pre-fix ordering and pass (green) after the reorder.
"""
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from nacl.encoding import RawEncoder
from nacl.signing import SigningKey

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import add_member, bind_membership, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport

POOL = "pool-1"
REPO = "demo"
MEMBER_USER = "member-1"
OWNER = "owner-1"


def memdb(foreign_keys=True):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c = init_ledger(c)
    # init_ledger now enables FK enforcement itself; the
    # foreign_keys=False worlds explicitly opt OUT again to simulate
    # a gateway connection that never enforced (needed to DELETE the
    # parent row and leave the orphan for the deliver step to repair).
    c.execute("PRAGMA foreign_keys=ON" if foreign_keys
              else "PRAGMA foreign_keys=OFF")
    return c


def _insert_identity(conn, ident):
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex,"
        " created_at) VALUES (?,?,?,0)",
        (ident.key_id,
         ident.verify_key.encode(RawEncoder).hex(),
         ident.box_pk.encode(RawEncoder).hex()))
    conn.commit()


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"],
                   check=True)
    (path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"],
                   check=True)
    return path


def world(tmp_path, foreign_keys=True, add_repo_step=False):
    """Maintainer + member, pool registered, NO repos row unless asked."""
    conn = memdb(foreign_keys=foreign_keys)
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
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    sync_whitelist(conn, POOL, [MEMBER_USER])
    add_member(conn, POOL, member.key_id, role="member",
               box_pubkey_hex=member.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=member.verify_key.encode(RawEncoder).hex())
    bind_membership(conn, POOL, MEMBER_USER, member.key_id)
    src = _git_src(tmp_path / "src")
    if add_repo_step:
        maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                           source={"user_id": OWNER})
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] \
        == (1 if add_repo_step else 0)
    return {"conn": conn, "maint_svc": maint_svc, "member_svc": member_svc,
            "src": src}


def test_add_repo_deliver_registers_repo_before_audit(tmp_path):
    """Empty repos + FK on: add-repo deliver must succeed and the repos
    row must exist afterwards (red pre-fix: FOREIGN KEY constraint
    failed at the audits INSERT)."""
    w = world(tmp_path)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    assert w["conn"].execute(
        "SELECT COUNT(*) FROM repos").fetchone()[0] == 0
    out = w["maint_svc"].add_repo_deliver(
        manifest["transfer_ticket"], manifest,
        source={"user_id": MEMBER_USER})
    assert out["ok"] is True and out["state"] == "submitted"
    row = w["conn"].execute(
        "SELECT repo_id, pool_id FROM repos WHERE repo_id=?",
        (REPO,)).fetchone()
    assert row is not None and row["pool_id"] == POOL
    # The audit row landed too (FK now resolves).
    assert w["conn"].execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 1


def test_push_deliver_registers_repo_before_audit(tmp_path):
    """Same defect class on the push path: audit runs before any repo
    registration. The repos row is removed (FK off) and enforcement
    turned on just before deliver; push_deliver must re-register the
    row itself before auditing (red pre-fix)."""
    w = world(tmp_path, foreign_keys=False, add_repo_step=True)
    conn = w["conn"]
    ticket = w["maint_svc"].add_ticket(POOL, REPO, "Work",
                                       source={"user_id": OWNER})
    w["member_svc"].claim(ticket["ticket_id"], source={"user_id": MEMBER_USER})
    grant = w["member_svc"].push_repo(REPO, ticket["ticket_id"],
                                       source={"user_id": MEMBER_USER})
    bundle = w["member_svc"].push_build_bundle(str(w["src"]), REPO)
    manifest = bundle["manifest"]
    # Simulate the live gateway: parent row missing, FK enforced.
    conn.execute("DELETE FROM repos WHERE repo_id=?", (REPO,))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
    out = w["maint_svc"].push_deliver(
        manifest["transfer_ticket"], grant["token"], manifest,
        source={"user_id": MEMBER_USER})
    assert out["ok"] is True
    row = conn.execute(
        "SELECT repo_id, pool_id FROM repos WHERE repo_id=?",
        (REPO,)).fetchone()
    assert row is not None and row["pool_id"] == POOL


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "agent_pool_plugin_deliver_fk",
        Path(__file__).parent.parent / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


def test_error_gives_tor_fix_only_for_tor_failures():
    """_error must not blame Tor for unrelated errors (e.g. the ledger
    FK IntegrityError), while genuine Tor errors keep the Tor string."""
    plugin = _load_plugin()
    fk = json.loads(plugin._error(
        sqlite3.IntegrityError("FOREIGN KEY constraint failed")))
    assert fk["ok"] is False
    assert "Tor" not in fk["fix"]
    assert "agent-pool-tor" not in fk["fix"]
    assert "FOREIGN KEY" in fk["error"]

    tor = json.loads(plugin._error(
        RuntimeError("Tor is not ready: onion endpoint unreachable")))
    assert "agent-pool-tor" in tor["fix"]
    assert "Tor readiness" in tor["fix"]
