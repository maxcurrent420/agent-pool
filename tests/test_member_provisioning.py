"""Claim-time member provisioning: a bound user's claim must register
their key in ``members`` so FUTURE repo versions wrap a DEK for them
(the legacy flow only ever called add_member for the maintainer, so
whitelisted users could claim yet never fetch bytes). The Discord-id
binding itself happens at enrollment (enroll_member), never at claim.

Also pins the documented late-joiner limitation: a version submitted
BEFORE the user was provisioned does not retroactively wrap them.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from nacl.encoding import RawEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))

from identity import ensure_identity
from ledger import init_ledger, sync_whitelist
from tickets import PoolService
from transport import FakeTransport

OWNER = "owner-1"
FRIEND = "friend-1"
POOL = "pool-1"
REPO = "demo"


def memdb():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return init_ledger(c)


def _git_src(path: Path, content="hello fetch\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "hello.txt").write_text(content)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "v1"], check=True)
    return path


def world(tmp_path):
    """Maintainer + member identities, NO manual add_member for the
    member (only the identities rows tests happen to record). The
    member's Discord id is bound to the member key at setup — the
    enrollment-time binding every real contributor gets from
    enroll_member (claim no longer binds anything)."""
    conn = memdb()
    maint = ensure_identity(tmp_path / "m")
    member = ensure_identity(tmp_path / "a")
    for ident in (maint, member):
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
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
    from ledger import bind_membership
    bind_membership(conn, POOL, FRIEND, member.key_id)
    return conn, transport, maint_svc, member_svc, maint, member


def member_row(conn, key_id):
    return conn.execute(
        "SELECT key_id, role, box_pubkey_hex, verify_pubkey_hex FROM members"
        " WHERE pool_id=? AND key_id=?", (POOL, key_id)).fetchone()


def test_claim_provisions_member_row_without_manual_add_member(tmp_path):
    conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
    assert member_row(conn, member.key_id) is None
    sync_whitelist(conn, POOL, [FRIEND])
    ticket = maint_svc.add_ticket(POOL, REPO, "Work", source={"user_id": OWNER})
    claim = member_svc.claim(ticket["ticket_id"], source={"user_id": FRIEND})
    assert claim["state"] == "active"
    row = member_row(conn, member.key_id)
    assert row is not None
    assert row["role"] == "member"
    assert row["box_pubkey_hex"] == member.box_pk.encode(RawEncoder).hex()
    assert row["verify_pubkey_hex"] == member.verify_key.encode(RawEncoder).hex()
    # The enrollment-time binding is untouched by the claim.
    bound = conn.execute(
        "SELECT key_id FROM pool_memberships WHERE pool_id=? AND discord_user_id=?",
        (POOL, FRIEND)).fetchone()
    assert bound[0] == member.key_id


def test_provisioned_member_gets_wrapped_dek_on_next_version(tmp_path):
    conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
    src = _git_src(tmp_path / "src")
    # v1 submitted BEFORE the friend was whitelisted/claimed.
    maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                       source={"user_id": OWNER})
    m_v1 = json.loads(maint_svc.fetch_repo(REPO, source={"user_id": OWNER})["manifest_json"])
    assert member.key_id not in (m_v1.get("wrapped_keys") or {})
    # Friend is whitelisted and claims -> provisioned.
    sync_whitelist(conn, POOL, [FRIEND])
    ticket = maint_svc.add_ticket(POOL, REPO, "Work", source={"user_id": OWNER})
    member_svc.claim(ticket["ticket_id"], source={"user_id": FRIEND})
    assert member_row(conn, member.key_id) is not None
    # v2: the provisioned member must now be wrapped, and can fetch.
    (src / "hello.txt").write_text("hello v2\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "v2"], check=True)
    out = maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                             source={"user_id": OWNER})
    assert out["version"] == 2
    m_v2 = json.loads(maint_svc.fetch_repo(REPO, source={"user_id": OWNER})["manifest_json"])
    assert m_v2["version"] == 2
    assert member.key_id in (m_v2.get("wrapped_keys") or {})
    # Fetch roundtrip on the contributor gateway (signer key recorded).
    dl = tmp_path / "dl"
    res = member_svc.fetch_repo_download(m_v2, dest=dl, source={"user_id": FRIEND})
    assert (Path(res["clone_dir"]) / "hello.txt").read_text() == "hello v2\n"


def test_late_joiner_cannot_fetch_pre_provision_version(tmp_path):
    """Documented limitation: a version submitted before provisioning
    has no wrapped DEK for that member and stays unfetchable."""
    conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
    src = _git_src(tmp_path / "src")
    maint_svc.add_repo(POOL, REPO, str(src), display_name="Demo",
                       source={"user_id": OWNER})
    m_v1 = json.loads(maint_svc.fetch_repo(REPO, source={"user_id": OWNER})["manifest_json"])
    sync_whitelist(conn, POOL, [FRIEND])
    ticket = maint_svc.add_ticket(POOL, REPO, "Work", source={"user_id": OWNER})
    member_svc.claim(ticket["ticket_id"], source={"user_id": FRIEND})
    with pytest.raises(PermissionError):
        member_svc.fetch_repo_download(m_v1, dest=tmp_path / "dl",
                                       source={"user_id": FRIEND})


def test_existing_member_row_is_never_clobbered(tmp_path):
    conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
    from ledger import add_member
    add_member(conn, POOL, member.key_id, role="member",
               box_pubkey_hex="ab" * 32, verify_pubkey_hex="cd" * 32)
    sync_whitelist(conn, POOL, [FRIEND])
    ticket = maint_svc.add_ticket(POOL, REPO, "Work", source={"user_id": OWNER})
    member_svc.claim(ticket["ticket_id"], source={"user_id": FRIEND})
    row = member_row(conn, member.key_id)
    assert row["box_pubkey_hex"] == "ab" * 32
    assert row["verify_pubkey_hex"] == "cd" * 32
    assert row["role"] == "member"


class TestEnrollmentBinding:
    """Task 3: enrollment REQUIRES the enrolling user's Discord id and
    binds it to the enrolled key; a conflicting prior binding hard-fails."""

    def test_enroll_without_discord_id_hard_fails(self, tmp_path):
        conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
        box = member.box_pk.encode(RawEncoder).hex()
        ver = member.verify_key.encode(RawEncoder).hex()
        with pytest.raises(ValueError, match="Discord user id"):
            maint_svc.enroll_member(POOL, member.key_id, box, ver,
                                    source={"user_id": OWNER})

    def test_enroll_binds_discord_id_to_enrolled_key(self, tmp_path):
        from ledger import lookup_membership_key
        conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
        box = member.box_pk.encode(RawEncoder).hex()
        ver = member.verify_key.encode(RawEncoder).hex()
        out = maint_svc.enroll_member(POOL, member.key_id, box, ver,
                                      discord_user_id=FRIEND,
                                      source={"user_id": OWNER})
        assert out["ok"] is True
        assert lookup_membership_key(conn, FRIEND, pool_id=POOL) == member.key_id
        assert member_row(conn, member.key_id) is not None

    def test_enroll_conflicting_binding_raises(self, tmp_path):
        from ledger import lookup_membership_key
        conn, transport, maint_svc, member_svc, maint, member = world(tmp_path)
        box = member.box_pk.encode(RawEncoder).hex()
        ver = member.verify_key.encode(RawEncoder).hex()
        maint_svc.enroll_member(POOL, member.key_id, box, ver,
                                discord_user_id=FRIEND,
                                source={"user_id": OWNER})
        other = ensure_identity(tmp_path / "other")
        box2 = other.box_pk.encode(RawEncoder).hex()
        ver2 = other.verify_key.encode(RawEncoder).hex()
        with pytest.raises(ValueError, match="membership conflict"):
            maint_svc.enroll_member(POOL, other.key_id, box2, ver2,
                                    discord_user_id=FRIEND,
                                    source={"user_id": OWNER})
        # First binding unchanged.
        assert lookup_membership_key(conn, FRIEND, pool_id=POOL) == member.key_id


class TestRegistrationBinding:
    """Task 2: registration binds BOTH the maintainer Discord id and the
    maintainer bot's Discord id to the gateway (maintainer) key."""

    BOT = "maint-bot-1"

    def _svc(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID", self.BOT)
        conn = memdb()
        maint = ensure_identity(tmp_path / "m")
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (maint.key_id, maint.verify_key.encode(RawEncoder).hex(),
             maint.box_pk.encode(RawEncoder).hex()))
        conn.commit()
        svc = PoolService(conn, FakeTransport(), caller_key_id=maint.key_id,
                          caller_signing_key=maint.signing_key,
                          caller_box_sk=maint.box_sk)
        return conn, svc, maint

    def test_register_binds_maintainer_and_bot_ids(self, tmp_path, monkeypatch):
        conn, svc, maint = self._svc(tmp_path, monkeypatch)
        out = svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
        assert out["ok"] is True
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT discord_user_id, key_id FROM pool_memberships WHERE pool_id=?",
            (POOL,))}
        assert rows == {OWNER: maint.key_id, self.BOT: maint.key_id}
        # The bot itself is whitelisted too.
        auth = {r[0] for r in conn.execute(
            "SELECT discord_user_id FROM pool_authorized WHERE pool_id=?", (POOL,))}
        assert self.BOT in auth and OWNER in auth

    def test_register_without_bot_id_hard_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID", raising=False)
        conn = memdb()
        maint = ensure_identity(tmp_path / "m")
        conn.execute(
            "INSERT INTO identities (key_id, verify_key_hex, box_pubkey_hex, created_at)"
            " VALUES (?,?,?,0)",
            (maint.key_id, maint.verify_key.encode(RawEncoder).hex(),
             maint.box_pk.encode(RawEncoder).hex()))
        conn.commit()
        svc = PoolService(conn, FakeTransport(), caller_key_id=maint.key_id,
                          caller_signing_key=maint.signing_key,
                          caller_box_sk=maint.box_sk)
        with pytest.raises(ValueError) as exc:
            svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
        assert str(exc.value) == (
            "register needs maintainer_bot_discord_user_id: set it in config or "
            "AGENT_POOL_MAINTAINER_BOT_DISCORD_USER_ID")

    def test_register_conflicting_preexisting_binding_raises(self, tmp_path, monkeypatch):
        from ledger import bind_membership, lookup_membership_key
        conn, svc, maint = self._svc(tmp_path, monkeypatch)
        svc.register_pool(POOL, "Team", maintainer_discord_user_id=OWNER)
        # The registration binding is protected by conflict semantics: a
        # DIFFERENT key for the maintainer id hard-fails and the original
        # registration binding is unchanged. (A pre-register conflict is
        # unreachable: pool_memberships FKs to pools, and create_pool
        # enforces one pool per gateway.)
        with pytest.raises(ValueError, match="membership conflict"):
            bind_membership(conn, POOL, OWNER, "agent:someone-else")
        assert lookup_membership_key(conn, OWNER, pool_id=POOL) == maint.key_id
