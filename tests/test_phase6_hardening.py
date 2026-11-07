"""Phase 6 hardening test matrix (>=20 named scenarios).

Named scenarios (plan minimum + Phase 6 gaps):
  01 unsigned manifest rejected            17 global freeze blocks claim (2 pools)
  02 unapproved member fetch refused      18 global freeze blocks complete [re-gated]
  03 double-claim                         19 global freeze blocks pool mutation
  04 overlap                              20 global freeze unfreezes cleanly
  06 chain tamper detected                21 legacy accept DEK rotation + unwrap [re-gated]
  07 revocation (forward separation)      22 legacy accept excludes removed member [re-gated]
  09 gateway-abstraction: full loop fake  23 propagator fetch retry (flaky sender)
  10 sender-offline: retry then success   24 coordination payload size guard
  11 sender-offline: bounded failure      25 frozen pool blocks complete [re-gated]
  12 300MB repo (scaled fixture, labelled)

Phase 2 / WO-013 removals (P6 will fully re-gate this file later):
  05 budget kill, 13 budget tracker hard kill — execution.py/BudgetTracker deleted.
  08 conflict bounce — integration gate (integrate/resolve) deleted.
  14/15/16 button bridge — bridge.py/buttons.py no longer wired.
Re-gated notes: submit() is now maintainer-only, so contributor-submission
scenarios were rebuilt around maintainer submit + claim/complete, and the
legacy accept() DEK-rotation scenarios (21/22) now drive accept() on a
submitted manifest instead of a pending contributor submission.

The 300MB scenario is exercised in pytest at scaled size (30MB bundle
over FakeTransport, full submit->fetch->clone path) and at real 300MB
over real send_hermes by scripts/measure_300mb.py (see Phase 6 report
for the measured run). Nothing here claims 300MB crossed Iroh in pytest.
"""
import json
import os
import secrets
import tempfile
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from crypto import (blake3_hash, build_manifest, manifest_hash, sign_manifest,
                    unwrap_dek, verify_chain, wrap_dek)
from ledger import (ClaimRefused, add_member, authorize_user, create_pool, get_ticket,
                    init_ledger, is_global_frozen)
from propagation import _verify_key, encrypt_bundle, pack_bundle
from tickets import PoolService
from transport import (FakeTransport, TransportBackend, TransportUnavailable,
                       fetch_with_retry)
from identity import ensure_identity

OWNER = "owner-1"
CONTRIB = "contrib-1"
MEMBER = "member-1"
SRC = {"user_id": OWNER}
CONTRIB_SRC = {"user_id": CONTRIB}
MEMBER_SRC = {"user_id": MEMBER}


def _git(*args, **kw):
    return subprocess.run(["git", *args], capture_output=True, text=True, **kw)


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


class _World:
    """Maintainer + contributor + member + extra, canonical v1 accepted."""

    def __init__(self, tmp_path):
        self.tmp = Path(tmp_path)
        self.conn = _memdb()
        self.transport = FakeTransport()
        self.maint = ensure_identity(self.tmp / "m")
        self.contrib = ensure_identity(self.tmp / "a")
        self.member = ensure_identity(self.tmp / "b")
        self.extra = ensure_identity(self.tmp / "x")
        create_pool(self.conn, "pool-1", "Team", self.maint.key_id, relay_url="off",
                    owner_discord_user_id=OWNER)
        # First-claim key binding is per Discord user: each service key
        # drives its own whitelisted user.
        authorize_user(self.conn, "pool-1", CONTRIB)
        authorize_user(self.conn, "pool-1", MEMBER)
        self.conn.execute(
            "INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('demo','pool-1','Demo',0)")
        for ident, role in ((self.maint, "maintainer"), (self.contrib, "member"),
                            (self.member, "member"), (self.extra, "member")):
            self.conn.execute(
                "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
                " VALUES (?,?,?,0)",
                (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
                 ident.box_pk.encode(RawEncoder).hex()))
            add_member(self.conn, "pool-1", ident.key_id, role=role,
                       box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
        self.conn.commit()
        self.maint_svc = PoolService(self.conn, self.transport,
                                     caller_key_id=self.maint.key_id,
                                     caller_signing_key=self.maint.signing_key,
                                     caller_box_sk=self.maint.box_sk)
        self.contrib_svc = PoolService(self.conn, self.transport,
                                       caller_key_id=self.contrib.key_id,
                                       caller_signing_key=self.contrib.signing_key,
                                       caller_box_sk=self.contrib.box_sk)
        self.member_svc = PoolService(self.conn, self.transport,
                                      caller_key_id=self.member.key_id,
                                      caller_signing_key=self.member.signing_key,
                                      caller_box_sk=self.member.box_sk)
        self.src = self.tmp / "src"
        self.src.mkdir()
        assert _git("-C", str(self.src), "init", "-q").returncode == 0
        _git("-C", str(self.src), "config", "user.email", "t@t.t")
        _git("-C", str(self.src), "config", "user.name", "T")
        (self.src / "hello.txt").write_text("v1\n")
        _git("-C", str(self.src), "add", ".")
        assert _git("-C", str(self.src), "commit", "-q", "-m", "v1").returncode == 0
        prop = self.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
        result = prop.submit(repo_path=self.src, repo_id="demo", version=1,
                             submitter=prop._members[self.maint.key_id],
                             submitter_signing_key=self.maint.signing_key)
        assert result["state"] == "accepted"
        from ledger import get_latest_accepted_manifest
        self.v1 = get_latest_accepted_manifest(self.conn, "demo")

    def make_ticket(self, title="Work", repo="demo"):
        return self.maint_svc.add_ticket("pool-1", repo, title, brief="Scope: hello.txt", source=SRC)

@pytest.fixture
def world(tmp_path):
    return _World(tmp_path)


def _dek(world, manifest, ident):
    wrapped = manifest["wrapped_keys"][ident.key_id]
    return unwrap_dek(ident.box_sk, bytes.fromhex(wrapped))


def _pending_manifest(world, prop, submitter, signing_key, version=2):
    """Hand-built pending contributor manifest (the pending branch of the
    deleted submit() path, Phase 2 re-gate): pack/encrypt/send/sign, stored
    in repo_versions as 'pending' but NOT appended to the chain. Returns
    {"manifest": signed, "transfer_id": ...} like the old submit result."""
    plaintext = pack_bundle(Path(world.src))
    content_hash = blake3_hash(plaintext)
    dek = secrets.token_bytes(32)
    ciphertext = encrypt_bundle(plaintext, dek)
    wrapped_keys = {}
    for member_id, member in prop._members.items():
        if not member.box_pubkey_hex:
            continue
        from nacl.public import PublicKey
        pk = PublicKey(bytes.fromhex(member.box_pubkey_hex), encoder=RawEncoder)
        wrapped_keys[member_id] = wrap_dek(pk, dek).hex()
    with tempfile.TemporaryDirectory(prefix="agentpool-pending-") as tmp:
        cipher_file = Path(tmp) / "bundle.enc"
        cipher_file.write_bytes(ciphertext)
        sent = prop._transport.send(cipher_file)
    body = build_manifest(
        repo_id="demo", version=version, content_hash=content_hash,
        size_bytes=len(plaintext), signer_key_id=submitter.key_id,
        submitter_key_id=submitter.key_id, wrapped_keys=wrapped_keys,
        transfer_ticket=sent["ticket"],
        prev_manifest_hash=prop._last_chain_hash("demo"), state="pending")
    signed = sign_manifest(signing_key, body)
    world.conn.execute(
        "INSERT INTO repo_versions (repo_id, version, manifest_json,"
        " manifest_hash, state) VALUES (?,?,?,?, 'pending')",
        ("demo", version, json.dumps(signed), manifest_hash(signed)))
    world.conn.commit()
    return {"manifest": signed, "transfer_id": sent["transfer_id"],
            "state": "pending"}


# 01 -----------------------------------------------------------------------
def test_01_unsigned_manifest_rejected(world):
    """A tampered manifest fails signature verification, and a manifest
    whose content_hash does not match the fetched bytes is refused."""
    from crypto import verify_manifest
    bad = dict(world.v1, size_bytes=999999)  # breaks the signature
    prop = world.member_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    assert verify_manifest(_verify_key(prop._members[world.maint.key_id]), bad) is False
    # Same version/ticket (so chain-tip checks pass) but forged content hash:
    # fetch must fail BLAKE3 verification rather than trusting the manifest.
    forged = dict(world.v1, content_hash=blake3_hash(b"forged"))
    with pytest.raises(RuntimeError, match="BLAKE3"):
        prop.fetch(manifest=forged, fetcher=prop._members[world.member.key_id],
                   dest=world.tmp / "forged")


# 02 -----------------------------------------------------------------------
def test_02_unapproved_member_fetch_refused(world):
    """A key with no wrapped_keys entry cannot fetch the accepted version."""
    from propagation import Member
    outsider = Member(key_id="agent:outsider",
                      verify_key_hex=world.maint_svc._members_for_propagator(
                          "pool-1")[world.maint.key_id].verify_key_hex,
                      box_pubkey_hex=world.maint.box_pk.encode(RawEncoder).hex(),
                      box_sk=world.maint.box_sk)
    prop = world.member_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    with pytest.raises(PermissionError):
        prop.fetch(manifest=world.v1, fetcher=outsider, dest=world.tmp / "evil")


# 03 -----------------------------------------------------------------------
def test_03_double_claim(world):
    ticket = world.make_ticket("Double")
    world.contrib_svc.claim(ticket["ticket_id"], declared_paths=["hello.txt"], source=CONTRIB_SRC)
    with pytest.raises(ClaimRefused):
        world.member_svc.claim(ticket["ticket_id"], declared_paths=["other.txt"], source=MEMBER_SRC)


# 04 -----------------------------------------------------------------------
def test_04_overlap(world):
    t1 = world.make_ticket("First")
    t2 = world.make_ticket("Second")
    world.contrib_svc.claim(t1["ticket_id"], declared_paths=["hello.txt"], source=CONTRIB_SRC)
    with pytest.raises(ClaimRefused, match="overlap"):
        world.member_svc.claim(t2["ticket_id"], declared_paths=["hello.txt"], source=MEMBER_SRC)


# 06 -----------------------------------------------------------------------
def test_06_chain_tamper_detected(world):
    chain = [json.loads(r[0]) for r in world.conn.execute(
        "SELECT manifest_json FROM repo_versions WHERE state='accepted' ORDER BY version")]
    prop = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    vk = {k: _verify_key(v) for k, v in prop._members.items()}
    ok, _ = verify_chain(chain, vk)
    assert ok
    tampered = [dict(chain[0], size_bytes=1)]
    ok, reason = verify_chain(tampered, vk)
    assert not ok and "signature" in reason


# 07 -----------------------------------------------------------------------
def test_07_revocation_forward_separation(world):
    """Removed member keeps the old version's key but gets none on new ones.

    Phase 2 re-gate: submit() is maintainer-only and the integrate/resolve
    gate is deleted, so the new version is a maintainer submit after removal."""
    assert world.extra.key_id in world.v1["wrapped_keys"]
    world.maint_svc.remove_member("pool-1", world.extra.key_id, source=SRC)
    prop = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    assert world.extra.key_id not in prop._members
    res = prop.submit(repo_path=world.src, repo_id="demo", version=2,
                      submitter=prop._members[world.maint.key_id],
                      submitter_signing_key=world.maint.signing_key)
    assert res["state"] == "accepted"
    from ledger import get_latest_accepted_manifest
    new = get_latest_accepted_manifest(world.conn, "demo")
    assert world.extra.key_id not in new["wrapped_keys"]
    assert world.extra.key_id in world.v1["wrapped_keys"]


# 09 -----------------------------------------------------------------------
def test_09_gateway_abstraction_full_loop_fake(world):
    """Full flow (ticket->claim->complete->submit->audit) runs end-to-end
    (Phase 2: the integrate/resolve gate is deleted, so completion is via
    complete()) and no coordination payload exceeds the 4KB guard
    (no repo bytes on gateway)."""
    ticket = world.make_ticket("Full loop")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"], source=CONTRIB_SRC)
    done = world.contrib_svc.complete(tid, source=CONTRIB_SRC)
    assert done["state"] == "completed"
    prop = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    res = prop.submit(repo_path=world.src, repo_id="demo", version=2,
                      submitter=prop._members[world.maint.key_id],
                      submitter_signing_key=world.maint.signing_key)
    assert res["state"] == "accepted"
    aud = prop.audit(manifest=res["manifest"],
                     maintainer=prop._members[world.maint.key_id])
    assert aud["report"]["verdict"] == "clean"
    shown = world.maint_svc.audit_show(ticket_id=tid)
    assert shown["verdict"] == "clean"
    # Gateway-native: notices are returned strings, not adapter calls.
    assert done["notice"]
    for n in world.maint_svc.peek_notices() or []:
        assert len(n.encode()) <= 4096


# 10 -----------------------------------------------------------------------
class _FlakyTransport(FakeTransport):
    """Fails the first N fetches (sender briefly offline), then succeeds."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.fetch_calls = 0

    def fetch(self, ticket, dest):
        self.fetch_calls += 1
        if self.fetch_calls <= self.failures:
            raise RuntimeError("sender offline (simulated)")
        return super().fetch(ticket, dest)


def test_10_sender_offline_retry_then_success(tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"x" * 1000)
    transport = _FlakyTransport(failures=2)
    sent = transport.send(src)
    dest = tmp_path / "out"
    dest.mkdir()
    delays = []
    out = fetch_with_retry(transport, sent["ticket"], dest,
                           max_attempts=3, sleep=delays.append)
    assert out.read_bytes() == b"x" * 1000
    assert transport.fetch_calls == 3
    assert delays == [0.5, 1.0]  # exponential backoff, bounded


# 11 -----------------------------------------------------------------------
def test_11_sender_offline_bounded_failure(tmp_path):
    src = tmp_path / "payload.bin"
    src.write_bytes(b"y")
    transport = _FlakyTransport(failures=99)
    sent = transport.send(src)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(TransportUnavailable) as exc:
        fetch_with_retry(transport, sent["ticket"], dest,
                         max_attempts=3, sleep=lambda _d: None)
    assert exc.value.attempts == 3
    assert transport.fetch_calls == 3  # bounded: no unbounded loop
    assert "sender offline" in str(exc.value.last_error)


# 12 -----------------------------------------------------------------------
def test_12_large_repo_scaled_fixture(world):
    """SCALED 300MB-scenario fixture (labelled): a repo whose bundle is
    ~30MB of incompressible data exercises the full submit->fetch->clone
    path over FakeTransport. The real 300MB run over send_hermes is
    scripts/measure_300mb.py (measured result in the Phase 6 report)."""
    big = world.tmp / "big-src"
    big.mkdir()
    _git("-C", str(big), "init", "-q")
    _git("-C", str(big), "config", "user.email", "t@t.t")
    _git("-C", str(big), "config", "user.name", "T")
    (big / "data.bin").write_bytes(os.urandom(30 * 1024 * 1024))
    (big / "hello.txt").write_text("big\n")
    _git("-C", str(big), "add", ".")
    assert _git("-C", str(big), "commit", "-q", "-m", "big").returncode == 0
    from propagation import pack_bundle
    bundle = pack_bundle(big)
    assert len(bundle) >= 29 * 1024 * 1024, f"scaled fixture too small: {len(bundle)}"
    prop = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    world.conn.execute(
        "INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('big','pool-1','Big',0)")
    world.conn.commit()
    res = prop.submit(repo_path=big, repo_id="big", version=1,
                      submitter=prop._members[world.maint.key_id],
                      submitter_signing_key=world.maint.signing_key)
    assert res["state"] == "accepted"
    mprop = world.member_svc._propagator({"pool_id": "pool-1", "repo_id": "big"})
    out = mprop.fetch(manifest=res["manifest"],
                      fetcher=mprop._members[world.member.key_id],
                      dest=world.tmp / "big-fetch")
    fetched = (Path(out["clone_dir"]) / "data.bin").read_bytes()
    assert len(fetched) == 30 * 1024 * 1024
    assert blake3_hash(fetched) == blake3_hash((big / "data.bin").read_bytes())


# 17 -----------------------------------------------------------------------
def test_17_global_freeze_blocks_claim_across_pools(world):
    # Phase 1: one pool per gateway — a second hosted pool is refused.
    with pytest.raises(ValueError, match="one pool per gateway"):
        create_pool(world.conn, "pool-2", "Other", world.maint.key_id)
    t1 = world.make_ticket("Pool one")
    t2 = world.make_ticket("Pool one again")
    world.maint_svc.freeze_global(True, source=SRC)
    assert is_global_frozen(world.conn) is True
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.contrib_svc.claim(t1["ticket_id"], declared_paths=["hello.txt"], source=CONTRIB_SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.contrib_svc.claim(t2["ticket_id"], declared_paths=["hello.txt"], source=CONTRIB_SRC)


# 18 -----------------------------------------------------------------------
def test_18_global_freeze_blocks_work_and_integrate(world):
    """Phase 2 re-gate: work_submit/integrate/resolve are deleted; the
    frozen pool now blocks claim and complete."""
    ticket = world.make_ticket("Frozen work")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"], source=CONTRIB_SRC)
    other = world.make_ticket("Frozen other")
    world.maint_svc.freeze_global(True, source=SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.contrib_svc.claim(other["ticket_id"], declared_paths=["hello.txt"], source=CONTRIB_SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.contrib_svc.complete(tid, source=CONTRIB_SRC)
    assert get_ticket(world.conn, tid)["state"] == "claimed"  # untouched


# 19 -----------------------------------------------------------------------
def test_19_global_freeze_blocks_pool_mutation(world):
    world.maint_svc.freeze_global(True, source=SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.maint_svc.add_ticket("pool-1", "demo", "Nope", source=SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.maint_svc.add_ticket("pool-1", "demo", "Nope again", source=SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.maint_svc.register_pool("pool-3", "Late", source=SRC)
    with pytest.raises(ClaimRefused, match="pool frozen"):
        world.maint_svc.remove_member("pool-1", world.extra.key_id, source=SRC)


# 20 -----------------------------------------------------------------------
def test_20_global_freeze_unfreezes_cleanly(world):
    ticket = world.make_ticket("Unfreeze")
    world.maint_svc.freeze_global(True, source=SRC)
    world.maint_svc.freeze_global(False, source=SRC)
    assert is_global_frozen(world.conn) is False
    assert world.maint_svc.is_frozen("pool-1") is False
    world.contrib_svc.claim(ticket["ticket_id"], declared_paths=["hello.txt"], source=CONTRIB_SRC)
    assert get_ticket(world.conn, ticket["ticket_id"])["state"] == "claimed"


# 21 -----------------------------------------------------------------------
def test_21_legacy_accept_rotates_dek(world):
    """Phase 6 gap 1a, Phase 2 re-gate: RepoPropagator.accept() must rotate
    the DEK. submit() is now maintainer-only, so the pending contributor
    manifest is hand-built by _pending_manifest() (deleted submit path)."""
    prop = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    res = _pending_manifest(world, prop, prop._members[world.contrib.key_id],
                            world.contrib.signing_key)
    assert res["state"] == "pending"
    pending_dek = _dek(world, res["manifest"], world.member)
    aud = prop.audit(manifest=res["manifest"],
                     maintainer=prop._members[world.maint.key_id])
    acc = prop.accept(manifest=res["manifest"], report_hash=aud["report_hash"],
                      maintainer=prop._members[world.maint.key_id],
                      maintainer_signing_key=world.maint.signing_key)
    assert acc["dek_rotated"] is True
    accepted_dek = _dek(world, acc["manifest"], world.member)
    assert accepted_dek != pending_dek, "accepted DEK must differ from pending DEK"
    assert acc["manifest"]["transfer_ticket"] != res["manifest"]["transfer_ticket"]
    assert acc["manifest"]["content_hash"] == res["manifest"]["content_hash"]
    # Both unwrap for an authorized member; the accepted one fetches.
    mprop = world.member_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    out = mprop.fetch(manifest=acc["manifest"],
                      fetcher=mprop._members[world.member.key_id],
                      dest=world.tmp / "fetch-rotated")
    assert (Path(out["clone_dir"]) / "hello.txt").read_text() == "v1\n"


# 22 -----------------------------------------------------------------------
def test_22_legacy_accept_excludes_removed_member(world):
    """A member removed before acceptance gets no wrapped key on the
    accepted manifest, even though the submitted one wrapped for them.
    Phase 2 re-gate: pending manifest hand-built (see test_21)."""
    prop = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    res = _pending_manifest(world, prop, prop._members[world.contrib.key_id],
                            world.contrib.signing_key)
    assert res["state"] == "pending"
    assert world.extra.key_id in res["manifest"]["wrapped_keys"]
    world.maint_svc.remove_member("pool-1", world.extra.key_id, source=SRC)
    # Rebuild the propagator AFTER removal (members come from the registry).
    prop2 = world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    assert world.extra.key_id not in prop2._members
    aud = prop2.audit(manifest=res["manifest"],
                      maintainer=prop2._members[world.maint.key_id])
    acc = prop2.accept(manifest=res["manifest"], report_hash=aud["report_hash"],
                       maintainer=prop2._members[world.maint.key_id],
                       maintainer_signing_key=world.maint.signing_key)
    assert world.extra.key_id not in acc["manifest"]["wrapped_keys"]
    assert world.member.key_id in acc["manifest"]["wrapped_keys"]


# 23 -----------------------------------------------------------------------
def test_23_propagator_fetch_retry_flaky_sender(world):
    """prop.fetch rides fetch_with_retry: a sender that drops twice then
    recovers still delivers the bundle (bounded retries in the real path)."""
    flaky = _FlakyTransport(failures=2)
    svc = PoolService(world.conn, flaky,
                      caller_key_id=world.maint.key_id,
                      caller_signing_key=world.maint.signing_key,
                      caller_box_sk=world.maint.box_sk)
    msvc = PoolService(world.conn, flaky,
                       caller_key_id=world.member.key_id,
                       caller_signing_key=world.member.signing_key,
                       caller_box_sk=world.member.box_sk)
    prop = svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    # Re-send the accepted bundle through the flaky transport so its ticket
    # is known to it: easiest is a fresh self-accepted submit via flaky.
    res = prop.submit(repo_path=world.src, repo_id="demo", version=2,
                      submitter=prop._members[world.maint.key_id],
                      submitter_signing_key=world.maint.signing_key)
    assert res["state"] == "accepted"
    mprop = msvc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    out = mprop.fetch(manifest=res["manifest"],
                      fetcher=mprop._members[world.member.key_id],
                      dest=world.tmp / "flaky-fetch")
    assert (Path(out["clone_dir"]) / "hello.txt").read_text() == "v1\n"
    assert flaky.fetch_calls >= 3  # 2 failures + 1 success, all retried


# 24 -----------------------------------------------------------------------
def test_24_coordination_payload_size_guard(world):
    """Gateway payloads stay under 4KB: the fake adapter refuses oversize."""
    with pytest.raises(ValueError, match="exceeds"):
        from replies import check_content_size
        check_content_size({"text": "x" * 5000})


# 25 -----------------------------------------------------------------------
def test_25_per_pool_freeze_blocks_integrate(world):
    """Phase 2 re-gate: integrate() is deleted; a per-pool freeze now
    blocks complete() (complete() checks frozen)."""
    ticket = world.make_ticket("Per-pool freeze")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"], source=CONTRIB_SRC)
    world.maint_svc.freeze("pool-1", True, source=SRC)
    with pytest.raises(ClaimRefused, match="frozen"):
        world.contrib_svc.complete(tid, source=CONTRIB_SRC)
    world.maint_svc.freeze("pool-1", False, source=SRC)
    done = world.contrib_svc.complete(tid, source=CONTRIB_SRC)
    assert done["state"] == "completed"
