import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crypto import (
    generate_signing_key, sign, verify,
    generate_box_keypair, wrap_dek, unwrap_dek,
    blake3_hash, blake3_verify,
)


def test_ed25519_roundtrip():
    sk = generate_signing_key()
    vk = sk.verify_key
    msg = b"hello agent pool"
    sig = sign(sk, msg)
    assert verify(vk, msg, sig) is True


def test_ed25519_wrong_message_fails():
    sk = generate_signing_key()
    vk = sk.verify_key
    sig = sign(sk, b"original")
    assert verify(vk, b"tampered", sig) is False


def test_dek_wrap_unwrap():
    recipient_sk, recipient_pk = generate_box_keypair()
    dek = os.urandom(32)
    wrapped = wrap_dek(recipient_pk, dek)
    assert unwrap_dek(recipient_sk, wrapped) == dek


def test_dek_wrong_key_unwrap_fails():
    _, recipient_pk = generate_box_keypair()
    wrong_sk, _ = generate_box_keypair()
    dek = os.urandom(32)
    wrapped = wrap_dek(recipient_pk, dek)
    try:
        unwrap_dek(wrong_sk, wrapped)
        failed = False
    except Exception:
        failed = True
    assert failed is True


def test_blake3_roundtrip():
    data = b"agent pool payload bytes"
    h = blake3_hash(data)
    assert blake3_verify(data, h) is True
    assert blake3_verify(b"other", h) is False