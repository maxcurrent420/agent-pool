"""contributor-push.py wrapper: output structure + failure cases.

Uses a FakeTransport-backed PoolService (the same construction as
test_push_path) injected via monkeypatching the wrapper's
build_service, plus a Tor-fail-closed case through the real
build_service with tor_ready monkeypatched to raise TorNotReady.
"""
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import add_member, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport, TorNotReady

REPO_ROOT = Path(__file__).parent.parent
OWNER = "owner-1"
MEMBER_USER = "member-1"
POOL = "pool-1"
REPO = "demo"


def _load_wrapper():
    path = REPO_ROOT / "scripts" / "contributor-push.py"
    spec = importlib.util.spec_from_file_location(
        "contributor_push_wrapper", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello push\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def _world(tmp_path):
    """Maintainer + member, repo v1 accepted, one claimed ticket."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn = init_ledger(conn)
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
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                       source={"user_id": OWNER})
    ticket = maint_svc.add_ticket(POOL, REPO, "Improve hello",
                                  source={"user_id": OWNER})
    member_svc.claim(ticket["ticket_id"], source={"user_id": MEMBER_USER})
    return member_svc, src


def test_wrapper_output_structure(tmp_path, monkeypatch, capsys):
    member_svc, src = _world(tmp_path)
    mod = _load_wrapper()
    monkeypatch.setattr(mod, "build_service", lambda data_dir=None: member_svc)
    rc = mod.main([str(src), "--repo", REPO])
    out = capsys.readouterr().out
    assert rc == 0
    assert "manifest_json=" in out
    assert "transfer_ticket=" in out
    manifest_json = next(
        line.split("=", 1)[1] for line in out.splitlines()
        if line.startswith("manifest_json="))
    assert "\n" not in manifest_json.strip()
    manifest = json.loads(manifest_json)  # single-line, pasteable JSON
    ticket = next(
        line.split("=", 1)[1] for line in out.splitlines()
        if line.startswith("transfer_ticket="))
    assert manifest["transfer_ticket"] == ticket
    assert f"/push repo --deliver {ticket} --token <GRANT> " \
           f"--manifest {manifest_json}" in out


def test_wrapper_json_output(tmp_path, monkeypatch, capsys):
    member_svc, src = _world(tmp_path)
    mod = _load_wrapper()
    monkeypatch.setattr(mod, "build_service", lambda data_dir=None: member_svc)
    rc = mod.main([str(src), "--repo", REPO, "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["manifest_json"]
    assert payload["transfer_ticket"]
    assert json.loads(payload["manifest_json"])["transfer_ticket"] == \
        payload["transfer_ticket"]
    assert "/push repo --deliver" in payload["deliver_command"]
    assert "--token <GRANT>" in payload["deliver_command"]
    assert "--manifest " in payload["deliver_command"]


def test_wrapper_rejects_non_directory(tmp_path, monkeypatch, capsys):
    mod = _load_wrapper()
    monkeypatch.setattr(
        mod, "build_service",
        lambda data_dir=None: (_ for _ in ()).throw(
            AssertionError("service must not be built for a bad path")))
    missing = tmp_path / "does-not-exist"
    rc = mod.main([str(missing), "--repo", REPO])
    err = capsys.readouterr().err
    assert rc != 0
    assert "repo_path is not an existing directory" in err
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("not a dir\n")
    rc = mod.main([str(a_file), "--repo", REPO])
    assert rc != 0


def test_wrapper_fails_closed_when_tor_down(tmp_path, monkeypatch, capsys):
    mod = _load_wrapper()

    def _boom(**kwargs):
        raise TorNotReady(
            "tor transport unavailable: Tor control port 127.0.0.1:9051 "
            "is not reachable. Start the Tor daemon: systemctl start "
            "agent-pool-tor.")

    monkeypatch.setattr(mod, "tor_ready", _boom)
    monkeypatch.setenv("AGENT_POOL_DATA_DIR", str(tmp_path / "data"))
    src = _git_src(tmp_path / "src")
    rc = mod.main([str(src), "--repo", REPO])
    err = capsys.readouterr().err
    assert rc != 0
    assert "agent-pool-tor" in err
