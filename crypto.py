"""Crypto helpers for Agent Pool: ed25519 sign/verify, DEK wrap per recipient pubkey (sealed box), BLAKE3.

Phase 1 extension: canonical manifest signing and append-only chain
verification via ``prev_manifest_hash`` (tamper-evident pool log).
"""
import json

import blake3 as _blake3
from nacl.signing import SigningKey, VerifyKey
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.encoding import RawEncoder


def generate_signing_key() -> SigningKey:
    return SigningKey.generate()


def sign(key: SigningKey, message: bytes) -> bytes:
    return key.sign(message).signature


def verify(vk: VerifyKey, message: bytes, signature: bytes) -> bool:
    try:
        vk.verify(message, signature)
        return True
    except Exception:
        return False


def generate_box_keypair():
    sk = PrivateKey.generate()
    return sk, sk.public_key


def wrap_dek(recipient_pk: PublicKey, dek: bytes) -> bytes:
    """Wrap a DEK for a recipient using a sealed box (anonymous sender)."""
    return SealedBox(recipient_pk).encrypt(dek)


def unwrap_dek(recipient_sk: PrivateKey, wrapped: bytes) -> bytes:
    return SealedBox(recipient_sk).decrypt(wrapped)


def blake3_hash(data: bytes) -> str:
    return _blake3.blake3(data).hexdigest()


def blake3_verify(data: bytes, expected_hex: str) -> bool:
    return _blake3.blake3(data).hexdigest() == expected_hex


# --- Manifest signing + chain (Phase 1) ------------------------------------

GENESIS_PREV = None  # first manifest in a chain has no predecessor


def canonical_manifest_bytes(manifest: dict) -> bytes:
    """Canonical JSON of a manifest with the ``signature`` field excluded."""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def manifest_hash(manifest: dict) -> str:
    """BLAKE3 hex digest over the canonical manifest bytes (signature excluded)."""
    return blake3_hash(canonical_manifest_bytes(manifest))


def sign_manifest(signing_key: SigningKey, manifest: dict) -> dict:
    """Return a copy of ``manifest`` with a hex ``signature`` over its canonical bytes."""
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    signed["signature"] = sign(signing_key, canonical_manifest_bytes(signed)).hex()
    return signed


def verify_manifest(verify_key: VerifyKey, signed_manifest: dict) -> bool:
    sig_hex = signed_manifest.get("signature")
    if not sig_hex:
        return False
    try:
        signature = bytes.fromhex(sig_hex)
    except (TypeError, ValueError):
        return False
    return verify(verify_key, canonical_manifest_bytes(signed_manifest), signature)


def verify_chain(signed_manifests, verify_keys) -> tuple:
    """Verify signature + ``prev_manifest_hash`` linkage of an ordered chain.

    ``verify_keys`` maps ``signer_key_id`` -> ``VerifyKey``. Returns
    ``(True, "ok")`` or ``(False, reason)``. Detects content tampering
    (signature mismatch), reordering/removal (broken prev-hash link), and
    unknown signers.
    """
    prev_hash = GENESIS_PREV
    for i, manifest in enumerate(signed_manifests):
        signer = manifest.get("signer_key_id")
        vk = verify_keys.get(signer)
        if vk is None:
            return False, f"manifest {i}: unknown signer {signer!r}"
        if manifest.get("prev_manifest_hash") != prev_hash:
            return False, f"manifest {i}: prev_manifest_hash mismatch"
        if not verify_manifest(vk, manifest):
            return False, f"manifest {i}: signature verification failed"
        prev_hash = manifest_hash(manifest)
    return True, "ok"


def build_proposal(kind: str, payload: dict, proposer_key_id: str,
                   signing_key: SigningKey) -> dict:
    body = {
        "type": "proposal",
        "kind": kind,
        "payload": dict(payload or {}),
        "proposer_key_id": proposer_key_id,
        "signer_key_id": proposer_key_id,
        "prev_manifest_hash": None,
    }
    return sign_manifest(signing_key, body)


def verify_proposal(verify_key: VerifyKey, packet: dict) -> bool:
    if not isinstance(packet, dict) or packet.get("type") != "proposal":
        return False
    return verify_manifest(verify_key, packet)


def proposal_packet_string(packet: dict) -> str:
    return json.dumps(packet, sort_keys=True, separators=(",", ":"))


def parse_proposal(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("type") != "proposal":
        raise ValueError("not a proposal packet")
    return data


def build_manifest(*, repo_id: str, version: int, content_hash: str,
                   size_bytes: int, signer_key_id: str, submitter_key_id: str,
                   wrapped_keys=None, transfer_ticket: str = "",
                   audit_report_hash: str = "", prev_manifest_hash=None,
                   state: str = "accepted") -> dict:
    """Construct an unsigned manifest body (sign with :func:`sign_manifest`)."""
    return {
        "type": "repo_manifest",
        "repo_id": repo_id,
        "version": version,
        "state": state,
        "content_hash": content_hash,
        "size_bytes": size_bytes,
        "signer_key_id": signer_key_id,
        "submitter_key_id": submitter_key_id,
        "wrapped_keys": dict(wrapped_keys or {}),
        "transfer_ticket": transfer_ticket,
        "audit_report_hash": audit_report_hash,
        "prev_manifest_hash": prev_manifest_hash,
    }
