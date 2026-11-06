"""Ticket coordination service — gateway-native.

Hermes posts; the plugin only returns content. Every method returns a result
dict with the state word and a ``notice`` string; handlers format it via
``replies.format_result`` and Hermes posts it. The service never opens a
Discord client and never imports ``discord``.
"""
from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

try:  # package import (Hermes loads plugins as a package)
    from . import crypto as C
    from . import ledger as L
    from .execution import (BudgetExhausted, BudgetTracker, LifecycleWorkerRunner,
                            LocalProcessRunner, SandboxPolicy, build_mandate_brief,
                            create_worktree, run_acceptance, run_worker_loop)
    from .integration import (apply_edit, merge_branch, rescan_tree, scan_diff)
    from .propagation import Member, RepoPropagator
    from .transport import TransportBackend
except ImportError:  # flat import (pytest from repo root)
    import crypto as C
    import ledger as L
    from execution import (BudgetExhausted, BudgetTracker, LifecycleWorkerRunner,
                           LocalProcessRunner, SandboxPolicy, build_mandate_brief,
                           create_worktree, run_acceptance, run_worker_loop)
    from integration import (apply_edit, merge_branch, rescan_tree, scan_diff)
    from propagation import Member, RepoPropagator
    from transport import TransportBackend

STATE_MARKER = "[agent-pool:state={state}]"
MAX_BRIEF_CHARS = 3000  # coordination payloads stay under the 4 KB guard


def _ticket_payload(ticket: dict) -> dict:
    """Forum-post body: title + mandate brief + state marker. Never repo bytes."""
    budget = json.loads(ticket.get("budget_json") or "{}")
    return {
        "ticket_id": ticket["ticket_id"],
        "title": ticket["title"],
        "repo_id": ticket["repo_id"],
        "brief": ticket.get("brief", "")[:MAX_BRIEF_CHARS],
        "budget": budget,
        "state": ticket["state"],
        "state_marker": STATE_MARKER.format(state=ticket["state"]),
    }


class PoolNotLocalError(Exception):
    """A gateway command named a pool not hosted by this agent (D3)."""

    def __init__(self, detail, pool_id=None):
        super().__init__(detail)
        self.detail = detail
        self.pool_id = pool_id

    @property
    def error_code(self):
        return "pool_not_local"


class PoolService:
    """Ticket coordination for one pool. Caller identity is injected."""

    def __init__(self, conn, *args,
                 transport=None, caller_key_id: str = "", caller_signing_key=None,
                 caller_box_sk=None, budget_defaults=None, pool_resolver=None,
                 gateway=None, transport_overrides=None):
        # Backwards-compat: old call sites passed (conn, gateway, transport).
        # Duck-type: anything with post_notice is a legacy gateway (ignored).
        if args:
            for _a in args:
                if hasattr(_a, "post_notice"):
                    gateway = _a
                elif hasattr(_a, "send"):
                    transport = _a
                elif transport is None:
                    transport = _a
                else:
                    raise TypeError(f"unexpected positional arg: {_a!r}")
        if transport is not None and hasattr(transport, "post_notice"):
            raise TypeError("PoolService no longer takes a gateway adapter")
        self._conn = conn
        self._transport = transport
        # Per-pool transport overrides (transport_mode/relay_url from the
        # plugin ``pools`` config); pools absent here use the top-level
        # transport. Built once by _service_for, never mutated.
        self._transport_overrides: dict = dict(transport_overrides or {})
        self._caller_key_id = caller_key_id
        self._caller_sk = caller_signing_key
        self._caller_box_sk = caller_box_sk
        self._budget_defaults = budget_defaults
        self._pool_resolver = pool_resolver
        self._workers: dict[str, dict] = {}  # ticket_id -> worker record
        self._integrations: dict[str, dict] = {}  # ticket_id -> gate context

    # -- pool resolution + isolation (D2/D3) ---------------------------------

    def _source_from(self, source=None):
        if source is not None:
            return source
        if callable(self._pool_resolver):
            try:
                return self._pool_resolver()
            except TypeError:
                return self._pool_resolver(source)
        return None

    @staticmethod
    def _source_field(source, *names):
        for n in names:
            if isinstance(source, dict) and source.get(n) is not None:
                return source.get(n)
            v = getattr(source, n, None) if source is not None else None
            if v is not None:
                return v
        return None

    def _resolve_pool(self, pool_id=None, source=None) -> str:
        """Hosted pools only: explicit --pool -> the hosted pool -> refusal.

        Platform routes cross-gateway commands; plugin selects among pools
        it hosts. No chat binding, no peer/anchor fallback (2109 P2).
        """
        if pool_id:
            pool_id = str(pool_id).strip()
            if pool_id:
                row = self._conn.execute("SELECT pool_id FROM pools WHERE pool_id=?",
                                         (pool_id,)).fetchone()
                if row is None:
                    hosts = L.list_local_pools(self._conn)
                    raise PoolNotLocalError(
                        f"pool {pool_id} is not hosted by this gateway (hosts: {', '.join(hosts) or 'none'})",
                        pool_id=pool_id)
                return pool_id
        pools = L.list_local_pools(self._conn)
        if len(pools) == 1:
            return pools[0]
        raise PoolNotLocalError(
            "no pool bound to this chat — pass --pool <pool_id>",
            pool_id=pool_id or "")

    def _assert_local_owned(self, pool_id=None, *, require_maintainer=False,
                            source=None) -> str:
        """Hosted-only: pool must exist locally. Owner checks are whitelist
        helpers; no key-based authz remains."""
        resolved = self._resolve_pool(pool_id, source=source)
        row = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                 (resolved,)).fetchone()
        if row is None:
            raise PoolNotLocalError(
                f"pool_not_local: pool {resolved!r} is not hosted by this agent",
                pool_id=resolved)
        return resolved

    def pool_fetch(self, repo_id=None, *, projects=False, tasks=False, pool_id=None, source=None) -> dict:
        """Hosted-pool fetch: projects/tickets/manifest (2109 P2).

        Returns accepted project list, open/claimed ticket list (never
        pending), and for a repo the latest accepted manifest. The manifest
        is carried ONCE, as ``manifest_json`` — the canonical serialization
        the signature covers and ``/work fetch --manifest`` consumes
        verbatim (OD3: no chunking in 2.5.0; see below).

        OD3 decision: manifest size is bounded by fixed fields + ~190 bytes
        per invited agent (one ``wrapped_keys`` entry) + one Iroh transfer
        ticket. Pools with a handful of agents stay well under the 4 KB
        reply guard. If the reply would breach the guard, the fetch fails
        with a determinate error naming the manifest hash instead of a
        silently-truncated reply; hash-addressed retrieval
        (``/work fetch --hash``) is the deferred milestone if membership
        ever approaches the ceiling.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._require_authorized(resolved, source)
        out = {"ok": True, "pool_id": resolved}
        if projects:
            out["projects"] = self._project_rows(resolved)
        if tasks:
            out["tickets"] = [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="open")]
            out["tickets"] += [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="claimed")]
        if repo_id:
            manifest = L.get_latest_accepted_manifest(self._conn, repo_id)
            if manifest is None:
                raise ValueError(f"no ACCEPTED version for repo {repo_id!r} to fetch")
            out["repo_id"] = repo_id
            out["manifest_json"] = json.dumps(manifest, sort_keys=True)
            try:
                from crypto import manifest_hash as _manifest_hash
            except Exception:
                from .crypto import manifest_hash as _manifest_hash
            out["manifest_hash"] = _manifest_hash(manifest)
            out["transfer_ticket"] = manifest.get("transfer_ticket", "")
        if not (projects or tasks or repo_id):
            out["projects"] = self._project_rows(resolved)
            out["tickets"] = [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="open")]
        # OD3: fail loudly rather than let format_result silently truncate a
        # manifest (which would corrupt the /work fetch round-trip).
        try:
            from replies import MAX_CONTENT_BYTES
        except Exception:
            MAX_CONTENT_BYTES = 4096
        size = len(json.dumps(out, indent=2, sort_keys=True).encode("utf-8"))
        if size > MAX_CONTENT_BYTES:
            raise ValueError(
                f"fetch reply {size} bytes exceeds the {MAX_CONTENT_BYTES}-byte "
                f"gateway limit (manifest {out.get('manifest_hash','?')});"
                f" hash-addressed retrieval is not built in 2.5.0")
        return out

    def work_fetch(self, manifest, *, dest, source=None) -> dict:
        """Fetch one repo from a fetched manifest using own box key.

        Unwraps DEK with the caller's box private key, BLAKE3-verifies,
        clones. Manifest comes from /pool fetch on the pool gateway.
        """
        if not manifest:
            raise ValueError("work fetch needs --manifest <json>")
        if not self._caller_box_sk:
            raise PermissionError("work fetch needs the agent's own box key for DEK unwrap")
        # Build a temporary propagator for verification/clone. The manifest
        # carries signer_key_id + wrapped_keys; verify chain against signer.
        repo_id = manifest.get("repo_id", "repo")
        # If this gateway hosts the pool, use the real propagator so the
        # chain check is real (defense-in-depth — the wrapped-key ACL is
        # the byte boundary). A contributor gateway that hosts no pool
        # still fetches by manifest with its own box key.
        try:
            pool_id = self._resolve_pool(None, source=source)
        except Exception:
            pool_id = None
        # Membership keyed by any trusted signer in manifest.
        from propagation import Member as _M
        members = {self._caller_key_id: _M(key_id=self._caller_key_id, role="member",
                                           box_pubkey_hex="", box_sk=self._caller_box_sk)}
        signer = manifest.get("signer_key_id", "")
        if signer and signer not in members:
            members[signer] = _M(key_id=signer, role="maintainer",
                                 verify_key_hex="", box_pubkey_hex="", box_sk=None)
        # If the pool is hosted locally, use the real propagator for verify.
        try:
            prop = self._propagator(repo_id if pool_id is None else pool_id) if pool_id else None
        except Exception:
            prop = None
        if prop is not None:
            fetcher = prop._members.get(self._caller_key_id) or members[self._caller_key_id]
            return prop.fetch(manifest=manifest, fetcher=fetcher, dest=Path(dest))
        # Non-hosted fetch context (contributor gateway): verify against
        # wrapped keys by direct fetch through the service transport.
        # Authorized if the caller's key appears in wrapped_keys.
        wrapped = manifest.get("wrapped_keys") or {}
        if self._caller_key_id not in wrapped:
            raise PermissionError(
                "work fetch refused: caller key not in manifest wrapped_keys (only invited agents can decrypt)")
        from propagation import RepoPropagator as _RP
        prop = _RP(self._conn, self._transport, pool_id=pool_id or "fetch",
                   maintainer_key_id=signer, maintainer_signing_key=None, members=members)
        fetcher = members[self._caller_key_id]
        # No local chain to verify against on a gateway that hosts no pool:
        # the wrapped-key ACL (checked in fetch) is the authorization
        # boundary, and BLAKE3 still pins the bytes to this manifest.
        return prop.fetch(manifest=manifest, fetcher=fetcher, dest=Path(dest),
                          verify_chain=False)

    def propose(self, kind, payload):
        if self._caller_sk is None:
            raise RuntimeError("proposal needs the local signing key")
        packet = C.build_proposal(kind, payload, self._caller_key_id or "agent:unknown", self._caller_sk)
        return {"ok": True, "kind": kind, "proposal": packet,
                "packet": C.proposal_packet_string(packet),
                "notice": f"proposal {kind}: copy this packet to the maintainer gateway"}

    def _packet_hash(self, packet) -> str:
        return C.blake3_hash(C.canonical_manifest_bytes(packet))

    def _member_verify_keys(self, pool_id) -> dict:
        """signer_key_id -> VerifyKey from members.verify_pubkey_hex."""
        keys = {}
        for row in L.list_members(self._conn, pool_id):
            hexv = (row.get("verify_pubkey_hex") or "").strip()
            if hexv:
                try:
                    from nacl.signing import VerifyKey
                    from nacl.encoding import RawEncoder
                    keys[row["key_id"]] = VerifyKey(bytes.fromhex(hexv), encoder=RawEncoder)
                except Exception:
                    continue
        return keys

    def accept_proposal(self, packet, proposer_verify_key=None, verify_keys=None, *, source=None):
        if isinstance(packet, str):
            packet = C.parse_proposal(packet)
        payload = packet.get("payload") or {}
        kind = packet.get("kind")
        proposer = packet.get("proposer_key_id") or packet.get("signer_key_id") or ""
        pool_id = payload.get("pool_id", "")
        # Resolve the pool for the membership/revocation check.
        resolved_pool = pool_id
        if pool_id:
            row = self._conn.execute("SELECT pool_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
            if row is not None:
                resolved_pool = pool_id
            else:
                raise ValueError(f"proposal pool {pool_id!r} is not hosted by this agent; "
                                 "write it on the pool's own gateway (writes are maintainer-only "
                                 "on the owning ledger)")
        # Packet mint is owner-only (whitelist model). A non-owner's packet
        # is relayed to the owner for minting, never minted here.
        if resolved_pool:
            self._check_not_frozen(resolved_pool)
            self._require_owner(resolved_pool, source)
        # Collect candidate verify keys: explicit > member table > passed single.
        candidates = dict(verify_keys or {})
        if resolved_pool:
            for k, v in self._member_verify_keys(resolved_pool).items():
                candidates.setdefault(k, v)
        if proposer_verify_key is not None:
            candidates[proposer] = proposer_verify_key
        # Signature + membership mandatory (Phase 2).
        if not candidates:
            raise ValueError("proposal rejected: no member verify keys on record "
                             "(invite with --verify-key <hex> first)")
        vk = candidates.get(proposer)
        if vk is None:
            raise ValueError(f"proposal rejected: proposer {proposer!r} is not a current pool member")
        if not C.verify_proposal(vk, packet):
            raise ValueError("proposal signature verification failed")
        # Membership / revocation: proposer must currently be a member row
        # (or the maintainer) of the target pool. Bare fixtures with no
        # member rows are exempt (legacy direct-call path).
        if resolved_pool:
            row = self._conn.execute(
                "SELECT maintainer_key_id FROM pools WHERE pool_id=?", (resolved_pool,)).fetchone()
            n_members = self._conn.execute(
                "SELECT COUNT(*) FROM members WHERE pool_id=?", (resolved_pool,)).fetchone()[0]
            is_maintainer = bool(row) and row[0] == proposer
            is_member = self._conn.execute(
                "SELECT 1 FROM members WHERE pool_id=? AND key_id=?",
                (resolved_pool, proposer)).fetchone() is not None
            if n_members and not is_maintainer and not is_member:
                raise ValueError(f"proposal rejected: proposer {proposer!r} is not a current member of pool {resolved_pool!r}")
        # Replay guard: packet_hash recorded in events.
        packet_hash = self._packet_hash(packet)
        dup = self._conn.execute(
            "SELECT 1 FROM events WHERE kind='proposal_minted' AND payload_json LIKE ?", (f"%{packet_hash}%",)).fetchone()
        if dup is not None:
            raise ValueError("duplicate proposal")
        if kind == "ticket_proposal":
            ticket = self.add_ticket(payload.get("pool_id", ""), payload.get("repo_id", ""),
                                     payload.get("title", ""), brief=payload.get("brief", ""),
                                     budget=payload.get("budget"), source=source)
        elif kind == "repo_proposal":
            ticket = self.add_repo(payload.get("pool_id", ""), payload.get("repo_id", ""),
                                   payload.get("path", ""), display_name=payload.get("display_name", ""),
                                   repo_link=payload.get("link") or payload.get("repo_link") or None,
                                   source=source)
        elif kind == "claim_proposal":
            ticket = self.claim(payload.get("ticket_id", ""), key_id=proposer,
                                declared_paths=payload.get("declared_paths") or [],
                                budget=payload.get("budget"), source=source)
        elif kind == "work_proposal":
            raise ValueError("work_proposal minting is gated on the Phase 4b transport spike")
        else:
            raise ValueError(f"unknown proposal kind: {kind!r}")
        L.record_event(self._conn, resolved_pool or "", "proposal_minted",
                       {"packet_hash": packet_hash, "proposer_key_id": proposer, "kind": kind})
        return ticket

    def _thread_ref_from(self, source=None):
        src = self._source_from(source)
        if src is None:
            return None
        chat_id = self._source_field(src, "chat_id", "parent_chat_id", "channel_id")
        thread_id = self._source_field(src, "thread_id")
        guild_id = self._source_field(src, "guild_id", "scope_id")
        if chat_id is None and thread_id is None and guild_id is None:
            return None
        return {"chat_id": chat_id, "thread_id": thread_id, "guild_id": guild_id}

    def _queue_notice(self, pool_id, text, kind="notice") -> None:
        L.queue_notice(self._conn, pool_id, text, kind=kind)

    def drain_notices(self, pool_id=None):
        """Unread notices accumulated since last drain (D5)."""
        return L.drain_notices(self._conn, pool_id=pool_id)

    def peek_notices(self, pool_id=None):
        return L.peek_notices(self._conn, pool_id=pool_id)

    # -- pool membership ----------------------------------------------------

    def register_pool(self, pool_id, name, relay_url="", key_id=None,
                      source=None, discord_user_id=None) -> dict:
        if L.is_global_frozen(self._conn):
            raise L.ClaimRefused(L.FROZEN_MESSAGE)
        if not discord_user_id:
            raise ValueError("register needs --discord-user <id>: the whitelist is the auth")
        maintainer = key_id or self._caller_key_id
        if not maintainer:
            raise ValueError("register needs a maintainer key_id")
        L.create_pool(self._conn, pool_id, name, maintainer, relay_url=relay_url, owner_discord_user_id=discord_user_id)
        # Record the maintainer's box pubkey so submit wraps the DEK for the
        # maintainer too (fixes "maintainer has no wrapped DEK" at audit).
        _irow = self._conn.execute(
            "SELECT box_pubkey_hex, verify_key_hex FROM identities WHERE key_id=?", (maintainer,)).fetchone()
        _box = _irow[0] if _irow else ""
        _vhex = _irow[1] if _irow else ""
        if not _box and self._caller_box_sk is not None and maintainer == self._caller_key_id:
            try:
                _box = self._caller_box_sk.public_key.encode().hex()
            except Exception:
                _box = ""
        L.add_member(self._conn, pool_id, maintainer, role="maintainer", box_pubkey_hex=_box or "",
                     verify_pubkey_hex=_vhex or "")
        L.authorize_user(self._conn, pool_id, discord_user_id)
        L.record_event(self._conn, pool_id, "pool_registered",
                       {"pool_id": pool_id, "maintainer": maintainer,
                        "owner_discord_user_id": str(discord_user_id)})
        # Attribution only: the invite-bound key for the owner's Discord id.
        # (No chat binding — the platform routes commands across gateways;
        # the plugin only selects among pools it hosts, 2109 Phase 2.)
        L.bind_discord_user(self._conn, maintainer, discord_user_id)
        return {"ok": True, "pool_id": pool_id, "name": name, "maintainer": maintainer,
                "owner_discord_user_id": str(discord_user_id)}

    def invite(self, pool_id, key_id, role="member", box_pubkey_hex="",
               source=None, discord_user_id=None, verify_pubkey_hex="") -> dict:
        pool_id = self._assert_local_owned(pool_id, source=source)
        # Owner-only (whitelist model): the whitelist IS the authorization.
        self._require_owner(pool_id, source)
        # Freeze is absolute: a frozen pool refuses every other verb from
        # every user with the exact message, before argument validation.
        self._check_not_frozen(pool_id)
        if role not in ("member", "maintainer"):
            raise ValueError(f"role must be member|maintainer, got {role!r}")
        if not discord_user_id:
            raise ValueError("invite needs --discord-user <id>: the whitelist is the auth")
        L.add_member(self._conn, pool_id, key_id, role=role, box_pubkey_hex=box_pubkey_hex,
                     verify_pubkey_hex=verify_pubkey_hex or "")
        L.bind_discord_user(self._conn, key_id, discord_user_id)
        L.authorize_user(self._conn, pool_id, discord_user_id)
        L.record_event(self._conn, pool_id, "invited",
                       {"key_id": key_id, "role": role})
        return {"ok": True, "pool_id": pool_id, "key_id": key_id, "role": role}

    def members(self, pool_id, source=None) -> list:
        pool_id = self._resolve_pool(pool_id, source=source)
        # Authorized-only read verb (whitelist model).
        self._require_pool_authorized(pool_id, source)
        return L.list_members(self._conn, pool_id)

    def set_auto_approve(self, pool_id, enabled, *, source=None) -> dict:
        """Owner-only auto-approve toggle (whitelist model)."""
        pool_id = self._assert_local_owned(pool_id, require_maintainer=False, source=source)
        # Freeze is absolute for every other verb from every user.
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        L.set_auto_approve(self._conn, pool_id, bool(enabled))
        L.record_event(self._conn, pool_id, "auto_approve_changed", {"enabled": bool(enabled)})
        return {"ok": True, "pool_id": pool_id, "auto_approve": bool(enabled)}


    def freeze(self, pool_id, frozen: bool, *, source=None) -> dict:
        self._require_owner(pool_id, source)
        L.set_frozen(self._conn, pool_id, frozen)
        notice = f"pool {pool_id} {'frozen' if frozen else 'unfrozen'}"
        self._queue_notice(pool_id, notice, kind="freeze")
        return {"ok": True, "pool_id": pool_id, "frozen": frozen, "notice": notice}

    def remove_member(self, pool_id, key_id, *, source=None) -> dict:
        """Revoke a member: registry removal + forward-separation notice.

        Future manifests omit the removed member's wrapped key (fresh DEK per
        version). Already-fetched versions are inherently unfetchable
        retroactively — the limitation is stated in the thread notice.
        """
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (pool_id,)).fetchone()
        if pool is None:
            raise ValueError(f"unknown pool: {pool_id!r}")
        removed = L.remove_member(self._conn, pool_id, key_id)
        # Drop the whitelist row if the invite-bound Discord id is known.
        try:
            _row = self._conn.execute("SELECT discord_user_id FROM identities WHERE key_id=?", (key_id,)).fetchone()
            if _row and _row[0]:
                L.deauthorize_user(self._conn, pool_id, _row[0])
        except Exception:
            pass
        notice = (f"member removed: {key_id} (forward separation only: "
                  f"{L.REVOCATION_LIMITATION})")
        self._queue_notice(pool_id, notice, kind="member_removed")
        removed = dict(removed)
        removed["notice"] = notice
        return removed

    def is_frozen(self, pool_id) -> bool:
        return L.is_frozen(self._conn, pool_id)

    def is_global_frozen(self) -> bool:
        return L.is_global_frozen(self._conn)

    def _check_not_frozen(self, pool_id) -> None:
        """Global kill-switch first, then per-pool freeze (Phase 6)."""
        L.check_not_frozen(self._conn, pool_id)

    def freeze_global(self, frozen: bool, *, source=None) -> dict:
        """GLOBAL kill-switch: freeze/unfreeze EVERY pool in the process.

        Owner-only (whitelist model): requires ownership of every hosted
        pool. Sets the ledger ``meta`` flag (persists across restarts) and
        mirrors it into each pool's per-pool flag so ``is_frozen`` stays
        truthful. While set, all pool-mutating and claim/work/integrate
        actions raise (see ``_check_not_frozen``); only this command (to
        unfreeze) and read-only queries are allowed. Notice posted to every
        pool channel.
        """
        hosts = [r[0] for r in self._conn.execute("SELECT pool_id FROM pools")]
        for _h in hosts:
            self._require_owner(_h, source)
        L.set_global_frozen(self._conn, frozen)
        pools = hosts
        notices = []
        for pool_id in pools:
            L.set_frozen(self._conn, pool_id, frozen)
            notice = (f"pool {pool_id} {'frozen' if frozen else 'unfrozen'} "
                      f"(global kill-switch {'set' if frozen else 'cleared'})")
            self._queue_notice(pool_id, notice, kind="freeze")
            notices.append(notice)
        return {"ok": True, "global_frozen": bool(frozen), "pools": pools,
                "notice": "; ".join(notices) if notices else ""}

    # -- tickets --------------------------------------------------------------

    def add_ticket(self, pool_id, repo_id, title, brief="", budget=None,
                   source=None) -> dict:
        if not title.strip():
            raise ValueError("ticket title is required")
        pool_id = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        self._assert_local_owned(pool_id, source=source)
        L.ensure_repo(self._conn, repo_id, pool_id)
        full_budget = L.normalize_budget(budget, self._budget_defaults)
        ticket_id = f"t-{secrets.token_hex(4)}"
        thread_ref = self._thread_ref_from(source)
        thread_ref_str = (f"chat:{thread_ref['chat_id']}:thread:{thread_ref['thread_id']}:"
                          f"guild:{thread_ref['guild_id']}" if thread_ref else None)
        state = "open" if L.get_auto_approve(self._conn, pool_id) else "pending"
        L.create_ticket(self._conn, ticket_id, pool_id, repo_id, title.strip(),
                        brief=brief, budget=full_budget, thread_ref=thread_ref_str, state=state)
        ticket = L.get_ticket(self._conn, ticket_id)
        L.record_event(self._conn, pool_id, "ticket_created",
                       {"ticket_id": ticket_id, "title": title,
                        "thread_ref": thread_ref_str})
        ticket = dict(ticket)
        ticket["thread_ref"] = thread_ref_str
        ticket["thread_ref_parts"] = thread_ref
        ticket["notice"] = (f"{STATE_MARKER.format(state=ticket.get('state','open'))} ticket {ticket_id}: "
                            f"{title}")
        self._queue_notice(pool_id, ticket["notice"], kind="ticket_created")
        return ticket

    def list_tickets(self, pool_id=None, state=None, source=None, repo_id=None) -> list:
        if repo_id:
            repo = L.get_repo(self._conn, repo_id)
            if repo is None:
                raise ValueError(f"unknown repo: {repo_id!r}")
            if pool_id:
                pool_id = self._resolve_pool(pool_id, source=source)
                if repo["pool_id"] != pool_id:
                    raise PoolNotLocalError(
                        f"pool_not_local: repo {repo_id!r} belongs to pool "
                        f"{repo['pool_id']!r}, not pool {pool_id!r}",
                        pool_id=repo["pool_id"])
            else:
                pool_id = repo["pool_id"]
        elif pool_id:
            pool_id = self._resolve_pool(pool_id, source=source)
        else:
            try:
                pool_id = self._resolve_pool(None, source=source)
            except PoolNotLocalError:
                pool_id = None
        # Authorized-only read verb (whitelist model).
        self._require_pool_authorized(pool_id, source)
        return self._filter_pending(L.list_tickets(self._conn, pool_id=pool_id, state=state, repo_id=repo_id), state=state, pool_id=pool_id, source=source)

    def _require_pool_authorized(self, pool_id, source) -> None:
        """Authorized-only read verb (2109): `/task list|show`,
        `/pool list|members`, `/audit show` are not public."""
        if not pool_id:
            return
        self._require_authorized(pool_id, source)

    def _is_bound_maintainer(self, pool_id, source=None) -> bool:
        # Whitelist model: pending visibility is owner-only, checked by
        # Discord id, not gateway key. Kept name for pending-filter callers.
        if not pool_id:
            return False
        src = self._source_from(source)
        user_id = self._source_field(src, "user_id", "discord_user_id")
        if user_id is None:
            return False
        return L.is_owner(self._conn, pool_id, str(user_id))

    def _discord_user(self, source) -> str | None:
        src = self._source_from(source)
        user_id = self._source_field(src, "user_id", "discord_user_id")
        return str(user_id) if user_id is not None else None

    def _invite_bound_key(self, source) -> str | None:
        """Invite-bound agent key for the driving Discord user (OD1).

        Attribution only — crypto records WHO acted (overlap checks,
        manifests, notices). The Discord id is the authorization; this
        lookup never decides access.
        """
        user_id = self._discord_user(source)
        if user_id is None:
            return None
        try:
            return L.lookup_key_by_discord_user(self._conn, user_id)
        except Exception:
            return None

    def _require_owner(self, pool_id, source) -> None:
        # Legacy bare fixtures (no owner/whitelist rows) skip the check;
        # whitelisted pools are strict (production path).
        try:
            _pool = self._conn.execute("SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
            _has = _pool is not None and _pool[0]
            _n = self._conn.execute("SELECT COUNT(*) FROM pool_authorized WHERE pool_id=?", (pool_id,)).fetchone()[0]
        except Exception:
            _has, _n = False, 0
        if not _has and not _n:
            return
        user_id = self._discord_user(source)
        if user_id is None or not L.is_owner(self._conn, pool_id, user_id):
            raise PermissionError(f"not authorized for pool {pool_id} — ask the maintainer")

    def _require_authorized(self, pool_id, source) -> None:
        try:
            _pool = self._conn.execute("SELECT owner_discord_user_id FROM pools WHERE pool_id=?", (pool_id,)).fetchone()
            _has = _pool is not None and _pool[0]
            _n = self._conn.execute("SELECT COUNT(*) FROM pool_authorized WHERE pool_id=?", (pool_id,)).fetchone()[0]
        except Exception:
            _has, _n = False, 0
        if not _has and not _n:
            return
        user_id = self._discord_user(source)
        if user_id is None or not L.is_authorized(self._conn, pool_id, user_id):
            raise PermissionError(f"not authorized for pool {pool_id} — ask the maintainer")

    def _filter_pending(self, rows, state=None, pool_id=None, source=None):
        """Pending hidden from non-maintainers unless explicitly requested."""
        if state is not None:
            return rows
        if pool_id and self._is_bound_maintainer(pool_id, source=source):
            return rows
        # If the caller is maintainer of any pool in the result set, keep.
        tickets = [r for r in rows if r.get("state") != "pending"]
        if pool_id is None and rows:
            # Check if caller is maintainer of every pool present.
            pools = {r.get("pool_id") for r in rows if r.get("pool_id")}
            if pools and all(self._is_bound_maintainer(p, source=source) for p in pools):
                return rows
        return tickets

    def list_pending(self, pool_id=None, source=None) -> list:
        pool_id = self._resolve_pool(pool_id, source=source) if pool_id else None
        return L.list_tickets(self._conn, pool_id=pool_id, state="pending")

    def approve_ticket(self, ticket_id, decision, *, source=None, edit_files=None, edit_patch=None, reason="") -> dict:
        """Owner decision on a pending ticket (whitelist model)."""
        decision = str(decision).strip().lower()
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        self._require_owner(ticket["pool_id"], source)
        if ticket["state"] != "pending":
            raise ValueError(f"approve/deny needs a pending ticket, got {ticket['state']!r}")
        if decision == "approve":
            updated = L.set_ticket_state(self._conn, ticket_id, "open")
            L.record_event(self._conn, ticket["pool_id"], "ticket_approved", {"ticket_id": ticket_id})
            notice = f"{STATE_MARKER.format(state='open')} ticket {ticket_id} approved by maintainer"
            self._queue_notice(ticket["pool_id"], notice, kind="ticket_approved")
            return {"ok": True, "ticket_id": ticket_id, "decision": "approve", "ticket_state": "open", "notice": notice}
        if decision == "deny":
            updated = L.set_ticket_state(self._conn, ticket_id, "blocked")
            L.record_event(self._conn, ticket["pool_id"], "ticket_denied", {"ticket_id": ticket_id, "reason": reason})
            notice = f"{STATE_MARKER.format(state='blocked')} ticket {ticket_id} denied: {reason or 'denied by maintainer'}"
            self._queue_notice(ticket["pool_id"], notice, kind="ticket_denied")
            return {"ok": True, "ticket_id": ticket_id, "decision": "deny", "ticket_state": "blocked", "notice": notice}
        if decision == "modify":
            # Re-prompt: apply optional edits then approve (title/brief update).
            if edit_files:
                L.record_event(self._conn, ticket["pool_id"], "ticket_modified", {"ticket_id": ticket_id, "edits": list(edit_files)})
            updated = L.set_ticket_state(self._conn, ticket_id, "open")
            L.record_event(self._conn, ticket["pool_id"], "ticket_approved", {"ticket_id": ticket_id, "modified": True})
            notice = f"{STATE_MARKER.format(state='open')} ticket {ticket_id} modified and approved by maintainer"
            self._queue_notice(ticket["pool_id"], notice, kind="ticket_approved")
            return {"ok": True, "ticket_id": ticket_id, "decision": "modify", "ticket_state": "open", "notice": notice}
        raise ValueError(f"decision must be approve|deny|modify, got {decision!r}")

    def resolve_pending(self, kind, item_id, decision, **kwargs) -> dict:
        """Single decision dispatcher (Phase 4): ticket | repo | claim | work."""
        kind = str(kind).strip().lower()
        if kind == "ticket":
            return self.approve_ticket(item_id, decision, **kwargs)
        if kind == "repo":
            # Repo approval = accept pending version (maintainer-signed).
            if decision.strip().lower() == "approve":
                from .propagation import RepoPropagator as _RP  # noqa
                raise ValueError("repo approve reuses /integrate gate; use /integrate approve")
            raise ValueError(f"repo {decision!r} not supported here")
        if kind == "claim":
            raise ValueError("claim decisions use the claim mint path (first-minted wins)")
        if kind == "work":
            raise ValueError("work decisions gated on Phase 4b spike")
        raise ValueError(f"unknown kind: {kind!r}")

    # -- project / repo registry ----------------------------------------------

    def add_repo(self, pool_id, repo_id, path, display_name="", source=None, repo_link=None, link=None) -> dict:
        """Register a project and submit its bytes as the next repo version.

        Registry row (upsert) + propagation submit (pack -> BLAKE3 -> DEK
        wrap -> Iroh send -> signed manifest appended to the chain).
        Re-running submits a NEW version (next_repo_version), never refuses.
        ``path`` is required: a project without bytes is not a project.

        With ``repo_link`` (``--link``): register-link form — records the
        link/ticket reference and signs intent WITHOUT cloning/fetching.
        Fetch is deferred to /work start. No RepoPropagator.fetch is called.
        """
        repo_link = repo_link or link
        if repo_link and not path:
            if not str(repo_link).strip():
                raise ValueError("add-repo --link needs a non-empty URL")
            pool_id = self._resolve_pool(pool_id, source=source)
            self._require_owner(pool_id, source)
            self._assert_local_owned(pool_id, source=source)
            self._check_not_frozen(pool_id)
            if not repo_id:
                raise ValueError("add-repo needs a repo_id")
            # Link is a pointer stored only in remote_repos — never in repos.path.
            L.upsert_repo(self._conn, repo_id, pool_id, path=None,
                          display_name=display_name or None,
                          submitter_key_id=self._caller_key_id or None)
            L.upsert_remote_repo(self._conn, repo_id, pool_id, remote_repo_id=repo_id, link=repo_link)
            L.record_event(self._conn, pool_id, "repo_link_registered",
                           {"repo_id": repo_id, "link": repo_link})
            notice = f"project {repo_id} link registered: {repo_link}; fetch deferred to /work start"
            self._queue_notice(pool_id, notice, kind="repo_link_registered")
            return {"ok": True, "repo_id": repo_id, "pool_id": pool_id, "link": repo_link,
                    "registered_link": True, "fetched": False, "notice": notice}
        if not path:
            raise ValueError(
                "add-repo needs --path <abs/path>: a project without bytes "
                "is not a project; pass the local checkout to submit")
        if not repo_id:
            raise ValueError("add-repo needs a repo_id (or --path to derive one)")
        pool_id = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(pool_id)
        self._require_owner(pool_id, source)
        self._assert_local_owned(pool_id, source=source)
        repo_path = Path(path)
        if not repo_path.is_dir():
            raise ValueError(f"repo path is not a directory: {path!r}")
        if self._caller_sk is None:
            raise RuntimeError("add-repo needs the local signing key")
        L.upsert_repo(self._conn, repo_id, pool_id,
                      path=str(repo_path.resolve()),
                      display_name=display_name or None,
                      submitter_key_id=self._caller_key_id or None)
        version = L.next_repo_version(self._conn, repo_id)
        prop = self._propagator(pool_id)
        submitter = prop._members.get(self._caller_key_id)
        if submitter is None:
            raise PermissionError(
                f"add-repo refused: caller {self._caller_key_id!r} is not a pool member")
        result = prop.submit(repo_path=repo_path, repo_id=repo_id, version=version,
                             submitter=submitter,
                             submitter_signing_key=self._caller_sk)
        L.record_event(self._conn, pool_id, "repo_added",
                       {"repo_id": repo_id, "version": version,
                        "state": result["state"],
                        "manifest_hash": result["manifest_hash"]})
        # DEK timing: members invited AFTER this version have no wrapped
        # key for it and must wait for the next version.
        members_with_box = [m for m in prop._members.values() if m.box_pubkey_hex]
        wrapped = set((result["manifest"].get("wrapped_keys") or {}).keys())
        missing = [m.key_id for m in members_with_box if m.key_id not in wrapped]
        notice = (f"project {repo_id} registered: v{version} "
                  f"{result['state']} ({result['manifest_hash'][:12]})")
        if result["state"] == "pending":
            notice += "; awaiting maintainer accept via /integrate"
        if missing:
            notice += (f"; WARNING: {len(missing)} member(s) have no DEK key "
                       f"for this version ({', '.join(sorted(missing))}) — invite "
                       "members BEFORE /add-repo; late joiners wait for the "
                       "next version")
        else:
            notice += ("; invite members BEFORE /add-repo — members invited "
                       "later wait for the next version")
        return {"ok": True, "repo_id": repo_id, "pool_id": pool_id,
                "version": version, "state": result["state"],
                "manifest_hash": result["manifest_hash"],
                "transfer_id": result.get("transfer_id", ""),
                "notice": notice}

    def list_projects(self, pool_id=None, source=None) -> list:
        """Hosted-pool-only project listing for /pool list.

        Authorized-only read verb (whitelist model); when no pool is
        named and the gateway hosts several, every hosted pool the
        caller is authorized for is listed.
        """
        if pool_id:
            resolved = self._resolve_pool(pool_id, source=source)
            self._require_pool_authorized(resolved, source)
            return self._project_rows(resolved)
        try:
            bound = self._resolve_pool(None, source=source)
            self._require_pool_authorized(bound, source)
            return self._project_rows(bound)
        except PoolNotLocalError:
            pass
        projects: list = []
        for pid in L.list_local_pools(self._conn):
            if self._is_authorized_or_legacy(pid, source):
                projects.extend(self._project_rows(pid))
        return projects

    def _is_authorized_or_legacy(self, pool_id, source) -> bool:
        """True when the caller may read *pool_id*: authorized, or the
        pool is a legacy bare fixture with no whitelist rows."""
        try:
            _pool = self._conn.execute(
                "SELECT owner_discord_user_id FROM pools WHERE pool_id=?",
                (pool_id,)).fetchone()
            _n = self._conn.execute(
                "SELECT COUNT(*) FROM pool_authorized WHERE pool_id=?",
                (pool_id,)).fetchone()[0]
        except Exception:
            return False
        if not (_pool is not None and _pool[0]) and not _n:
            return True
        user_id = self._discord_user(source)
        return user_id is not None and L.is_authorized(self._conn, pool_id, user_id)

    def _project_rows(self, pool_id) -> list:
        rows = []
        for r in L.list_repos(self._conn, pool_id):
            accepted = r.get("accepted_version")
            row = {"repo_id": r["repo_id"], "pool_id": r["pool_id"],
                   "display_name": r["display_name"], "name": r["name"],
                   "accepted_version": accepted,
                   "open_tickets": r["open_tickets"],
                   "claimed_tickets": r["claimed_tickets"],
                   "total_tickets": r["total_tickets"],
                   "submitter_key_id": r.get("submitter_key_id") or ""}
            if accepted is None:
                row["note"] = ("registered via ticket, no bytes submitted "
                               "yet — run /add-repo --path <checkout>")
            rows.append(row)
        return rows

    def show_ticket(self, ticket_id) -> dict:
        t = L.get_ticket(self._conn, ticket_id)
        if t is not None:
            return t
        raise ValueError(f"unknown ticket: {ticket_id!r}")

    def _set_state_and_notify(self, ticket, state) -> dict:
        updated = L.set_ticket_state(self._conn, ticket["ticket_id"], state)
        notice = (f"{STATE_MARKER.format(state=state)} ticket "
                  f"{updated['ticket_id']}: {updated['title']}")
        self._queue_notice(updated["pool_id"], notice, kind="ticket_state")
        updated = dict(updated)
        updated["notice"] = notice
        return updated

    # -- claims ---------------------------------------------------------------

    def claim(self, ticket_id, key_id=None, declared_paths=None, budget=None,
              maintainer_override=False, *, source=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        # Whitelist: only authorized users may claim (OD1: claimant stored as
        # invite-bound agent key; the lookup is attribution, not authz).
        self._require_authorized(ticket["pool_id"], source)
        # Resolve claimant key: explicit > invite-bound (attribution) >
        # gateway identity for bare fixtures / CLI.
        key = key_id or self._invite_bound_key(source) or self._caller_key_id
        if not key:
            raise ValueError("claim needs a claimant key_id")
        claim = L.claim_ticket_enforced(
            self._conn, f"c-{secrets.token_hex(4)}", ticket_id, key,
            declared_paths=declared_paths, budget=budget,
            maintainer_override=maintainer_override)
        ticket = L.get_ticket(self._conn, ticket_id)
        notice = (f"{STATE_MARKER.format(state='claimed')} ticket {ticket_id} "
                  f"claimed by {key}")
        self._queue_notice(ticket["pool_id"], notice, kind="ticket_claim")
        claim = dict(claim)
        claim["notice"] = notice
        claim["state_word"] = "claimed"
        return claim

    def claim_any(self, pool_id=None, repo_id=None, max_tasks=1, key_id=None,
                  budget=None, maintainer_override=False, source=None) -> dict:
        """Claim up to ``max_tasks`` open tickets, whole-ticket form (no
        paths pre-declared), across any project in the pool (or one repo).

        Every individual claim still goes through the enforced path
        (freeze, one-ticket-one-worker, overlap, budget), so the bound is
        ``max_tasks`` successful claims. Tickets that refuse (e.g. a live
        file claim on the same repo) are reported as skipped and the
        search continues. Bounded hard at 10 tickets per call.
        """
        max_tasks = int(max_tasks or 1)
        if max_tasks < 1:
            raise ValueError("max_tasks must be >= 1")
        max_tasks = min(max_tasks, 10)
        key = key_id or self._caller_key_id
        if not key:
            raise ValueError("claim needs a claimant key_id")
        open_tickets = self.list_tickets(pool_id=pool_id, state="open",
                                         source=source, repo_id=repo_id)
        claimed, skipped = [], []
        for ticket in open_tickets:
            if len(claimed) >= max_tasks:
                break
            try:
                c = self.claim(ticket["ticket_id"], key_id=key,
                               declared_paths=[], budget=budget,
                               maintainer_override=maintainer_override, source=source)
                claimed.append({"ticket_id": ticket["ticket_id"],
                                "title": ticket["title"],
                                "repo_id": ticket["repo_id"],
                                "claim_id": c.get("claim_id", "")})
            except (L.ClaimRefused, ValueError) as exc:
                skipped.append({"ticket_id": ticket["ticket_id"],
                                "reason": str(exc)})
        notice = (f"claim-any: claimed {len(claimed)} ticket(s)"
                  + (f"; skipped {len(skipped)}" if skipped else ""))
        return {"ok": True, "claimed": claimed, "skipped": skipped,
                "count": len(claimed), "notice": notice}

    def release(self, ticket_id, key_id=None, *, source=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        # Whitelist: authorized-level verb.
        self._require_authorized(ticket["pool_id"], source)
        key = key_id or self._invite_bound_key(source) or self._caller_key_id
        updated = L.release_ticket(self._conn, ticket_id, key)
        notice = f"{STATE_MARKER.format(state='open')} ticket {ticket_id} released"
        self._queue_notice(updated["pool_id"], notice, kind="ticket_release")
        updated = dict(updated)
        updated["notice"] = notice
        return updated

    # -- Phase 4: agent execution --------------------------------------------

    def report_progress(self, ticket_id, text) -> dict:
        """Milestone progress (4 KB guard, no bytes)."""
        ticket = self.show_ticket(ticket_id)
        short = f"ticket {ticket_id}: {text}"[:3500]
        self._queue_notice(ticket["pool_id"], short, kind="progress")
        return {"ok": True, "ticket_id": ticket_id, "text": short, "notice": short}

    def _budget_exhausted(self, ticket, reason, usage) -> dict:
        """Structured waiting/blocked report + claim released (re-armable).

        The ticket goes claimed -> blocked with a machine-readable notice;
        the claim row is released so the ticket can be re-claimed later
        (blocked -> open is a legal transition).
        """
        report = (f"{STATE_MARKER.format(state='blocked')} [blocked] {reason} "
                  f"usage={json.dumps(usage or {}, sort_keys=True)} "
                  f"claim released; ticket re-armable")
        self._queue_notice(ticket["pool_id"], report[:3500], kind="blocked_report")
        L.release_claim_for_ticket(self._conn, ticket["ticket_id"])
        if ticket["state"] == "claimed":
            L.set_ticket_state(self._conn, ticket["ticket_id"], "blocked")
        L.record_event(self._conn, ticket["pool_id"], "budget_exhausted",
                       {"ticket_id": ticket["ticket_id"], "reason": str(reason),
                        "usage": usage or {}})
        self._workers.pop(ticket["ticket_id"], None)
        return {"ok": False, "ticket_id": ticket["ticket_id"], "state": "blocked",
                "reason": str(reason), "usage": usage or {},
                "claim": "released", "notice": report[:3500]}

    def start_work(self, ticket_id, *, work_root, lifecycle=None, runner=None,
                   worker_command=None, acceptance_cmd=None) -> dict:
        """Fetch latest ACCEPTED version over Iroh, create the ticket worktree,
        and spawn the worker session seeded with mandate + budget + sandbox.

        ``runner`` overrides backend selection (tests). With no runner and no
        Hermes ``lifecycle``, a LocalProcessRunner over ``worker_command``
        is used (CLI / exit-demo path).
        """
        ticket = self.show_ticket(ticket_id)
        self._check_not_frozen(ticket["pool_id"])
        if ticket["state"] != "claimed":
            raise ValueError(f"start work needs a claimed ticket, got {ticket['state']!r}")
        if ticket["claimant_key_id"] != self._caller_key_id:
            raise PermissionError("start work: only the claimant may start work")
        if ticket_id in self._workers:
            raise ValueError(f"a worker is already running for ticket {ticket_id!r}")
        claim = L.get_active_claim(self._conn, ticket_id)
        if claim is None:
            raise ValueError(f"no active claim for ticket {ticket_id!r}")
        budget = json.loads(claim["budget_json"] or "{}")
        manifest = L.get_latest_accepted_manifest(self._conn, ticket["repo_id"])
        if manifest is None:
            raise ValueError(f"no ACCEPTED version for repo {ticket['repo_id']!r} to fetch")

        prop = self._propagator(ticket)
        fetcher = prop._members.get(self._caller_key_id)
        if fetcher is None or fetcher.box_sk is None:
            raise PermissionError("start work needs the claimant's local box key for DEK unwrap")
        work_root = Path(work_root)
        fetched = prop.fetch(manifest=manifest, fetcher=fetcher,
                             dest=work_root / "fetch")
        wt = create_worktree(fetched["clone_dir"], ticket_id, work_root / "worktrees")
        sandbox = SandboxPolicy()
        mandate = build_mandate_brief(ticket=ticket, budget=budget, worktree=wt["worktree"],
                                      branch=wt["branch"], sandbox=sandbox)
        if runner is None:
            if lifecycle is not None:
                runner = LifecycleWorkerRunner(lifecycle)
            elif worker_command:
                runner = LocalProcessRunner(list(worker_command), sandbox=sandbox)
            else:
                raise ValueError("start work needs a worker_command or Hermes lifecycle")
        handle = runner.launch(mandate, worktree=wt["worktree"])
        tracker = BudgetTracker(budget)
        self._workers[ticket_id] = {
            "handle": handle, "runner": runner, "tracker": tracker,
            "worktree": wt["worktree"], "branch": wt["branch"],
            "clone_dir": wt["clone_dir"], "fetched": fetched,
            "acceptance_cmd": list(acceptance_cmd or []),
            "mandate": mandate, "budget": budget,
        }
        L.record_event(self._conn, ticket["pool_id"], "worker_started",
                       {"ticket_id": ticket_id, "branch": wt["branch"],
                        "worktree": wt["worktree"],
                        "content_hash": fetched.get("content_hash")})
        self.report_progress(ticket_id,
                             f"worker started on {wt['branch']} "
                             f"(accepted v{manifest['version']}, sandbox={sandbox.describe()[:80]}...)")
        return {"ok": True, "ticket_id": ticket_id, "branch": wt["branch"],
                "worktree": wt["worktree"], "repo_version": manifest["version"],
                "content_hash": fetched.get("content_hash"), "mandate": mandate}

    def complete_work(self, ticket_id, *, timeout_seconds=None,
                      poll_interval=0.05) -> dict:
        """Wait for the worker, enforce budget, run acceptance tests sandboxed,
        then submit the contributor-signed manifest (ticket -> review)."""
        record = self._workers.get(ticket_id)
        if record is None:
            raise ValueError(f"no running worker for ticket {ticket_id!r}")
        ticket = self.show_ticket(ticket_id)
        self._check_not_frozen(ticket["pool_id"])
        runner, handle, tracker = record["runner"], record["handle"], record["tracker"]
        try:
            status = run_worker_loop(runner, handle, tracker,
                                     poll_interval=poll_interval,
                                     max_polls=None if timeout_seconds is None else
                                     max(1, int(timeout_seconds / max(poll_interval, 0.001))))
        except BudgetExhausted as exc:
            return self._budget_exhausted(ticket, exc.reason, exc.usage)
        usage = tracker.usage(status.get("usage") or {})
        if status.get("state") != "done":
            return self._budget_exhausted(
                ticket, f"worker failed: {(status.get('summary') or '')[:200]}", usage)
        self.report_progress(ticket_id,
                             f"worker done (turns={usage['turns']}, "
                             f"wall={usage['wall_clock_seconds']}s); running acceptance tests")
        acceptance = run_acceptance(record["worktree"], record["acceptance_cmd"])
        if not acceptance["ok"]:
            self.report_progress(
                ticket_id, f"acceptance FAILED rc={acceptance['returncode']}: "
                           f"{acceptance['output'][-500:]}")
            return self._budget_exhausted(
                ticket, f"acceptance tests failed (rc={acceptance['returncode']}, "
                        f"sandbox={acceptance['sandbox']})", usage)
        version = L.next_repo_version(self._conn, ticket["repo_id"])
        result = self.work_submit(ticket_id, record["worktree"], version)
        self._workers.pop(ticket_id, None)
        L.record_event(self._conn, ticket["pool_id"], "worker_completed",
                       {"ticket_id": ticket_id, "branch": record["branch"],
                        "manifest_hash": result["manifest_hash"], "usage": usage})
        return {"ok": True, "ticket_id": ticket_id, "state": "review",
                "manifest_hash": result["manifest_hash"], "version": version,
                "branch": record["branch"], "acceptance": acceptance, "usage": usage}

    def cancel_work(self, ticket_id, reason="cancelled by operator") -> dict:
        record = self._workers.get(ticket_id)
        if record is None:
            raise ValueError(f"no running worker for ticket {ticket_id!r}")
        record["runner"].cancel(record["handle"], reason)
        ticket = self.show_ticket(ticket_id)
        return self._budget_exhausted(ticket, reason, record["tracker"].usage())

    # -- work submit / integrate (real Phase 2 flow) --------------------------

    def _members_for_propagator(self, pool_id) -> dict:
        """Member map from the members table + identities; private box key only
        for the local caller (never persisted here)."""
        members = {}
        for row in self._conn.execute(
                "SELECT m.key_id, m.role, i.verify_key_hex, m.box_pubkey_hex,"
                " i.box_pubkey_hex, m.verify_pubkey_hex"
                " FROM members m LEFT JOIN identities i ON i.key_id = m.key_id"
                " WHERE m.pool_id=?", (pool_id,)):
            box_sk = self._caller_box_sk if row[0] == self._caller_key_id else None
            members[row[0]] = Member(key_id=row[0], role=row[1] or "member",
                                     verify_key_hex=row[5] or row[2] or "",
                                     box_pubkey_hex=row[3] or row[4] or "", box_sk=box_sk)
        if self._caller_key_id and self._caller_key_id not in members:
            members[self._caller_key_id] = Member(
                key_id=self._caller_key_id, role="member",
                box_pubkey_hex="", box_sk=self._caller_box_sk)
        return members

    def _propagator(self, ticket_or_pool) -> RepoPropagator:
        """Build the propagator for a pool. Accepts a ticket dict (legacy)
        or a pool_id string (used by /add-repo, which has no ticket)."""
        if self._transport is None:
            raise RuntimeError("work submit / integrate need a transport backend")
        pool_id = (ticket_or_pool["pool_id"] if isinstance(ticket_or_pool, dict)
                   else str(ticket_or_pool))
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (pool_id,)).fetchone()
        if pool is None:
            raise RuntimeError(f"unknown pool: {pool_id!r}")
        maintainer_id = pool["maintainer_key_id"]
        members = self._members_for_propagator(pool_id)
        if maintainer_id not in members:
            raise RuntimeError(f"maintainer {maintainer_id!r} is not a pool member")
        maintainer_sk = self._caller_sk
        # RepoPropagator only uses maintainer_signing_key in accept() (Phase 5);
        # submit/audit run as the caller. The caller's key is a safe stand-in
        # until the maintainer gate lands.
        return RepoPropagator(
            self._conn, self._transport_overrides.get(pool_id, self._transport),
            pool_id=pool_id,
            maintainer_key_id=maintainer_id, maintainer_signing_key=maintainer_sk,
            members=members)

    def work_submit(self, ticket_id, repo_path, version, key_id=None, *, source=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        # Whitelist: authorized-level verb (attribution via invite-bound key).
        self._require_authorized(ticket["pool_id"], source)
        key = key_id or self._invite_bound_key(source) or self._caller_key_id
        if ticket["state"] != "claimed":
            raise ValueError(f"work submit needs a claimed ticket, got {ticket['state']!r}")
        if ticket["claimant_key_id"] != key:
            raise PermissionError("work submit: only the claimant may submit work")
        if self._caller_sk is None:
            raise RuntimeError("work submit needs the local signing key")
        prop = self._propagator(ticket)
        members = prop._members
        submitter = members.get(key)
        if submitter is None:
            raise PermissionError(f"submit refused: {key!r} is not a pool member")
        result = prop.submit(repo_path=Path(repo_path), repo_id=ticket["repo_id"],
                             version=int(version), submitter=submitter,
                             submitter_signing_key=self._caller_sk)
        if result["state"] == "pending":
            self._set_state_and_notify(ticket, "review")
        # Maintainer self-accept leaves the ticket claimed for integrate.
        return {"ok": True, "ticket_id": ticket_id, "version": int(version),
                "manifest_hash": result["manifest_hash"], "state": result["state"],
                "notice": result.get("notice", "")}

    def _default_work_dir(self):
        return Path.home() / ".hermes" / "plugin-data" / "agent-pool" / "work" / "integrate"

    def _integration_ctx_path(self, work_dir, ticket_id):
        return Path(work_dir) / f"gate-{ticket_id}.json"

    def _load_integration_ctx(self, ticket_id, work_dir):
        ctx = self._integrations.get(ticket_id)
        if ctx is not None:
            return ctx
        path = self._integration_ctx_path(work_dir, ticket_id)
        if path.exists():
            ctx = json.loads(path.read_text())
            self._integrations[ticket_id] = ctx
            return ctx
        return None

    def integrate(self, ticket_id, key_id=None, *, work_dir=None, source=None) -> dict:
        """Phase 5 full gate: fetch branch -> dialog scan -> audit -> buttons.

        Steps (all real, network-off scan):
        a. fetch the contributor BRANCH bundle (``RepoPropagator.fetch_pending``)
           and the canonical baseline (latest accepted) over the transport;
        b. sandboxed scan of the branch DIFF (``integration.scan_diff``,
           bwrap ``--unshare-net``) — containment-by-construction;
        c. audit report posted to the ticket thread / pool channel;
        d. if clean: return approve/edit/discard decision prompt and
           keep the ticket in ``review`` awaiting a human decision; if the
           scan is FLAGGED the verdict blocks the gate: no buttons are posted,
           the ticket goes to ``blocked`` and canonical is never touched.
        """
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        self._require_owner(ticket["pool_id"], source)
        key = key_id or self._caller_key_id
        pool = self._conn.execute("SELECT maintainer_key_id FROM pools WHERE pool_id=?",
                                  (ticket["pool_id"],)).fetchone()
        if pool is None:
            raise ValueError(f"unknown pool: {ticket['pool_id']!r}")
        key = pool[0]
        pending = L.get_pending_manifest(self._conn, ticket["repo_id"])
        if pending is None:
            raise ValueError(f"no pending submission for repo {ticket['repo_id']!r}")
        prop = self._propagator(ticket)
        maintainer = prop._members[key]
        work_dir = Path(work_dir) if work_dir else self._default_work_dir()
        work_dir.mkdir(parents=True, exist_ok=True)
        audit = prop.audit(manifest=pending, maintainer=maintainer)
        report = audit["report"]

        # (a) fetch branch bundle over the transport.
        branch = prop.fetch_pending(manifest=pending, maintainer=maintainer,
                                    dest=work_dir / f"branch-{ticket_id}")
        # Canonical baseline: clone of the latest accepted bundle.
        accepted = L.get_latest_accepted_manifest(self._conn, ticket["repo_id"])
        canon_dir = work_dir / f"canonical-{ticket['repo_id']}"
        if accepted is not None and not canon_dir.exists():
            fetched = prop.fetch(manifest=accepted, fetcher=maintainer,
                                 dest=work_dir / f"canon-fetch-{ticket['repo_id']}")
            subprocess_clone = fetched["clone_dir"]
            import shutil as _shutil
            _shutil.copytree(subprocess_clone, canon_dir)
            subprocess.run(["git", "-C", str(canon_dir), "config", "user.email", "agent-pool@local"],
                           capture_output=True, text=True, timeout=60)
            subprocess.run(["git", "-C", str(canon_dir), "config", "user.name", "agent-pool"],
                           capture_output=True, text=True, timeout=60)
        if not canon_dir.exists() and accepted is None:
            # First-ever submission with no accepted baseline: canonical
            # starts as a copy of the branch tip (merge is up-to-date).
            import shutil as _shutil
            _shutil.copytree(branch["clone_dir"], canon_dir)
            subprocess.run(["git", "-C", str(canon_dir), "config", "user.email", "agent-pool@local"],
                           capture_output=True, text=True, timeout=60)
            subprocess.run(["git", "-C", str(canon_dir), "config", "user.name", "agent-pool"],
                           capture_output=True, text=True, timeout=60)
        if canon_dir.exists():
            # Stale canonical from a different repo/ledger (shared work dir
            # across runs): rebuild it if it shares no commit with the branch.
            branch_repo = Path(branch["clone_dir"])
            probe = subprocess.run(
                ["git", "-C", str(branch_repo), "cat-file", "-e", "HEAD^{commit}"],
                capture_output=True, text=True, timeout=60)
            canon_commits = subprocess.run(
                ["git", "-C", str(canon_dir), "rev-list", "HEAD"],
                capture_output=True, text=True, timeout=60).stdout.split()
            common = False
            for sha in canon_commits:
                chk = subprocess.run(
                    ["git", "-C", str(branch_repo), "cat-file", "-e", f"{sha}^{{commit}}"],
                    capture_output=True, text=True, timeout=60)
                if chk.returncode == 0:
                    common = True
                    break
            if not common:
                import shutil as _shutil
                _shutil.rmtree(canon_dir)
                if accepted is not None:
                    fetched = prop.fetch(manifest=accepted, fetcher=maintainer,
                                         dest=work_dir / f"canon-refetch-{ticket['repo_id']}")
                    _shutil.copytree(fetched["clone_dir"], canon_dir)
                else:
                    _shutil.copytree(branch["clone_dir"], canon_dir)
                subprocess.run(["git", "-C", str(canon_dir), "config", "user.email", "agent-pool@local"],
                               capture_output=True, text=True, timeout=60)
                subprocess.run(["git", "-C", str(canon_dir), "config", "user.name", "agent-pool"],
                               capture_output=True, text=True, timeout=60)
        if not (canon_dir / ".git").exists():
            raise RuntimeError("canonical checkout unavailable for repo "
                               f"{ticket['repo_id']!r} (no accepted baseline)")
        # (b) sandboxed diff scan.
        diff_report = scan_diff(Path(branch["clone_dir"]), canon_dir)
        verdict = diff_report["verdict"]
        hits = diff_report.get("hits", [])
        combined_verdict = "flagged" if (verdict == "flagged" or report["verdict"] == "flagged") else "clean"
        if report["verdict"] == "flagged" and verdict == "clean":
            diff_report = dict(diff_report, hits=report.get("hits", []), verdict="flagged")
        # (c) audit report recorded; notice returned (Hermes posts).
        audit_notice = (f"audit {audit['audit_id']}: verdict={combined_verdict} "
                        f"diff_scan={verdict} sandbox={diff_report.get('sandbox', '?')} "
                        f"diff_files={len(diff_report.get('diff_files', []))} "
                        f"hits={len(hits)} report_hash={audit['report_hash'][:16]}")
        self._queue_notice(ticket["pool_id"], audit_notice, kind="audit_report")
        L.record_event(self._conn, ticket["pool_id"], "integrate_gate",
                       {"ticket_id": ticket_id, "verdict": combined_verdict,
                        "audit_id": audit["audit_id"],
                        "report_hash": audit["report_hash"],
                        "branch_head": branch["head"], "hits": hits})
        ctx = {
            "ticket_id": ticket_id, "pool_id": ticket["pool_id"],
            "repo_id": ticket["repo_id"], "version": pending["version"],
            "pending_manifest_hash": L.manifest_hash_value(pending) if hasattr(L, "manifest_hash_value") else pending.get("signature", "")[:16],
            "audit_id": audit["audit_id"], "report_hash": audit["report_hash"],
            "verdict": combined_verdict, "diff_verdict": verdict,
            "hits": hits, "diff_files": diff_report.get("diff_files", []),
            "branch_clone": branch["clone_dir"], "branch_ref": branch["head"],
            "branch_name": branch["branch"],
            "canonical_dir": str(canon_dir),
            "work_dir": str(work_dir),
        }
        self._integrations[ticket_id] = ctx
        self._integration_ctx_path(work_dir, ticket_id).write_text(json.dumps(ctx, indent=2))
        if combined_verdict != "clean":
            # Malware / flagged: REJECTED BEFORE HUMAN REVIEW. No buttons,
            # ticket blocked, canonical untouched.
            if ticket["state"] != "blocked":
                self._set_state_and_notify(ticket, "blocked")
            rejected_notice = (f"{STATE_MARKER.format(state='blocked')} integrate rejected before "
                               f"human review: scan verdict=flagged hits={json.dumps(hits)[:1200]}; "
                               f"no approve buttons posted; canonical unchanged")
            self._queue_notice(ticket["pool_id"], rejected_notice, kind="integrate_rejected")
            return {"ok": True, "ticket_id": ticket_id, "verdict": combined_verdict,
                    "report_hash": audit["report_hash"], "audit_id": audit["audit_id"],
                    "ticket_state": "blocked", "buttons_posted": False,
                    "hits": hits, "diff_files": ctx["diff_files"],
                    "notice": rejected_notice}
        if ticket["state"] != "review":
            self._set_state_and_notify(ticket, "review")
        buttons = [
            {"id": "approve", "label": "Approve & merge",
             "value": f"approve:{ticket_id}"},
            {"id": "edit", "label": "Edit & merge",
             "value": f"edit:{ticket_id}"},
            {"id": "discard", "label": "Discard",
             "value": f"discard:{ticket_id}"},
        ]
        decision_notice = (f"{STATE_MARKER.format(state='review')} awaiting maintainer decision "
                           f"(approve/edit/discard buttons) ticket {ticket_id} "
                           f"repo {ticket['repo_id']} v{pending['version']}: "
                           f"scan verdict=clean report_hash={audit['report_hash'][:16]}")
        self._queue_notice(ticket["pool_id"], decision_notice, kind="decision_prompt")
        return {"ok": True, "ticket_id": ticket_id, "verdict": combined_verdict,
                "report_hash": audit["report_hash"], "audit_id": audit["audit_id"],
                "ticket_state": "review", "buttons_posted": True,
                "buttons": buttons, "button_prompt": decision_notice,
                "branch_ref": branch["head"],
                "diff_files": ctx["diff_files"], "notice": decision_notice}

    def resolve_integration(self, ticket_id, decision, *, source=None,
                            work_dir=None, edit_files=None, edit_patch=None,
                            reason="", key_id=None) -> dict:
        """Human decision handler for the approve/edit/discard buttons.

        approve: merge branch -> canonical bundle -> NEW accepted manifest
        (maintainer-signed, DEK rotated, current members only) -> re-pin ->
        ticket ``merged``. edit: apply the maintainer's ``edit_files`` /
        ``edit_patch`` to the merged tree, re-scan, then the same publish.
        discard: ticket ``blocked`` with the reason; nothing merged.
        Merge conflict on approve/edit: merge aborted, canonical untouched,
        ticket bounced to ``blocked`` with ``rebase needed`` + both refs.
        """
        ticket = self.show_ticket(ticket_id)
        # Freeze is absolute: the exact message for every user.
        self._check_not_frozen(ticket["pool_id"])
        self._require_owner(ticket["pool_id"], source)
        # Member map key for maintainer (crypto only): the pool's
        # maintainer key from the ledger. ``key_id`` from a button click is
        # attribution only (OD1) and is never an authorization decision.
        key = self._caller_key_id
        pool = self._conn.execute("SELECT maintainer_key_id FROM pools WHERE pool_id=?",
                                  (ticket["pool_id"],)).fetchone()
        if pool is None:
            raise ValueError(f"unknown pool: {ticket['pool_id']!r}")
        key = pool[0]
        decision = str(decision).strip().lower()
        if decision not in ("approve", "edit", "discard"):
            raise ValueError(f"decision must be approve|edit|discard, got {decision!r}")
        ctx_work = Path(work_dir) if work_dir else self._default_work_dir()
        ctx = self._load_integration_ctx(ticket_id, ctx_work)
        if ctx is None:
            raise ValueError(f"no integration gate run for ticket {ticket_id!r}; run /integrate first")
        if ctx.get("verdict") != "clean":
            raise ValueError("scan verdict is not clean: buttons for this ticket are not actionable")
        if decision == "edit" and not edit_files and not edit_patch:
            raise ValueError("edit decision needs edit_files and/or edit_patch")
        if decision == "discard":
            if ticket["state"] not in ("review", "blocked"):
                raise ValueError(f"discard needs a review ticket, got {ticket['state']!r}")
            prop = self._propagator(ticket)
            maintainer = prop._members[key]
            pending = L.get_pending_manifest(self._conn, ticket["repo_id"], ctx["version"])
            if pending is not None:
                prop.reject(manifest=pending, reason=reason or "discarded by maintainer at integration gate",
                            maintainer=maintainer)
            if ticket["state"] != "blocked":
                self._set_state_and_notify(ticket, "blocked")
            discard_notice = (f"{STATE_MARKER.format(state='blocked')} discarded ticket {ticket_id}: "
                              f"{reason or 'discarded by maintainer'}; canonical unchanged")
            self._queue_notice(ticket["pool_id"], discard_notice, kind="integrate_discard")
            L.record_event(self._conn, ticket["pool_id"], "integrate_discarded",
                           {"ticket_id": ticket_id, "reason": reason})
            return {"ok": True, "ticket_id": ticket_id, "decision": "discard",
                    "ticket_state": "blocked", "merged": False, "notice": discard_notice}
        if ticket["state"] != "review":
            raise ValueError(f"decision needs a ticket in review, got {ticket['state']!r}")
        prop = self._propagator(ticket)
        maintainer = prop._members[key]
        canonical_dir = Path(ctx["canonical_dir"])
        branch_clone = Path(ctx["branch_clone"])
        merge = merge_branch(canonical_dir, branch_clone)
        if merge.get("conflict"):
            if ticket["state"] != "blocked":
                self._set_state_and_notify(ticket, "blocked")
            rebase_notice = (f"{STATE_MARKER.format(state='blocked')} rebase needed ticket {ticket_id}: "
                             f"merge conflict; canonical_ref={merge['canonical_ref']} "
                             f"branch_ref={merge['branch_ref']} branch={merge['branch_name']}; "
                             f"canonical unchanged — contributor must rebase and resubmit")
            self._queue_notice(ticket["pool_id"], rebase_notice, kind="rebase_needed")
            L.record_event(self._conn, ticket["pool_id"], "integrate_conflict",
                           {"ticket_id": ticket_id, **merge})
            return {"ok": True, "ticket_id": ticket_id, "decision": decision,
                    "ticket_state": "blocked", "merged": False, "conflict": True,
                    "canonical_ref": merge["canonical_ref"],
                    "branch_ref": merge["branch_ref"], "branch_name": merge["branch_name"],
                    "notice": rebase_notice}
        edit_info = None
        if decision == "edit":
            pre_edit_head = merge["merge_commit"]
            edit_info = apply_edit(canonical_dir, edit_files=edit_files,
                                   edit_patch=edit_patch)
            touched = [p for p in edit_info["touched"] if p != "edit_patch"]
            rescan = rescan_tree(canonical_dir, touched or edit_info["touched"])
            if rescan["verdict"] != "clean":
                import subprocess as _sp
                _sp.run(["git", "-C", str(canonical_dir), "reset", "--hard", pre_edit_head],
                        capture_output=True, text=True, timeout=60)
                if ticket["state"] != "blocked":
                    self._set_state_and_notify(ticket, "blocked")
                edit_rejected = (f"{STATE_MARKER.format(state='blocked')} maintainer edit rejected: "
                                 f"re-scan flagged hits={json.dumps(rescan.get('hits', []))[:800]}; "
                                 f"edit rolled back")
                self._queue_notice(ticket["pool_id"], edit_rejected, kind="integrate_rejected")
                return {"ok": True, "ticket_id": ticket_id, "decision": "edit",
                        "ticket_state": "blocked", "merged": False,
                        "reason": "edit re-scan flagged", "hits": rescan.get("hits", []),
                        "notice": edit_rejected}
        prev = L.get_latest_accepted_manifest(self._conn, ticket["repo_id"])
        from crypto import manifest_hash as _mh
        prev_hash = _mh(prev) if prev else None
        published = prop.publish_merged(
            canonical_dir=canonical_dir, repo_id=ticket["repo_id"],
            version=ctx["version"], maintainer=maintainer,
            maintainer_signing_key=self._caller_sk,
            prev_manifest_hash=prev_hash, audit_report_hash=ctx["report_hash"])
        if ticket["state"] != "merged":
            self._set_state_and_notify(ticket, "merged")
        merged_notice = (f"{STATE_MARKER.format(state='merged')} merged ticket {ticket_id}: "
                         f"version {ctx['version']} manifest {published['manifest_hash'][:16]} "
                         f"merge_commit={(edit_info or merge).get('edit_commit') or merge.get('merge_commit')}")
        self._queue_notice(ticket["pool_id"], merged_notice, kind="integrate_merged")
        L.record_event(self._conn, ticket["pool_id"], "integrate_merged",
                       {"ticket_id": ticket_id, "version": ctx["version"],
                        "manifest_hash": published["manifest_hash"],
                        "merge_commit": (edit_info or {}).get("edit_commit") or merge.get("merge_commit"),
                        "decision": decision})
        return {"ok": True, "ticket_id": ticket_id, "decision": decision,
                "ticket_state": "merged", "merged": True,
                "manifest_hash": published["manifest_hash"],
                "version": ctx["version"],
                "merge_commit": (edit_info or {}).get("edit_commit") or merge.get("merge_commit"),
                "edit": edit_info, "notice": merged_notice}

    def audit_show(self, ticket_id=None, repo_id=None, version=None) -> dict:
        if ticket_id:
            ticket = self.show_ticket(ticket_id)
            repo_id, version = ticket["repo_id"], None
        if not repo_id:
            raise ValueError("audit show needs a ticket or repo")
        audit = L.get_audit(self._conn, repo_id, version)
        if audit is None:
            raise ValueError(f"no audit recorded for repo {repo_id!r}")
        report = json.loads(audit["report_json"] or "{}")
        return {"ok": True, "audit_id": audit["audit_id"], "repo_id": audit["repo_id"],
                "version": audit["version"], "verdict": audit["verdict"],
                "report_hash": audit["report_hash"], "report": report}
