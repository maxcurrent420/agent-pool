"""record_pool_context must persist enrollment-context members (public key
material only) into the recording gateway's members table, so cross-gateway
manifest signature verification (_recorded_verify_key_hex) can resolve the
maintainer signer's verify key on a contributor ledger that hosts no pool.

Conflict/merge discipline (bind_membership-style): a local members row is
authoritative — its role is never overwritten, empty fields may be filled,
and a CONFLICTING non-empty verify_pubkey_hex is never silently swapped:
the local row is kept and the conflict is reported.
"""
import sqlite3
import sys
from pathlib import Path

from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))
import ledger as L
from identity import ensure_identity
from ledger import init_ledger
from tickets import PoolService
from transport import FakeTransport

POOL = "pool-ctx-members"
OWNER = "owner-1"
MAINT_VERIFY = "63f980252946dfe07e1da63cb6a97a4786425e7efbe6d8cd6a054ce5432dae2f"
OTHER_VERIFY = "aa" * 32


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _world(tmp_path):
    """Maintainer hosts the pool; contributor hosts none (fresh ledger)."""
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "c")
    transport = FakeTransport()
    mconn = memdb()
    maint_svc = PoolService(mconn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    enrolled = maint_svc.enroll_member(
        POOL, contrib.key_id,
        contrib.box_pk.encode(RawEncoder).hex(),
        contrib.verify_key.encode(RawEncoder).hex(),
        discord_user_id="contrib-user",
        source={"user_id": OWNER})
    cconn = memdb()
    contrib_svc = PoolService(cconn, transport, caller_key_id=contrib.key_id,
                              caller_signing_key=contrib.signing_key,
                              caller_box_sk=contrib.box_sk)
    return {"mconn": mconn, "cconn": cconn, "maint": maint,
            "contrib": contrib, "maint_svc": maint_svc,
            "contrib_svc": contrib_svc, "enrolled": enrolled}


def _context(w):
    """Enrollment context with the maintainer's verify key populated
    (as the next enrollment will carry it)."""
    ctx = dict(w["enrolled"]["pool_context"])
    members = []
    for m in ctx["members"]:
        m = dict(m)
        if m["key_id"] == w["maint"].key_id:
            m["verify_key_hex"] = MAINT_VERIFY
        members.append(m)
    ctx["members"] = members
    return ctx


def _member_row(conn, key_id):
    return conn.execute(
        "SELECT pool_id, key_id, role, box_pubkey_hex, verify_pubkey_hex"
        " FROM members WHERE key_id=?", (key_id,)).fetchone()


def test_record_creates_members_rows_with_exact_keys(tmp_path):
    w = _world(tmp_path)
    ctx = _context(w)
    w["contrib_svc"].record_pool_context(POOL, ctx)
    for m in ctx["members"]:
        row = _member_row(w["cconn"], m["key_id"])
        assert row is not None, f"missing members row for {m['key_id']}"
        assert row["verify_pubkey_hex"] == (m.get("verify_key_hex") or "")
        assert row["box_pubkey_hex"] == (m.get("box_pubkey_hex") or "")
    maint_row = _member_row(w["cconn"], w["maint"].key_id)
    assert maint_row["verify_pubkey_hex"] == MAINT_VERIFY
    assert maint_row["role"] == "maintainer"


def test_recorded_verify_key_resolves_for_maintainer_signer(tmp_path):
    w = _world(tmp_path)
    w["contrib_svc"].record_pool_context(POOL, _context(w))
    # Fetch-style lookup: members.verify_pubkey_hex first, no transport.
    assert w["contrib_svc"]._recorded_verify_key_hex(
        w["maint"].key_id) == MAINT_VERIFY


def test_rerecord_is_idempotent_noop(tmp_path):
    w = _world(tmp_path)
    ctx = _context(w)
    w["contrib_svc"].record_pool_context(POOL, ctx)
    before = [tuple(r) for r in w["cconn"].execute(
        "SELECT pool_id, key_id, role, box_pubkey_hex, verify_pubkey_hex"
        " FROM members ORDER BY key_id").fetchall()]
    w["contrib_svc"].record_pool_context(POOL, ctx)
    after = [tuple(r) for r in w["cconn"].execute(
        "SELECT pool_id, key_id, role, box_pubkey_hex, verify_pubkey_hex"
        " FROM members ORDER BY key_id").fetchall()]
    assert before == after
    assert len(after) == len(ctx["members"])


def test_conflicting_local_verify_key_kept_and_reported(tmp_path):
    w = _world(tmp_path)
    maint_id = w["maint"].key_id
    # Local row (e.g. locally enrolled) with a CONFLICTING verify key.
    # Needs a pools row for the FK; create it directly.
    w["cconn"].execute(
        "INSERT INTO pools (pool_id, name, maintainer_key_id, relay_url,"
        " settings_json, created_at) VALUES (?,?,?,?,?,?)",
        (POOL, "Team", maint_id, "", "{}", 0.0))
    w["cconn"].commit()
    L.add_member(w["cconn"], POOL, maint_id, role="maintainer",
                 box_pubkey_hex="", verify_pubkey_hex=OTHER_VERIFY)
    result = w["contrib_svc"].record_pool_context(POOL, _context(w))
    row = _member_row(w["cconn"], maint_id)
    assert row["verify_pubkey_hex"] == OTHER_VERIFY  # kept, not swapped
    report = result.get("members_report") or {}
    conflicts = report.get("conflicts") or []
    assert any(c.get("key_id") == maint_id for c in conflicts)
    # Non-conflicting context members still recorded.
    assert _member_row(w["cconn"], w["contrib"].key_id) is not None


def test_empty_or_missing_members_list_does_not_raise(tmp_path):
    w = _world(tmp_path)
    for ctx in ({"pool_id": POOL, "maintainer_key_id": w["maint"].key_id,
                 "members": []},
                {"pool_id": POOL, "maintainer_key_id": w["maint"].key_id}):
        w["contrib_svc"].record_pool_context(POOL, ctx)
    assert w["cconn"].execute(
        "SELECT COUNT(*) FROM members").fetchone()[0] == 0


def test_empty_verify_in_context_recorded_with_box_only(tmp_path):
    w = _world(tmp_path)
    ctx = _context(w)
    for m in ctx["members"]:
        if m["key_id"] == w["maint"].key_id:
            m["verify_key_hex"] = ""
    w["contrib_svc"].record_pool_context(POOL, ctx)
    row = _member_row(w["cconn"], w["maint"].key_id)
    assert row is not None
    assert row["verify_pubkey_hex"] == ""
    assert row["box_pubkey_hex"] != ""
