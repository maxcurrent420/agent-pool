"""contributor-fetch.py wrapper: argument parsing + reply-shape
normalization (both /fetch repo and /claim reply shapes) + a fake-
transport happy path mirroring test_workflow_c_claim.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import add_member, bind_membership, init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport, TorNotReady

REPO_ROOT = Path(__file__).parent.parent
OWNER = "owner-1"
MEMBER_USER = "member-1"
POOL = "pool-1"
REPO = "demo"


def _load_wrapper():
    path = REPO_ROOT / "scripts" / "contributor-fetch.py"
    spec = importlib.util.spec_from_file_location(
        "contributor_fetch_wrapper", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_src(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text("hello fetch\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def _world(tmp_path):
    """Maintainer + enrolled contributor, repo v1 accepted."""
    conn = init_ledger(__import__("sqlite3").connect(":memory:"))
    conn.row_factory = __import__("sqlite3").Row
    maint = ensure_identity(tmp_path / "m")
    contrib = ensure_identity(tmp_path / "c")
    for ident in (maint, contrib):
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
    contrib_svc = PoolService(conn, transport, caller_key_id=contrib.key_id,
                              caller_signing_key=contrib.signing_key,
                              caller_box_sk=contrib.box_sk)
    maint_svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
    sync_whitelist(conn, POOL, [MEMBER_USER])
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), source={"user_id": OWNER})
    keys = contrib_svc.my_keys()
    maint_svc.enroll_member(
        POOL, keys["key_id"], keys["box_pubkey_hex"], keys["verify_key_hex"],
        discord_user_id=MEMBER_USER, source={"user_id": OWNER})
    ticket = maint_svc.add_ticket(POOL, REPO, "Work",
                                  source={"user_id": OWNER})
    return {"conn": conn, "maint_svc": maint_svc,
            "contrib_svc": contrib_svc, "ticket_id": ticket["ticket_id"]}


# ---- argument parsing ----

def test_parser_requires_a_source():
    mod = _load_wrapper()
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args([])


def test_parser_reply_file_and_manifest_are_mutually_exclusive():
    mod = _load_wrapper()
    with pytest.raises(SystemExit):
        mod.build_parser().parse_args(
            ["--reply-file", "r.json", "--manifest", "m.json"])


def test_parser_defaults():
    mod = _load_wrapper()
    args = mod.build_parser().parse_args(["--reply-file", "r.json"])
    assert args.dest is None and args.ticket_id is None and args.data_dir is None


def test_manifest_requires_transfer_ticket(tmp_path, capsys):
    mod = _load_wrapper()
    m = tmp_path / "m.json"
    m.write_text("{}")
    rc = mod.main(["--manifest", str(m)])
    assert rc == 2
    assert "--transfer-ticket" in capsys.readouterr().err


# ---- reply-shape normalization ----

def test_normalize_fetch_repo_reply_shape():
    mod = _load_wrapper()
    manifest = {"repo_id": REPO, "version": 1, "content_hash": "abc"}
    reply = {"ok": True, "manifest_json": json.dumps(manifest),
             "transfer_ticket": "fresh-ticket"}
    m, t = mod.normalize_reply(reply)
    assert m == manifest and t == "fresh-ticket"


def test_normalize_claim_reply_shape_dict_manifest():
    mod = _load_wrapper()
    manifest = {"repo_id": REPO, "version": 2, "content_hash": "def"}
    reply = {"ok": True, "claim": {"manifest": manifest,
                                   "transfer_ticket": "claim-ticket"}}
    m, t = mod.normalize_reply(reply)
    assert m == manifest and t == "claim-ticket"


def test_normalize_claim_reply_shape_manifest_json_string():
    mod = _load_wrapper()
    manifest = {"repo_id": REPO, "version": 3}
    reply = {"claim": {"manifest_json": json.dumps(manifest),
                       "transfer_ticket": "claim-ticket-2"}}
    m, t = mod.normalize_reply(reply)
    assert m == manifest and t == "claim-ticket-2"


def test_normalize_falls_back_to_manifest_ticket():
    mod = _load_wrapper()
    manifest = {"repo_id": REPO, "version": 1,
                "transfer_ticket": "stored-ticket"}
    m, t = mod.normalize_reply({"manifest": manifest})
    assert t == "stored-ticket"


def test_normalize_rejects_garbage():
    mod = _load_wrapper()
    with pytest.raises(ValueError):
        mod.normalize_reply({"ok": True})
    with pytest.raises(ValueError):
        mod.normalize_reply({"claim": {"manifest_json": "{not json"}})
    with pytest.raises(ValueError):
        mod.normalize_reply({"manifest": {"repo_id": REPO}})  # no ticket


def test_load_manifest_arg_path_and_inline(tmp_path):
    mod = _load_wrapper()
    manifest = {"repo_id": REPO, "version": 1}
    f = tmp_path / "m.json"
    f.write_text(json.dumps(manifest))
    assert mod.load_manifest_arg(str(f)) == manifest
    assert mod.load_manifest_arg(json.dumps(manifest)) == manifest
    with pytest.raises(ValueError):
        mod.load_manifest_arg("{nope")


# ---- happy path (fake transport) ----

def test_fetch_happy_path_via_reply_file(tmp_path, monkeypatch, capsys):
    w = _world(tmp_path)
    mod = _load_wrapper()
    monkeypatch.setattr(mod, "build_service",
                        lambda data_dir=None: w["contrib_svc"])
    fetch = w["maint_svc"].fetch_repo(REPO, source={"user_id": OWNER})
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps(fetch))
    dest = tmp_path / "dl"
    rc = mod.main(["--reply-file", str(reply), "--dest", str(dest)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["repo_id"] == REPO
    assert Path(payload["bundle_file"]).exists()
    assert Path(payload["clone_dir"]).exists()
    assert payload["content_hash"]
    # no secrets in output
    assert "box_sk" not in out and "signing_key" not in out


def test_fetch_tor_fail_closed(tmp_path, monkeypatch, capsys):
    mod = _load_wrapper()

    def boom(*a, **kw):
        raise TorNotReady("tor not ready")

    monkeypatch.setattr(mod, "tor_ready", boom)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"repo_id": REPO, "version": 1}))
    rc = mod.main(["--manifest", str(manifest), "--transfer-ticket", "t"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tor not ready" in err
