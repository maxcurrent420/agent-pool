import copy

import pytest

from crypto import (
    blake3_hash, build_manifest, generate_signing_key, manifest_hash,
    sign_manifest, verify_chain, verify_manifest,
)


def _chain(n=3):
    sk = generate_signing_key()
    key_id = "agent:test"
    manifests, prev, keys = [], None, {key_id: sk.verify_key}
    for i in range(1, n + 1):
        body = build_manifest(
            repo_id="static-echo", version=i,
            content_hash=blake3_hash(f"bundle-{i}".encode()),
            size_bytes=1000 * i, signer_key_id=key_id, submitter_key_id=key_id,
            prev_manifest_hash=prev, state="accepted" if i > 1 else "submitted")
        signed = sign_manifest(sk, body)
        manifests.append(signed)
        prev = manifest_hash(signed)
    return sk, manifests, keys


def test_sign_verify_manifest():
    sk = generate_signing_key()
    body = build_manifest(repo_id="r", version=1, content_hash=blake3_hash(b"x"),
                          size_bytes=1, signer_key_id="agent:a",
                          submitter_key_id="agent:a")
    signed = sign_manifest(sk, body)
    assert verify_manifest(sk.verify_key, signed) is True
    tampered = dict(signed, size_bytes=999)
    assert verify_manifest(sk.verify_key, tampered) is False
    assert verify_manifest(sk.verify_key, {k: v for k, v in signed.items()
                                          if k != "signature"}) is False


def test_verify_chain_ok():
    _, manifests, keys = _chain()
    ok, reason = verify_chain(manifests, keys)
    assert ok is True, reason


def test_chain_tamper_content_detected():
    _, manifests, keys = _chain()
    bad = copy.deepcopy(manifests)
    bad[1]["content_hash"] = blake3_hash(b"evil")
    ok, reason = verify_chain(bad, keys)
    assert ok is False and "signature" in reason


def test_chain_tamper_reorder_detected():
    _, manifests, keys = _chain()
    bad = [manifests[0], manifests[2], manifests[1]]
    ok, reason = verify_chain(bad, keys)
    assert ok is False and "prev_manifest_hash" in reason


def test_chain_tamper_removal_detected():
    _, manifests, keys = _chain()
    bad = [manifests[0], manifests[2]]
    ok, reason = verify_chain(bad, keys)
    assert ok is False and "prev_manifest_hash" in reason


def test_chain_unknown_signer_detected():
    _, manifests, _ = _chain()
    ok, reason = verify_chain(manifests, {"agent:other": generate_signing_key().verify_key})
    assert ok is False and "unknown signer" in reason


def test_wrap_unwrap_per_member():
    from crypto import generate_box_keypair, wrap_dek, unwrap_dek
    dek = b"\x01" * 32
    alice_sk, alice_pk = generate_box_keypair()
    bob_sk, bob_pk = generate_box_keypair()
    wrapped_alice = wrap_dek(alice_pk, dek)
    wrapped_bob = wrap_dek(bob_pk, dek)
    assert unwrap_dek(alice_sk, wrapped_alice) == dek
    assert unwrap_dek(bob_sk, wrapped_bob) == dek
    with pytest.raises(Exception):
        unwrap_dek(bob_sk, wrapped_alice)
