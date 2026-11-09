"""Cache-first reuse of the persisted, Tor-fetched, BLAKE3-verified bundle.

``_decrypt_for_maintainer`` must prefer the persisted
``bundle-v{version}.git`` in the pool checkout dir (strict BLAKE3 +
size vs the signed manifest) and fall back to the Tor fetch only
when no valid persisted copy exists — so accept/approve succeeds
even when the contributor's Tor provider is dead, while the accept
re-send still goes over Tor.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from crypto import blake3_hash
from test_tor_persistence_and_agent_approve import POOL, REPO, _delivered, world
from transport import FakeTransport


class DeadTransport(FakeTransport):
    """Every fetch raises (contributor provider dead); sends counted."""

    def __init__(self):
        super().__init__()
        self.fetch_calls = 0
        self.send_calls = 0

    def fetch(self, ticket, dest):
        self.fetch_calls += 1
        raise RuntimeError("provider dead: no live sender")

    def send(self, path):
        self.send_calls += 1
        return super().send(path)


class CountingTransport(FakeTransport):
    """Working transport sharing the original store, counting fetches."""

    def __init__(self, orig: FakeTransport):
        super().__init__()
        self._store = dict(orig._store)
        self._providers = dict(orig._providers)
        self.fetch_calls = 0

    def fetch(self, ticket, dest):
        self.fetch_calls += 1
        return super().fetch(ticket, dest)


def _prop_and_maintainer(w):
    prop = w["maint_svc"]._propagator(POOL)
    return prop, prop._members[prop._maintainer_key_id]


def test_decrypt_prefers_persisted_bundle_no_tor_fetch(tmp_path):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    manifest = _delivered(w)  # audit persisted the verified bundle
    dead = DeadTransport()
    w["maint_svc"]._transport = dead
    prop, maintainer = _prop_and_maintainer(w)
    plaintext, dek, source = prop._decrypt_for_maintainer(manifest, maintainer)
    assert source == "persisted"
    assert dead.fetch_calls == 0
    assert blake3_hash(plaintext) == manifest["content_hash"]
    assert len(plaintext) == manifest["size_bytes"]


def test_accept_succeeds_with_dead_provider_and_resends_over_tor(tmp_path):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    _delivered(w)
    dead = DeadTransport()
    w["maint_svc"]._transport = dead
    out = w["maint_svc"].add_repo_approve(REPO, source=None)
    assert out["ok"] is True and out["state"] == "accepted"
    assert dead.fetch_calls == 0, "accept must not re-download the bundle"
    assert dead.send_calls >= 1, "accept re-send must still go over Tor"


@pytest.mark.parametrize("tamper", ["truncated", "wrong-hash"])
def test_tampered_persisted_bundle_falls_back_to_fetch(tmp_path, tamper):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    manifest = _delivered(w)
    bundle_path = checkout / "bundle-v1.git"
    original = bundle_path.read_bytes()
    if tamper == "truncated":
        bundle_path.write_bytes(original[: len(original) // 2])
    else:
        bundle_path.write_bytes(b"X" * len(original))
    counting = CountingTransport(w["transport"])
    w["maint_svc"]._transport = counting
    prop, maintainer = _prop_and_maintainer(w)
    plaintext, dek, source = prop._decrypt_for_maintainer(manifest, maintainer)
    assert source == "tor_fetch"
    assert counting.fetch_calls >= 1
    assert blake3_hash(plaintext) == manifest["content_hash"]


def test_missing_bundle_falls_back_to_fetch(tmp_path):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    manifest = _delivered(w)
    (checkout / "bundle-v1.git").unlink()
    counting = CountingTransport(w["transport"])
    w["maint_svc"]._transport = counting
    prop, maintainer = _prop_and_maintainer(w)
    plaintext, dek, source = prop._decrypt_for_maintainer(manifest, maintainer)
    assert source == "tor_fetch"
    assert counting.fetch_calls >= 1
    assert blake3_hash(plaintext) == manifest["content_hash"]


def test_audit_first_time_persists_second_run_idempotent_from_cache(tmp_path):
    checkout = tmp_path / "checkout"
    w = world(tmp_path, checkout=checkout)
    bundle = w["member_svc"].add_repo_build_bundle(w["src"], REPO)
    manifest = bundle["manifest"]
    prop, maintainer = _prop_and_maintainer(w)
    first = prop.audit(manifest=manifest, maintainer=maintainer)
    bundle_path = Path(first["bundle_path"])
    assert bundle_path.is_file()
    first_bytes = bundle_path.read_bytes()
    assert blake3_hash(first_bytes) == manifest["content_hash"]
    # Second run with a dead provider: must reuse the persisted copy.
    # (audit_id is second-resolution; sleep past the boundary.)
    time.sleep(1.1)
    dead = DeadTransport()
    prop._transport = dead
    second = prop.audit(manifest=manifest, maintainer=maintainer)
    assert second["bundle_path"] == first["bundle_path"]
    assert dead.fetch_calls == 0
    assert bundle_path.read_bytes() == first_bytes
