"""Regression tests for the version-resolution defects observed live
2026-11-09. Each test fails on unfixed code and passes after the fix.

Defects:
1. ledger.record_submission used INSERT OR REPLACE: a late delivery
   clobbered an already-accepted repo_versions row.
2. A contributor build blind-minted v1 from the repo-less local ledger
   (L.next_repo_version on the contributor conn always yields 1),
   colliding with a pool chain already at v1/v2.
3. The documented ledger-read procedure used immutable=1, which skips
   the WAL and returns stale data on a live database.

Plan: .hermes/plans/2026-11-09_0240-agent-pool-version-resolution-fixes.md
"""
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from identity import ensure_identity
import ledger as L
from ledger import add_member, bind_membership, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport

OWNER = "owner-1"
MEMBER_USER = "member-1"
CONTRIB_USER = "contrib-user"
POOL = "pool-1"
REPO = "demo"


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"],
                   check=True)
    (path / "hello.txt").write_text("hello collision\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"],
                   check=True)
    return path


def _commit_src(src: Path, note: str) -> None:
    (src / "hello.txt").write_text(f"hello {note}\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", note],
                   check=True)


# --- maintainer env (single shared conn, hosted pool) ------------------------

@pytest.fixture
def maint_env(tmp_path):
    """Maintainer gateway + whitelisted member on ONE ledger (hosted pool),
    with a git source dir. Mirrors tests/test_add_repo_semantics.py::world."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex,"
            " created_at) VALUES (?,?,?,0)",
            (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
             ident.box_pk.encode(RawEncoder).hex()))
    conn.commit()
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
    return {"conn": conn, "maint": maint, "member": member,
            "maint_svc": maint_svc, "member_svc": member_svc,
            "src": _git_src(tmp_path / "src")}


def _accept_next(w, note):
    """Build + deliver + approve one chain version on the hosted pool.
    Returns the accepted version number."""
    _commit_src(w["src"], note)
    b = w["member_svc"].add_repo_build_bundle(str(w["src"]), REPO)
    w["maint_svc"].add_repo_deliver(b["manifest"]["transfer_ticket"],
                                    b["manifest"],
                                    source={"user_id": MEMBER_USER})
    out = w["maint_svc"].add_repo_approve(REPO, source={"user_id": OWNER})
    assert out["ok"] is True and out["state"] == "accepted"
    return out["version"]


# --- contributor env (own repo-less ledger, no hosted pool) ------------------

def _contributor_world(tmp_path):
    """Two ledgers: maintainer hosts POOL; contributor hosts none and has
    enrolled (enroll_member binds CONTRIB_USER -> contrib key)."""
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "c")
    transport = FakeTransport()
    mconn = memdb()
    maint_svc = PoolService(mconn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    sync_whitelist(mconn, POOL, [OWNER, CONTRIB_USER])
    enrolled = maint_svc.enroll_member(
        POOL, contrib.key_id,
        contrib.box_pk.encode(RawEncoder).hex(),
        contrib.verify_key.encode(RawEncoder).hex(),
        discord_user_id=CONTRIB_USER,
        source={"user_id": OWNER})
    cconn = memdb()
    contrib_svc = PoolService(cconn, transport, caller_key_id=contrib.key_id,
                              caller_signing_key=contrib.signing_key,
                              caller_box_sk=contrib.box_sk)
    return {"mconn": mconn, "cconn": cconn, "maint": maint,
            "contrib": contrib, "maint_svc": maint_svc,
            "contrib_svc": contrib_svc, "enrolled": enrolled,
            "src": _git_src(tmp_path / "src")}


def _accept_contrib_version(w, version, note):
    """Land one accepted chain version contributed from the contributor
    gateway (explicit version — the contributor ledger is repo-less)."""
    _commit_src(w["src"], note)
    b = w["contrib_svc"].add_repo_build_bundle(
        str(w["src"]), REPO, pool_id=POOL, version=version)
    w["maint_svc"].add_repo_deliver(b["manifest"]["transfer_ticket"],
                                    b["manifest"],
                                    source={"user_id": OWNER})
    out = w["maint_svc"].add_repo_approve(REPO, version=version,
                                          source={"user_id": OWNER})
    assert out["ok"] is True and out["state"] == "accepted"


@pytest.fixture
def contributor_env_no_tip(tmp_path):
    """Contributor gateway with the enrollment context recorded, but the
    context carries NO chain_tips key (Tip unknown)."""
    w = _contributor_world(tmp_path)
    w["contrib_svc"].record_pool_context(POOL, w["enrolled"]["pool_context"])
    return w


@pytest.fixture
def contributor_env_with_pool_chain(tmp_path):
    """Contributor gateway whose pool chain is at v2 (v1+v2 accepted on the
    maintainer ledger). Also carries a stale signed v1 manifest built BEFORE
    the chain existed — the deliver-collision fixture for Tasks 4."""
    w = _contributor_world(tmp_path)
    # Context first (no tips): required for _contributor_propagator to build.
    w["contrib_svc"].record_pool_context(POOL, w["enrolled"]["pool_context"])
    # Stale v1 manifest, built before any chain version exists.
    w["stale_v1"] = w["contrib_svc"].add_repo_build_bundle(
        str(w["src"]), REPO, pool_id=POOL, version=1)
    _accept_contrib_version(w, 1, "chain-v1")
    _accept_contrib_version(w, 2, "chain-v2")
    # Hand-craft the tip-carrying context (Task 5 makes the real
    # pool_context_for carry it; record_pool_context accepts extra keys).
    ctx = dict(w["enrolled"]["pool_context"])
    ctx["chain_tips"] = {REPO: 2}
    w["contrib_svc"].record_pool_context(POOL, ctx)  # replaces the context
    return w


# --- Defect 1: record_submission must never clobber non-submitted rows ------

def test_record_submission_never_overwrites_accepted_row(maint_env):
    conn = maint_env["conn"]
    L.upsert_repo(conn, "r-ow", POOL)
    L.record_submission(conn, "r-ow", 1, "h-orig", "{}")
    conn.execute(
        "UPDATE repo_versions SET state='accepted' WHERE repo_id='r-ow' AND version=1")
    conn.commit()
    with pytest.raises(L.VersionCollisionError):
        L.record_submission(conn, "r-ow", 1, "h-new", "{}")
    row = conn.execute(
        "SELECT state, manifest_hash FROM repo_versions"
        " WHERE repo_id='r-ow' AND version=1").fetchone()
    assert (row["state"], row["manifest_hash"]) == ("accepted", "h-orig")


def test_record_submission_same_hash_idempotent_while_submitted(maint_env):
    conn = maint_env["conn"]
    L.upsert_repo(conn, "r-ow", POOL)
    L.record_submission(conn, "r-ow", 1, "h-orig", "{}")
    L.record_submission(conn, "r-ow", 1, "h-orig", "{}")  # no raise
    rows = conn.execute(
        "SELECT version, state, manifest_hash FROM repo_versions"
        " WHERE repo_id='r-ow'").fetchall()
    assert [tuple(r) for r in rows] == [(1, "submitted", "h-orig")]


def test_record_submission_refuses_submitted_differing_hash(maint_env):
    conn = maint_env["conn"]
    L.upsert_repo(conn, "r-ow", POOL)
    L.record_submission(conn, "r-ow", 1, "h-orig", "{}")
    with pytest.raises(L.VersionCollisionError, match="different manifest hash"):
        L.record_submission(conn, "r-ow", 1, "h-different", "{}")


def test_record_submission_refuses_accepted_version(maint_env):
    conn = maint_env["conn"]
    L.upsert_repo(conn, "r-ow", POOL)
    L.record_submission(conn, "r-ow", 1, "h-orig", "{}")
    conn.execute(
        "UPDATE repo_versions SET state='accepted' WHERE repo_id='r-ow' AND version=1")
    conn.commit()
    with pytest.raises(L.VersionCollisionError, match="already ACCEPTED"):
        L.record_submission(conn, "r-ow", 1, "h-orig", "{}")


# --- Defect 2: contributor build must mint from the pool chain tip ----------

def test_contributor_build_does_not_blind_mint_v1(contributor_env_with_pool_chain):
    w = contributor_env_with_pool_chain
    bundle = w["contrib_svc"].add_repo_build_bundle(str(w["src"]), REPO,
                                                    pool_id=POOL)
    assert bundle["manifest"]["version"] == 3, (
        "contributor minted a version at/below the pool chain tip "
        "(tip is 2; expected tip+1 = 3)")


def test_contributor_build_without_chain_tip_refuses_to_guess(contributor_env_no_tip):
    w = contributor_env_no_tip
    with pytest.raises(ValueError, match="chain tip"):
        w["contrib_svc"].add_repo_build_bundle(str(w["src"]), REPO,
                                               pool_id=POOL)


def test_explicit_version_below_tip_refused(contributor_env_with_pool_chain):
    w = contributor_env_with_pool_chain
    with pytest.raises(L.VersionCollisionError, match="chain tip"):
        w["contrib_svc"].add_repo_build_bundle(str(w["src"]), REPO,
                                               pool_id=POOL, version=2)


def test_explicit_version_above_tip_builds(contributor_env_with_pool_chain):
    w = contributor_env_with_pool_chain
    bundle = w["contrib_svc"].add_repo_build_bundle(str(w["src"]), REPO,
                                                    pool_id=POOL, version=3)
    assert bundle["manifest"]["version"] == 3
    assert bundle["manifest"]["state"] == "submitted"


def test_maintainer_hosted_resolution_unchanged(maint_env):
    """The maintainer-hosted build path (local rows exist) keeps minting
    next_repo_version from the local ledger — untouched by Task 6."""
    w = maint_env
    v1 = w["member_svc"].add_repo_build_bundle(str(w["src"]), REPO)
    assert v1["manifest"]["version"] == 1
    w["maint_svc"].add_repo_deliver(v1["manifest"]["transfer_ticket"],
                                    v1["manifest"],
                                    source={"user_id": MEMBER_USER})
    out0 = w["maint_svc"].add_repo_approve(REPO, source={"user_id": OWNER})
    assert out0["ok"] is True and out0["state"] == "accepted"
    assert _accept_next(w, "second") == 2
    rows = [tuple(r) for r in w["conn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "accepted"), (2, "accepted")]


# --- Deliver-side hard-fail BEFORE the audit --------------------------------

def test_deliver_accepted_version_hard_fails_before_audit(
        contributor_env_with_pool_chain):
    w = contributor_env_with_pool_chain
    stale = w["stale_v1"]["manifest"]
    audits_before = w["mconn"].execute(
        "SELECT COUNT(*) FROM audits").fetchone()[0]
    with pytest.raises(L.VersionCollisionError, match="re-mint"):
        w["maint_svc"].add_repo_deliver(stale["transfer_ticket"], stale,
                                        source={"user_id": CONTRIB_USER})
    audits_after = w["mconn"].execute(
        "SELECT COUNT(*) FROM audits").fetchone()[0]
    assert audits_after == audits_before, "refused delivery ran the audit"
    rows = [tuple(r) for r in w["mconn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "accepted"), (2, "accepted")]
    ev = w["mconn"].execute(
        "SELECT COUNT(*) FROM events WHERE kind='repo_submission_refused'"
        " AND payload_json LIKE '%version_collision%'").fetchone()
    assert ev[0] >= 1


def test_push_deliver_accepted_version_hard_fails_before_audit(
        contributor_env_with_pool_chain):
    w = contributor_env_with_pool_chain
    ticket = w["maint_svc"].add_ticket(POOL, REPO, "work",
                                       source={"user_id": OWNER})
    w["maint_svc"].claim(ticket["ticket_id"], source={"user_id": CONTRIB_USER})
    out = w["maint_svc"].push_repo(REPO, ticket["ticket_id"],
                                   source={"user_id": CONTRIB_USER})
    stale = w["stale_v1"]["manifest"]
    audits_before = w["mconn"].execute(
        "SELECT COUNT(*) FROM audits").fetchone()[0]
    with pytest.raises(L.VersionCollisionError, match="re-mint"):
        w["maint_svc"].push_deliver(stale["transfer_ticket"], out["token"],
                                    stale, source={"user_id": CONTRIB_USER})
    audits_after = w["mconn"].execute(
        "SELECT COUNT(*) FROM audits").fetchone()[0]
    assert audits_after == audits_before, "refused push ran the audit"
    rows = [tuple(r) for r in w["mconn"].execute(
        "SELECT version, state FROM repo_versions ORDER BY version")]
    assert rows == [(1, "accepted"), (2, "accepted")]


# --- Chain tips must reach the contributor ----------------------------------

def test_pool_context_carries_chain_tips(maint_env):
    w = maint_env
    assert _accept_next(w, "first") == 1
    assert _accept_next(w, "second") == 2
    ctx = w["maint_svc"].pool_context_for(POOL)
    assert ctx["chain_tips"] == {REPO: 2}


def test_push_grant_link_carries_next_version(maint_env):
    w = maint_env
    assert _accept_next(w, "first") == 1
    ticket = w["maint_svc"].add_ticket(POOL, REPO, "improve",
                                       source={"user_id": OWNER})
    w["member_svc"].claim(ticket["ticket_id"],
                          source={"user_id": MEMBER_USER})
    out = w["maint_svc"].push_repo(REPO, ticket["ticket_id"],
                                   source={"user_id": MEMBER_USER})
    assert out["push_link"]["next_version"] == 2


def test_record_pool_context_roundtrips_chain_tips(contributor_env_no_tip):
    """Guard: the enrollment context is stored as one JSON blob — an extra
    chain_tips key must survive record -> get unchanged."""
    w = contributor_env_no_tip
    ctx = dict(w["enrolled"]["pool_context"])
    ctx["chain_tips"] = {REPO: 5}
    w["contrib_svc"].record_pool_context(POOL, ctx)
    got = w["contrib_svc"].get_pool_context(POOL)
    assert got["chain_tips"] == {REPO: 5}


# --- Defect 3: no documented immutable=1 read URI ---------------------------

def test_report_read_procedure_never_uses_immutable_uri():
    """The repo-local retrieval report must use plain mode=ro."""
    report = (Path(__file__).resolve().parent.parent
              / ".hermes/reports/discord-session-retrieval-procedure.md")
    text = report.read_text()
    assert not re.search(r"[?&]immutable=1", text), (
        "documented immutable=1 read URI (stale WAL reads) still present")
    assert "mode=ro" in text, "corrected report must use mode=ro URIs"


def test_no_immutable_uri_in_any_documented_read_procedure():
    """Repo-wide guard: no git-tracked markdown may document an
    immutable=1 CONNECT URI. The regex only matches URI contexts
    (?immutable=1 / &immutable=1) so prose like 'never immutable=1'
    (skills/agent-pool/SKILL.md:188) does not trip it."""
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for md in repo_root.rglob("*.md"):
        if ".git" in md.parts or ".hermes" in md.parts:
            continue  # .hermes is gitignored; the report has its own test
        text = md.read_text(errors="replace")
        if re.search(r"[?&]immutable=1", text):
            offenders.append(str(md.relative_to(repo_root)))
    assert offenders == [], f"documented immutable=1 read URIs in: {offenders}"
