"""Phase 2 read surface: fetch_repo_list / fetch_task_list / fetch_repo /
fetch_repo_download (+ auth, freeze, foreign-pool gateway recording)."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import ClaimRefused, add_member, init_ledger, sync_whitelist
from tickets import PoolNotLocalError, PoolService
from transport import FakeTransport

OWNER = "owner-1"
MEMBER_USER = "member-1"
HELLO = "hello fetch\n"
POOL = "pool-1"
REPO = "demo"


def _whitelist(conn, pool_id, user_id, ident):
    """Phase 1 stand-in for the removed invite method (see test_push_path)."""
    sync_whitelist(conn, pool_id, [user_id])
    add_member(conn, pool_id, ident.key_id, role="member",
               box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex(),
               verify_pubkey_hex=ident.verify_key.encode(RawEncoder).hex())


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_src(path: Path, files=None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    for name, content in (files or {"hello.txt": HELLO}).items():
        (path / name).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def _insert_identity(conn, ident):
    conn.execute(
        "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
        " VALUES (?,?,?,0)",
        (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
         ident.box_pk.encode(RawEncoder).hex()),
    )


def world(tmp_path):
    """Maint + member identities, whitelisted pool, real git src, repo v1."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        _insert_identity(conn, ident)
    conn.commit()
    transport = FakeTransport()
    maint_svc = PoolService(conn, transport, caller_key_id=maint.key_id,
                            caller_signing_key=maint.signing_key,
                            caller_box_sk=maint.box_sk)
    member_svc = PoolService(conn, transport, caller_key_id=member.key_id,
                             caller_signing_key=member.signing_key,
                             caller_box_sk=member.box_sk)
    maint_svc.register_pool(POOL, "Team", discord_user_id=OWNER)
    _whitelist(conn, POOL, MEMBER_USER, member)
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                       source={"user_id": OWNER})
    return {
        "conn": conn, "transport": transport,
        "maint": maint, "member": member,
        "maint_svc": maint_svc, "member_svc": member_svc,
        "src": src,
    }


def _owner_source():
    return {"user_id": OWNER}


def _member_source():
    return {"user_id": MEMBER_USER}


def test_fetch_repo_list_carries_available_tasks_and_addressing(tmp_path):
    w = world(tmp_path)
    t = w["maint_svc"].add_ticket(POOL, REPO, "Fix hello", brief="Scope: hello.txt",
                                  source=_owner_source())
    out = w["maint_svc"].fetch_repo_list(source=_owner_source())
    assert out["ok"] is True and out["pool_id"] == POOL
    assert len(out["repos"]) == 1
    repo = out["repos"][0]
    assert repo["repo_id"] == REPO and repo["accepted_version"] == 1
    assert [x["ticket_id"] for x in repo["available_tasks"]] == [t["ticket_id"]]
    avail = repo["available_tasks"][0]
    assert avail["claimed"] is False
    gw = out["gateway"]
    assert gw["endpoint"] == POOL
    assert gw["transport_mode"] == "relay"
    assert "relay_url" in gw and "transport_note" in gw


def test_fetch_task_list_claimed_status_and_claimant(tmp_path):
    w = world(tmp_path)
    open_t = w["maint_svc"].add_ticket(POOL, REPO, "Open one", source=_owner_source())
    claimed_t = w["maint_svc"].add_ticket(POOL, REPO, "Claimed one", source=_owner_source())
    done_t = w["maint_svc"].add_ticket(POOL, REPO, "Done one", source=_owner_source())
    w["member_svc"].claim(done_t["ticket_id"], declared_paths=["done.py"],
                          source=_member_source())
    w["member_svc"].complete(done_t["ticket_id"], source=_member_source())
    w["member_svc"].claim(claimed_t["ticket_id"], declared_paths=["claimed.py"],
                          source=_member_source())
    out = w["maint_svc"].fetch_task_list(source=_owner_source())
    by_id = {x["ticket_id"]: x for x in out["tasks"]}
    assert by_id[open_t["ticket_id"]]["claimed"] is False
    assert by_id[open_t["ticket_id"]]["claimant_key_id"] is None
    assert by_id[claimed_t["ticket_id"]]["claimed"] is True
    assert by_id[claimed_t["ticket_id"]]["claimant_key_id"] == w["member"].key_id
    assert done_t["ticket_id"] not in by_id


def test_fetch_task_list_single_ticket_detail(tmp_path):
    w = world(tmp_path)
    t = w["maint_svc"].add_ticket(POOL, REPO, "Detail me", source=_owner_source())
    out = w["maint_svc"].fetch_task_list(ticket=t["ticket_id"], source=_owner_source())
    assert out["ok"] is True and out["pool_id"] == POOL
    row = out["ticket"]
    assert row["ticket_id"] == t["ticket_id"]
    assert row["title"] == "Detail me"
    assert row["claimed"] is False
    assert row["claimant_key_id"] is None


def test_fetch_repo_download_roundtrip(tmp_path):
    w = world(tmp_path)
    fetched = w["maint_svc"].fetch_repo(REPO, source=_owner_source())
    manifest = json.loads(fetched["manifest_json"])
    find = PoolService(memdb(), w["transport"], caller_key_id=w["member"].key_id,
                       caller_signing_key=w["member"].signing_key,
                       caller_box_sk=w["member"].box_sk)
    dest = tmp_path / "dl"
    res = find.fetch_repo_download(manifest, dest=dest, source=_member_source())
    hello = Path(res["clone_dir"]) / "hello.txt"
    assert hello.read_text() == HELLO


def test_fetch_repo_download_refuses_unwhitelisted_key(tmp_path):
    w = world(tmp_path)
    fetched = w["maint_svc"].fetch_repo(REPO, source=_owner_source())
    manifest = json.loads(fetched["manifest_json"])
    outsider = ensure_identity(tmp_path / "out")
    svc = PoolService(memdb(), w["transport"], caller_key_id=outsider.key_id,
                      caller_signing_key=outsider.signing_key,
                      caller_box_sk=outsider.box_sk)
    with pytest.raises(PermissionError):
        svc.fetch_repo_download(manifest, dest=tmp_path / "dl")


def test_fetch_refuses_unauthorized_and_frozen(tmp_path):
    w = world(tmp_path)
    w["maint_svc"].add_ticket(POOL, REPO, "T", source=_owner_source())
    stranger = {"user_id": "stranger-9"}
    with pytest.raises(PermissionError):
        w["maint_svc"].fetch_repo_list(source=stranger)
    with pytest.raises(PermissionError):
        w["maint_svc"].fetch_task_list(source=stranger)
    with pytest.raises(PermissionError):
        w["maint_svc"].fetch_repo(REPO, source=stranger)
    w["maint_svc"].freeze(POOL, True, source=_owner_source())
    with pytest.raises(ClaimRefused, match="frozen"):
        w["maint_svc"].fetch_repo_list(source=_owner_source())
    with pytest.raises(ClaimRefused, match="frozen"):
        w["maint_svc"].fetch_task_list(source=_owner_source())
    with pytest.raises(ClaimRefused, match="frozen"):
        w["maint_svc"].fetch_repo(REPO, source=_owner_source())


def test_fetch_foreign_pool_records_gateway(tmp_path):
    w = world(tmp_path)
    with pytest.raises(PoolNotLocalError):
        w["maint_svc"].fetch_repo_list(pool_id="pool-elsewhere")
    gateways = w["maint_svc"].list_gateways()
    row = next(g for g in gateways if g["pool_id"] == "pool-elsewhere")
    assert row["role"] == "interact"


def test_fetch_repo_unknown_repo(tmp_path):
    w = world(tmp_path)
    with pytest.raises(ValueError, match="no ACCEPTED version"):
        w["maint_svc"].fetch_repo("nope", source=_owner_source())
