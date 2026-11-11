"""Transfer-ticket re-issue on /fetch: the persisted ticket in the
signed manifest is ephemeral; /fetch repo mints a FRESH Tor transfer of
the persisted verified bundle and returns it top-level, leaving the
signed manifest bytes untouched."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import add_member, bind_membership, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport

from tests.test_fetch_repo_tasks import (
    HELLO, MEMBER_USER, OWNER, POOL, REPO, _git_src, _insert_identity,
    _member_source, _owner_source, memdb, world,
)


def test_fetch_repo_returns_fresh_ticket_not_stored(tmp_path):
    w = world(tmp_path)
    fetched = w["maint_svc"].fetch_repo(REPO, source=_owner_source())
    stored = json.loads(fetched["manifest_json"])["transfer_ticket"]
    assert fetched["transfer_reissued"] is True
    assert fetched["transfer_ticket"]
    assert fetched["transfer_ticket"] != stored
    # Signed manifest bytes untouched: signature still verifies.
    import crypto as C
    from nacl.signing import VerifyKey
    manifest = json.loads(fetched["manifest_json"])
    vk = VerifyKey(bytes.fromhex(
        w["maint"].verify_key.encode(RawEncoder).hex()), encoder=RawEncoder)
    assert C.verify_manifest(vk, manifest)


def test_fetch_repo_download_consumes_fresh_ticket(tmp_path):
    w = world(tmp_path)
    fetched = w["maint_svc"].fetch_repo(REPO, source=_owner_source())
    manifest = json.loads(fetched["manifest_json"])
    stored = manifest["transfer_ticket"]
    fresh = fetched["transfer_ticket"]
    # Kill the stored ticket: a download without the fresh ticket must fail.
    del w["transport"]._store[stored]
    find_conn = memdb()
    _insert_identity(find_conn, w["maint"])
    find_conn.commit()
    find = PoolService(find_conn, w["transport"],
                       caller_key_id=w["member"].key_id,
                       caller_signing_key=w["member"].signing_key,
                       caller_box_sk=w["member"].box_sk)
    with pytest.raises(Exception):
        find.fetch_repo_download(manifest, dest=tmp_path / "dl-dead",
                                 source=_member_source())
    res = find.fetch_repo_download(manifest, dest=tmp_path / "dl",
                                   source=_member_source(),
                                   transfer_ticket=fresh)
    hello = Path(res["clone_dir"]) / "hello.txt"
    assert hello.read_text() == HELLO


def test_claim_reply_carries_fresh_ticket(tmp_path):
    w = world(tmp_path)
    t = w["maint_svc"].add_ticket(POOL, REPO, "Claim me", brief="scope",
                                  source=_owner_source())
    # Production /claim is processed on the pool (maintainer) gateway,
    # which holds the maintainer box key needed to re-send the bundle.
    out = w["maint_svc"].claim(t["ticket_id"], declared_paths=["hello.txt"],
                               source=_owner_source())
    stored = json.loads(
        w["conn"].execute(
            "SELECT manifest_json FROM repo_versions WHERE repo_id=? AND state='accepted'",
            (REPO,)).fetchone()[0])["transfer_ticket"]
    assert out.get("transfer_reissued") is True
    assert out.get("transfer_ticket")
    assert out["transfer_ticket"] != stored
    # Manifest inside the claim reply is the untouched signed artifact.
    if "manifest" in out:
        assert out["manifest"]["transfer_ticket"] == stored


def test_fetch_repo_reissue_falls_back_honestly_without_transport(tmp_path):
    w = world(tmp_path)
    svc = PoolService(w["conn"], None, caller_key_id=w["maint"].key_id,
                      caller_signing_key=w["maint"].signing_key,
                      caller_box_sk=w["maint"].box_sk)
    out = svc.fetch_repo(REPO, source=_owner_source())
    assert out["transfer_reissued"] is False
    assert out["transfer_reissue_error"]
    assert out["transfer_ticket"] == json.loads(out["manifest_json"])["transfer_ticket"]
