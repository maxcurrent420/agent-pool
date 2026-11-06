"""Phase 2 propagation tests: submit/audit/accept/fetch + ACL."""
import json
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder
sys.path.insert(0, str(Path(__file__).parent.parent))

from crypto import (blake3_hash, build_manifest, generate_signing_key,
                    manifest_hash, sign_manifest, verify_chain, verify_manifest,
                    wrap_dek, unwrap_dek)
from ledger import add_member, append_manifest, create_pool, init_ledger, record_event
from propagation import RepoPropagator, pack_bundle, encrypt_bundle, decrypt_bundle
from propagation import _sandbox_scan
from transport import FakeTransport
from identity import ensure_identity


def _git(*args, **kw):
    return subprocess.run(["git", *args], capture_output=True, text=True, **kw)


@pytest.fixture
def world(tmp_path):
    """Three members on one host (maintainer M, contributor A, fetcher B)."""
    conn = init_ledger(sqlite3.connect(":memory:"))
    create_pool(conn, "pool-1", "Team", "agent:m-maint", relay_url="off")
    conn.execute("INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('demo','pool-1','Demo',0)")
    conn.commit()

    identities = {}
    for name in ("m-maint", "a-contrib", "b-member", "outsider"):
        ident = ensure_identity(tmp_path / name)
        identities[name] = ident

    from propagation import Member
    m_sk = identities["m-maint"].signing_key
    members = {}
    for name, ident in identities.items():
        members[ident.key_id] = Member(
            key_id=ident.key_id,
            role="maintainer" if name == "m-maint" else "member",
            verify_key_hex=ident.verify_key.encode(RawEncoder).hex(),
            box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
            box_sk=ident.box_sk,
        )
        # Only the three pool members are added; outsider stays outside.
        if name != "outsider":
            add_member(conn, "pool-1", ident.key_id,
                       role="maintainer" if name == "m-maint" else "member",
                       box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())

    # A real git repo to pack.
    src = tmp_path / "src"
    src.mkdir()
    assert _git("-C", str(src), "init", "-q").returncode == 0
    _git("-C", str(src), "config", "user.email", "t@t.t")
    _git("-C", str(src), "config", "user.name", "T")
    (src / "hello.txt").write_text("hello agent pool\n")
    _git("-C", str(src), "add", ".")
    assert _git("-C", str(src), "commit", "-q", "-m", "v1").returncode == 0

    transport = FakeTransport()
    prop = RepoPropagator(
        conn, transport,
        pool_id="pool-1",
        maintainer_key_id=identities["m-maint"].key_id,
        maintainer_signing_key=m_sk,
        members={k: v for k, v in members.items() if k != identities["outsider"].key_id},
    )
    return {
        "conn": conn, "src": src, "identities": identities, "members": members,
        "transport": transport, "prop": prop, "tmp": tmp_path,
        "maint_id": identities["m-maint"].key_id,
        "contrib_id": identities["a-contrib"].key_id,
        "member_id": identities["b-member"].key_id,
        "outsider_id": identities["outsider"].key_id,
    }


def _manual_push_manifest(world):
    """Build a contributor push manifest via build_push_bundle (push path).

    The bundle is a transient contributor-signed body (state=submitted),
    never persisted; the deliver gate verifies it at audit/accept time.
    """
    return world["prop"].build_push_bundle(
        repo_path=world["src"], repo_id="demo", version=1,
        pusher=world["members"][world["contrib_id"]],
        pusher_signing_key=world["identities"]["a-contrib"].signing_key)["manifest"]


def test_pack_hash_encrypt_manifest(world):
    bundle = pack_bundle(world["src"])
    assert bundle and len(bundle) > 100
    digest = blake3_hash(bundle)
    dek = b"\x07" * 32
    assert decrypt_bundle(encrypt_bundle(bundle, dek), dek) == bundle
    body = build_manifest(repo_id="demo", version=1, content_hash=digest,
                          size_bytes=len(bundle), signer_key_id="agent:x",
                          submitter_key_id="agent:x")
    sk = generate_signing_key()
    signed = sign_manifest(sk, body)
    assert verify_manifest(sk.verify_key, signed) is True


def test_maintainer_self_accept(world):
    res = world["prop"].submit(
        repo_path=world["src"], repo_id="demo", version=1,
        submitter=world["members"][world["maint_id"]],
        submitter_signing_key=world["identities"]["m-maint"].signing_key)
    assert res["state"] == "accepted"
    chain = world["conn"].execute("SELECT version, state FROM repo_versions").fetchall()
    assert chain == [(1, "accepted")]
    assert res["notice"] and "latest accepted" in res["notice"]
    assert res["manifest"]["state"] == "accepted"


def test_contributor_submit_refused(world):
    """Contributor submit is maintainer-only; nothing is persisted."""
    with pytest.raises(PermissionError, match="maintainer-only"):
        world["prop"].submit(
            repo_path=world["src"], repo_id="demo", version=1,
            submitter=world["members"][world["contrib_id"]],
            submitter_signing_key=world["identities"]["a-contrib"].signing_key)
    assert world["conn"].execute("SELECT COUNT(*) FROM repo_versions").fetchone()[0] == 0
    assert world["conn"].execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 0


def test_contributor_push_audit_accept_chain(world):
    # Contributor submit is maintainer-only; pushes go via build_push_bundle.
    with pytest.raises(PermissionError, match="maintainer-only"):
        world["prop"].submit(
            repo_path=world["src"], repo_id="demo", version=1,
            submitter=world["members"][world["contrib_id"]],
            submitter_signing_key=world["identities"]["a-contrib"].signing_key)
    # Build the push manifest (push-repo path).
    manifest = _manual_push_manifest(world)
    assert manifest["state"] == "submitted"
    # chain NOT appended for push submissions (transient body, no ledger write)
    assert world["conn"].execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 0
    assert world["conn"].execute("SELECT COUNT(*) FROM repo_versions").fetchone()[0] == 0

    aud = world["prop"].audit(
        manifest=manifest, maintainer=world["members"][world["maint_id"]])
    assert aud["report"]["verdict"] == "clean"
    assert "bwrap" in aud["report"]["sandbox"] or "fallback" in aud["report"]["sandbox"]
    assert aud["report_hash"] == blake3_hash(json.dumps(aud["report"], sort_keys=True).encode())

    acc = world["prop"].accept(
        manifest=manifest, report_hash=aud["report_hash"],
        maintainer=world["members"][world["maint_id"]],
        maintainer_signing_key=world["identities"]["m-maint"].signing_key)
    assert acc["manifest"]["state"] == "accepted"
    assert acc["manifest"]["signer_key_id"] == world["maint_id"]
    assert world["conn"].execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 1
    # full chain verifies
    chain = [json.loads(r[0]) for r in world["conn"].execute(
        "SELECT manifest_json FROM repo_versions WHERE state='accepted' ORDER BY version").fetchall()]
    from propagation import _verify_key
    vk_map = {k: _verify_key(v) for k, v in world["members"].items()}
    ok, reason = verify_chain(chain, vk_map)
    assert ok, reason


def test_fetch_happy_path_maintainer_and_member(world):
    res = world["prop"].submit(
        repo_path=world["src"], repo_id="demo", version=1,
        submitter=world["members"][world["maint_id"]],
        submitter_signing_key=world["identities"]["m-maint"].signing_key)
    transfer_id = res["transfer_id"]
    assert world["transport"].provider_alive(transfer_id) is True  # alive until fetch done
    out = world["prop"].fetch(manifest=res["manifest"],
                               fetcher=world["members"][world["member_id"]],
                               dest=world["tmp"] / "fetch-b")
    assert Path(out["clone_dir"], "hello.txt").read_text() == "hello agent pool\n"
    world["transport"].stop(transfer_id)
    assert world["transport"].provider_alive(transfer_id) is False


def test_acl_nonmember_submit_refused(world):
    from propagation import Member
    outsider = Member(key_id=world["outsider_id"],
                      verify_key_hex=world["members"][world["maint_id"]].verify_key_hex,
                      box_pubkey_hex=world["identities"]["outsider"].box_pk.encode(RawEncoder).hex(),
                      box_sk=world["identities"]["outsider"].box_sk)
    with pytest.raises(PermissionError):
        world["prop"].submit(repo_path=world["src"], repo_id="demo", version=1,
                             submitter=outsider,
                             submitter_signing_key=world["identities"]["outsider"].signing_key)


def test_acl_fetch_without_wrapped_key_refused(world):
    from propagation import Member
    from crypto import generate_box_keypair
    res = world["prop"].submit(
        repo_path=world["src"], repo_id="demo", version=1,
        submitter=world["members"][world["maint_id"]],
        submitter_signing_key=world["identities"]["m-maint"].signing_key)
    # Outsider is not a member and has no wrapped_keys entry.
    outsider = Member(
        key_id=world["outsider_id"],
        verify_key_hex=world["members"][world["maint_id"]].verify_key_hex,
        box_pubkey_hex=world["identities"]["outsider"].box_pk.encode(RawEncoder).hex(),
        box_sk=world["identities"]["outsider"].box_sk)
    # Temporarily add outsider to members so only the wrapped-keys check blocks them.
    world["prop"]._members[world["outsider_id"]] = outsider
    with pytest.raises(PermissionError, match="wrapped DEK"):
        world["prop"].fetch(manifest=res["manifest"], fetcher=outsider,
                             dest=world["tmp"] / "evil")


def test_contributor_signature_not_accepted(world):
    """A contributor-signed manifest masquerading as state=accepted must be rejected."""
    sk = world["identities"]["a-contrib"].signing_key
    body = build_manifest(repo_id="demo", version=1,
                          content_hash=blake3_hash(b"x"), size_bytes=1,
                          signer_key_id=world["contrib_id"],
                          submitter_key_id=world["contrib_id"],
                          state="accepted", prev_manifest_hash=None)
    signed = sign_manifest(sk, body)
    # Seed the ledger directly to simulate a forged accepted entry.
    world["conn"].execute(
        "INSERT INTO repo_versions (repo_id, version, manifest_json, manifest_hash, state) VALUES (?,?,?,?,?)",
        ("demo", 1, json.dumps(signed), manifest_hash(signed), "accepted"))
    world["conn"].commit()
    # fetch must refuse: chain tip is not maintainer-signed.
    with pytest.raises(RuntimeError, match="non-maintainer"):
        world["prop"].fetch(manifest=signed,
                             fetcher=world["members"][world["member_id"]],
                             dest=world["tmp"] / "forged")


def test_chain_tamper_detected(world):
    res = world["prop"].submit(
        repo_path=world["src"], repo_id="demo", version=1,
        submitter=world["members"][world["maint_id"]],
        submitter_signing_key=world["identities"]["m-maint"].signing_key)
    manifest = res["manifest"]
    bad = dict(manifest, size_bytes=12345)
    from propagation import _verify_key
    assert verify_manifest(_verify_key(world["members"][world["maint_id"]]), bad) is False
    # Chain tamper via content edit breaks verify_chain.
    manifests = [manifest, dict(manifest, version=2)]
    from propagation import _verify_key as vk
    ok, reason = verify_chain(
        [manifest, dict(manifest, version=2, prev_manifest_hash=None)],
        {world["maint_id"]: vk(world["members"][world["maint_id"]])})
    assert ok is False


def test_blake3_mismatch_on_fetch(world):
    """Corrupt the stored ciphertext: fetch must fail BLAKE3 verification."""
    res = world["prop"].submit(
        repo_path=world["src"], repo_id="demo", version=1,
        submitter=world["members"][world["maint_id"]],
        submitter_signing_key=world["identities"]["m-maint"].signing_key)
    # Flip a byte in the fake transport store.
    ticket = res["manifest"]["transfer_ticket"]
    blob = bytearray(world["transport"]._store[ticket])
    blob[0] ^= 0xFF
    world["transport"]._store[ticket] = bytes(blob)
    with pytest.raises(Exception):
        world["prop"].fetch(manifest=res["manifest"],
                             fetcher=world["members"][world["member_id"]],
                             dest=world["tmp"] / "corrupt")
