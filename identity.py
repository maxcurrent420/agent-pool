"""Local identity: keypairs generated once at install, persisted under the
plugin data dir with 0600 permissions. Private keys never leave this file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from nacl.encoding import RawEncoder
from nacl.public import PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey

try:  # package import (Hermes loads plugins as a package)
    from .crypto import blake3_hash, generate_box_keypair, generate_signing_key
except ImportError:  # flat import (pytest from repo root)
    from crypto import blake3_hash, generate_box_keypair, generate_signing_key

IDENTITY_FILENAME = "identity.json"


@dataclass
class Identity:
    signing_key: SigningKey
    box_sk: PrivateKey

    @property
    def verify_key(self) -> VerifyKey:
        return self.signing_key.verify_key

    @property
    def box_pk(self) -> PublicKey:
        return self.box_sk.public_key

    @property
    def key_id(self) -> str:
        return "agent:" + blake3_hash(self.verify_key.encode(RawEncoder))[:16]

    @property
    def box_key_id(self) -> str:
        return "box:" + blake3_hash(self.box_pk.encode(RawEncoder))[:16]


def identity_path(data_dir) -> Path:
    return Path(data_dir) / IDENTITY_FILENAME


def load_identity(data_dir) -> Identity:
    data = json.loads(identity_path(data_dir).read_text())
    return Identity(
        signing_key=SigningKey(bytes.fromhex(data["signing_sk_hex"])),
        box_sk=PrivateKey(bytes.fromhex(data["box_sk_hex"])),
    )


def ensure_identity(data_dir) -> Identity:
    """Load existing identity or generate + persist a fresh one (install step)."""
    path = identity_path(data_dir)
    if path.exists():
        return load_identity(data_dir)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    identity = Identity(signing_key=generate_signing_key(),
                        box_sk=generate_box_keypair()[0])
    payload = {
        "key_id": identity.key_id,
        "box_key_id": identity.box_key_id,
        "verify_key_hex": identity.verify_key.encode(RawEncoder).hex(),
        "box_pubkey_hex": identity.box_pk.encode(RawEncoder).hex(),
        "signing_sk_hex": identity.signing_key.encode().hex(),
        "box_sk_hex": identity.box_sk.encode().hex(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return identity
