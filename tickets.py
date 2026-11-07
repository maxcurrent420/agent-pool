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
    from .propagation import Member, RepoPropagator
    from .transport import TransportBackend
except ImportError:  # flat import (pytest from repo root)
    import crypto as C
    import ledger as L
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
        """The hosted pool is THE single pool (one pool per gateway).

        No pool named -> the one hosted pool. An explicit --pool naming
        anything but the hosted pool is refused (pool_not_local) naming
        the hosted pool. A pre-existing multi-pool DB is refused until
        the operator removes the extras (never auto-deleted).
        """
        hosts = L.list_local_pools(self._conn)
        if pool_id:
            pool_id = str(pool_id).strip()
            if pool_id:
                if pool_id not in hosts:
                    raise PoolNotLocalError(
                        f"pool {pool_id} is not hosted by this gateway (hosts: {', '.join(hosts) or 'none'})",
                        pool_id=pool_id)
                if len(hosts) != 1:
                    raise PoolNotLocalError(
                        f"multiple hosted pools ({', '.join(hosts)}) — one pool per gateway; "
                        f"the operator must remove the extras",
                        pool_id=pool_id)
                return pool_id
        if len(hosts) == 1:
            return hosts[0]
        if not hosts:
            raise PoolNotLocalError(
                "no pool hosted by this gateway",
                pool_id=pool_id or "")
        raise PoolNotLocalError(
            f"multiple hosted pools ({', '.join(hosts)}) — one pool per gateway; "
            f"the operator must remove the extras",
            pool_id=pool_id or "")

    def record_gateway(self, pool_id, gateway_endpoint="", transport_addr="", role="") -> dict:
        """Record a pool/gateway this agent interacts with (differentiation)."""
        return L.record_gateway(self._conn, pool_id,
                                gateway_endpoint=gateway_endpoint,
                                transport_addr=transport_addr, role=role)

    def list_gateways(self) -> list:
        """Pools/gateways this agent interacts with (not the hosted pool)."""
        return L.list_gateways(self._conn)

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

    def fetch_repo_list(self, pool_id=None, *, source=None) -> dict:
        """Hosted-pool repo list: repos + latest accepted version + available
        (unclaimed) tasks + gateway addressing (the /fetch repo list verb).

        Runs on the pool's own gateway (the ledger lives there; the
        platform routes the command to the addressed pool's gateway).
        A foreign pool id is refused (pool_not_local) but recorded in
        ``pool_gateways`` so the agent can differentiate the pools it
        interacts with (interaction, not management).
        """
        if pool_id and str(pool_id).strip() not in L.list_local_pools(self._conn):
            self.record_gateway(str(pool_id).strip(), role="interact")
            raise PoolNotLocalError(
                f"pool {str(pool_id).strip()} is not hosted by this gateway "
                f"(hosts: {', '.join(L.list_local_pools(self._conn)) or 'none'})",
                pool_id=str(pool_id).strip())
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        repos = []
        for r in L.list_repos(self._conn, resolved):
            available = [dict(t) for t in L.list_tickets(
                self._conn, pool_id=resolved, state="open", repo_id=r["repo_id"])]
            repos.append({
                "repo_id": r["repo_id"], "pool_id": r["pool_id"],
                "display_name": r["display_name"], "name": r["name"],
                "accepted_version": r["accepted_version"],
                "available_tasks": [
                    {"ticket_id": t["ticket_id"], "title": t["title"],
                     "claimed": False, "claimant_key_id": t.get("claimant_key_id")}
                    for t in available],
                "open_tickets": r["open_tickets"],
                "claimed_tickets": r["claimed_tickets"],
                "total_tickets": r["total_tickets"],
            })
        out = {"ok": True, "pool_id": resolved, "repos": repos,
               "gateway": self._gateway_addressing(resolved)}
        self._enforce_reply_guard(out)
        return out

    def fetch_task_list(self, pool_id=None, *, source=None, ticket=None) -> dict:
        """All non-completed tasks with claimed status (the /fetch task
        list verb). A claimed task is shown as claimed (and by which
        key), never as available. ``--ticket <id>`` returns one task's
        detail (completed tasks are returned on explicit request).
        """
        if pool_id and str(pool_id).strip() and str(pool_id).strip() in L.list_local_pools(self._conn):
            resolved = self._resolve_pool(pool_id, source=source)
        elif ticket:
            resolved = self.show_ticket(ticket)["pool_id"]
        else:
            resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        if ticket:
            t = self.show_ticket(ticket)
            if t["pool_id"] != resolved:
                raise PoolNotLocalError(
                    f"pool_not_local: ticket {ticket!r} belongs to pool "
                    f"{t['pool_id']!r}, not pool {resolved!r}",
                    pool_id=t["pool_id"])
            row = dict(t)
            row["claimed"] = t["state"] == "claimed"
            row["claimant_key_id"] = t.get("claimant_key_id")
            return {"ok": True, "pool_id": resolved, "ticket": row}
        rows = [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="open")]
        rows += [dict(t) for t in L.list_tickets(self._conn, pool_id=resolved, state="claimed")]
        tasks = [{"ticket_id": t["ticket_id"], "title": t["title"],
                  "repo_id": t["repo_id"], "state": t["state"],
                  "claimed": t["state"] == "claimed",
                  "claimant_key_id": t.get("claimant_key_id")}
                 for t in rows]
        return {"ok": True, "pool_id": resolved, "tasks": tasks}

    def fetch_repo(self, repo_id, *, pool_id=None, source=None) -> dict:
        """The latest accepted signed manifest for one repo (the
        /fetch repo <repo_id> verb) — the artifact the contributor's
        plugin downloads with. Carried as ``manifest_json`` (canonical
        serialization the signature covers); OD3 4 KB guard applies.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        manifest = L.get_latest_accepted_manifest(self._conn, repo_id)
        if manifest is None:
            raise ValueError(f"no ACCEPTED version for repo {repo_id!r} to fetch")
        try:
            from crypto import manifest_hash as _manifest_hash
        except Exception:
            from .crypto import manifest_hash as _manifest_hash
        out = {"ok": True, "pool_id": resolved, "repo_id": repo_id,
               "manifest_json": json.dumps(manifest, sort_keys=True),
               "manifest_hash": _manifest_hash(manifest),
               "transfer_ticket": manifest.get("transfer_ticket", "")}
        self._enforce_reply_guard(out)
        return out

    def fetch_repo_download(self, manifest, *, dest, source=None) -> dict:
        """Download + decrypt + clone one repo from a fetched manifest
        using the caller's own box key (the /fetch repo --manifest
        form), over the configured transport.

        Unwraps the DEK with the caller's box private key,
        BLAKE3-verifies, clones. Manifest comes from /fetch repo on
        the pool gateway.
        """
        if not manifest:
            raise ValueError("fetch repo needs --manifest <json>")
        if not self._caller_box_sk:
            raise PermissionError("fetch repo needs the agent's own box key for DEK unwrap")
        repo_id = manifest.get("repo_id", "repo")
        try:
            pool_id = self._resolve_pool(None, source=source)
        except Exception:
            pool_id = None
        from propagation import Member as _M
        members = {self._caller_key_id: _M(key_id=self._caller_key_id, role="member",
                                           box_pubkey_hex="", box_sk=self._caller_box_sk)}
        signer = manifest.get("signer_key_id", "")
        if signer and signer not in members:
            members[signer] = _M(key_id=signer, role="maintainer",
                                 verify_key_hex="", box_pubkey_hex="", box_sk=None)
        try:
            prop = self._propagator(repo_id if pool_id is None else pool_id) if pool_id else None
        except Exception:
            prop = None
        if prop is not None:
            fetcher = prop._members.get(self._caller_key_id) or members[self._caller_key_id]
            return prop.fetch(manifest=manifest, fetcher=fetcher, dest=Path(dest))
        wrapped = manifest.get("wrapped_keys") or {}
        if self._caller_key_id not in wrapped:
            raise PermissionError(
                "fetch repo refused: caller key not in manifest wrapped_keys (only whitelisted members can decrypt)")
        from propagation import RepoPropagator as _RP
        prop = _RP(self._conn, self._transport, pool_id=pool_id or "fetch",
                   maintainer_key_id=signer, maintainer_signing_key=None, members=members)
        fetcher = members[self._caller_key_id]
        return prop.fetch(manifest=manifest, fetcher=fetcher, dest=Path(dest),
                          verify_chain=False)

    def _gateway_addressing(self, pool_id) -> dict:
        """Transport addressing for this gateway's hosted pool (endpoint
        id; under Tor, the hidden-service identity)."""
        try:
            prop = self._propagator(pool_id)
            info = prop._transport_info()
        except Exception:
            info = {"transport_mode": "", "relay_url": ""}
        row = self._conn.execute("SELECT relay_url FROM pools WHERE pool_id=?",
                                 (pool_id,)).fetchone()
        relay_url = (row[0] or "") if row is not None else ""
        return {"endpoint": pool_id,
                "transport_addr": "",
                "transport_mode": info.get("transport_mode", ""),
                "relay_url": info.get("relay_url") or relay_url,
                "transport_note": info.get("transport_note", "")}

    def _enforce_reply_guard(self, out: dict) -> None:
        """OD3: fail loudly rather than silently truncate a manifest."""
        try:
            from replies import MAX_CONTENT_BYTES
        except Exception:
            MAX_CONTENT_BYTES = 4096
        size = len(json.dumps(out, indent=2, sort_keys=True).encode("utf-8"))
        if size > MAX_CONTENT_BYTES:
            raise ValueError(
                f"fetch reply {size} bytes exceeds the {MAX_CONTENT_BYTES}-byte "
                f"gateway limit (manifest {out.get('manifest_hash','?')});"
                f" hash-addressed retrieval is deferred")
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
        # Register authorizes the owner only. Key attribution is NOT bound
        # here: it binds on the user's first claim (pool_memberships), and
        # the whitelist itself is synced from env/config by _service_for.
        return {"ok": True, "pool_id": pool_id, "name": name, "maintainer": maintainer,
                "owner_discord_user_id": str(discord_user_id)}

    def members(self, pool_id, source=None) -> list:
        pool_id = self._resolve_pool(pool_id, source=source)
        # Authorized-only read verb (whitelist model).
        self._require_pool_authorized(pool_id, source)
        return L.list_members(self._conn, pool_id)

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
        # Drop the whitelist row if the bound Discord id is known.
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
        truthful. While set, all pool-mutating, claim, and push actions
        raise (see ``_check_not_frozen``); only this command (to
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
        state = "open"
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
        return L.list_tickets(self._conn, pool_id=pool_id, state=state, repo_id=repo_id)

    def _require_pool_authorized(self, pool_id, source) -> None:
        """Authorized-only read verb (2109): `/task list|show`,
        `/pool list|members`, `/audit show` are not public."""
        if not pool_id:
            return
        self._require_authorized(pool_id, source)

    def _is_bound_maintainer(self, pool_id, source=None) -> bool:
        # Whitelist model: owner check by Discord id, not gateway key.
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

    def _whitelist_bound_key(self, pool_id=None, source=None) -> str | None:
        """Whitelist-bound agent key for the driving Discord user (Phase 1).

        Reads ``pool_memberships`` (first-claim binding). Attribution only —
        crypto records WHO acted (overlap checks, manifests, notices). The
        Discord id is the authorization; this lookup never decides access.
        Returns the bound key_id, or None when unbound (callers fall back
        to the gateway identity).
        """
        user_id = self._discord_user(source)
        if user_id is None:
            return None
        try:
            return L.lookup_membership_key(self._conn, user_id, pool_id=pool_id)
        except Exception:
            return None

    def _require_owner(self, pool_id, source) -> None:
        # No legacy skip: absence of an owner row = refusal.
        user_id = self._discord_user(source)
        if user_id is None or not L.is_owner(self._conn, pool_id, user_id):
            raise PermissionError(f"not authorized for pool {pool_id} — ask the maintainer")

    def _require_authorized(self, pool_id, source) -> None:
        # No legacy skip: absence of whitelist rows = refusal.
        user_id = self._discord_user(source)
        if user_id is None or not L.is_authorized(self._conn, pool_id, user_id):
            raise PermissionError(f"not authorized for pool {pool_id} — ask the maintainer")


    # -- project / repo registry ----------------------------------------------

    def add_repo(self, pool_id, repo_id, path, display_name="", source=None) -> dict:
        """Register a project and submit its bytes as the next repo version.

        Registry row (upsert) + propagation submit (pack -> BLAKE3 -> DEK
        wrap -> Iroh send -> signed manifest appended to the chain).
        Re-running submits a NEW version (next_repo_version), never refuses.
        ``path`` is required: a project without bytes is not a project.
        The old ``--link`` pointer form is deleted (OD5).
        """
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
        # DEK timing: members whitelisted AFTER this version have no wrapped
        # key for it and must wait for the next version.
        members_with_box = [m for m in prop._members.values() if m.box_pubkey_hex]
        wrapped = set((result["manifest"].get("wrapped_keys") or {}).keys())
        missing = [m.key_id for m in members_with_box if m.key_id not in wrapped]
        notice = (f"project {repo_id} registered: v{version} "
                  f"{result['state']} ({result['manifest_hash'][:12]})")
        if missing:
            notice += (f"; WARNING: {len(missing)} member(s) have no DEK key "
                       f"for this version ({', '.join(sorted(missing))}) — whitelist "
                       "members BEFORE /add-repo; late joiners wait for the "
                       "next version")
        else:
            notice += ("; whitelist members BEFORE /add-repo — members whitelisted "
                       "later wait for the next version")
        return {"ok": True, "repo_id": repo_id, "pool_id": pool_id,
                "version": version, "state": result["state"],
                "manifest_hash": result["manifest_hash"],
                "transfer_id": result.get("transfer_id", ""),
                "notice": notice}


    def list_projects(self, pool_id=None, source=None) -> list:
        """Hosted-pool-only project listing for /pool list.

        Authorized-only read verb (whitelist model). One pool per gateway:
        when no pool is named, the single hosted pool is resolved via
        _resolve_pool; a PoolNotLocalError (no/foreign/multiple hosted
        pools) is re-raised as pool_not_local — never aggregated across
        hosted pools.
        """
        if pool_id:
            resolved = self._resolve_pool(pool_id, source=source)
            self._require_pool_authorized(resolved, source)
            return self._project_rows(resolved)
        bound = self._resolve_pool(None, source=source)
        self._require_pool_authorized(bound, source)
        return self._project_rows(bound)

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
        # Whitelist: only authorized users may claim (the Discord id is the
        # authorization; the bound key lookup is attribution, not authz).
        self._require_authorized(ticket["pool_id"], source)
        # First-claim key binding (Phase 1): the presented key is
        # explicit key_id, else the gateway identity. If this user already
        # has a bound key, the presented key MUST match it — the binding is
        # permanent; a different key is refused with the exact message
        # below. If unbound, the presented key binds on a successful claim.
        user_id = self._discord_user(source)
        bound = self._whitelist_bound_key(ticket["pool_id"], source)
        presented = key_id or self._caller_key_id
        if bound and presented and presented != bound:
            raise PermissionError(
                f"claim refused: user {user_id} already bound to key {bound} "
                f"on pool {ticket['pool_id']} — presented key {presented} differs; "
                "first-claim binding is permanent")
        key = bound or presented
        if not key:
            raise ValueError("claim needs a claimant key_id")
        claim = L.claim_ticket_enforced(
            self._conn, f"c-{secrets.token_hex(4)}", ticket_id, key,
            declared_paths=declared_paths, budget=budget,
            maintainer_override=maintainer_override)
        if not bound and user_id and key:
            L.bind_membership(self._conn, ticket["pool_id"], user_id, key)
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
        key = key_id or self._whitelist_bound_key(ticket["pool_id"], source) or self._caller_key_id
        updated = L.release_ticket(self._conn, ticket_id, key)
        notice = f"{STATE_MARKER.format(state='open')} ticket {ticket_id} released"
        self._queue_notice(updated["pool_id"], notice, kind="ticket_release")
        updated = dict(updated)
        updated["notice"] = notice
        return updated

    def complete(self, ticket_id, key_id=None, *, source=None) -> dict:
        """Complete a claimed ticket (terminal ``completed`` state)."""
        ticket = self.show_ticket(ticket_id)
        self._check_not_frozen(ticket["pool_id"])
        self._require_authorized(ticket["pool_id"], source)
        key = key_id or self._whitelist_bound_key(ticket["pool_id"], source) or self._caller_key_id
        updated = L.complete_ticket(self._conn, ticket_id, key)
        notice = f"{STATE_MARKER.format(state='completed')} ticket {ticket_id} completed"
        self._queue_notice(updated["pool_id"], notice, kind="ticket_completed")
        updated = dict(updated)
        updated["notice"] = notice
        return updated

    # -- Phase 4: push write path (grant -> push -> deliver -> complete) -----

    def push_repo(self, repo_id, ticket_id, *, pool_id=None, source=None) -> dict:
        """Issue a one-time push grant for a claimed task (the ``/push
        repo <repo_id> --ticket <id>`` verb, on the POOL's gateway).

        Claimant-only: the ticket must be ``claimed`` by the driving
        user's whitelist-bound key. The grant token plus the gateway
        addressing is the push link; the contributor's agent pushes the
        diff over the configured transport and delivers the transfer
        ticket + grant back (see :meth:`push_deliver`).
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        ticket = self.show_ticket(ticket_id)
        if ticket["pool_id"] != resolved:
            raise PoolNotLocalError(
                f"pool_not_local: ticket {ticket_id!r} belongs to pool "
                f"{ticket['pool_id']!r}, not pool {resolved!r}",
                pool_id=ticket["pool_id"])
        if repo_id and ticket["repo_id"] != repo_id:
            raise ValueError(
                f"push refused: ticket {ticket_id!r} is on repo "
                f"{ticket['repo_id']!r}, not {repo_id!r}")
        key = self._whitelist_bound_key(resolved, source) or self._caller_key_id
        if ticket["state"] != "claimed" or ticket.get("claimant_key_id") != key:
            raise PermissionError(
                f"push refused: ticket {ticket_id!r} is {ticket['state']!r} "
                f"(claimant {ticket.get('claimant_key_id')!r}); only the "
                f"claimant may push")
        token = secrets.token_hex(16)
        grant = L.create_push_grant(self._conn, token, resolved, ticket_id, key)
        L.record_event(self._conn, resolved, "push_granted",
                       {"ticket_id": ticket_id, "repo_id": ticket["repo_id"],
                        "key_id": key})
        gateway = self._gateway_addressing(resolved)
        notice = (f"push link issued for ticket {ticket_id} "
                  f"(repo {ticket['repo_id']}): token {token[:12]}…")
        self._queue_notice(resolved, notice, kind="push_granted")
        return {"ok": True, "pool_id": resolved, "repo_id": ticket["repo_id"],
                "ticket_id": ticket_id, "token": token,
                "push_link": {"token": token, "gateway": gateway,
                              "repo_id": ticket["repo_id"],
                              "ticket_id": ticket_id},
                "gateway": gateway, "grant": grant, "notice": notice}

    def push_build_bundle(self, repo_path, repo_id, *, version=None,
                          pool_id=None, source=None) -> dict:
        """Build a contributor push bundle on the contributor's own
        gateway (the push step): pack -> hash -> DEK wrap -> encrypt ->
        transport send -> contributor-signed manifest, with no ledger
        write. Thin wrapper over the propagator so the contributor's
        plugin drives it through the service; *version* defaults to the
        next repo version."""
        resolved = self._resolve_pool(pool_id, source=source)
        prop = self._propagator(resolved)
        pusher = prop._members.get(self._caller_key_id)
        if pusher is None:
            raise PermissionError(
                f"push refused: caller {self._caller_key_id!r} is not a pool member")
        if version is None:
            version = L.next_repo_version(self._conn, repo_id)
        return prop.build_push_bundle(
            repo_path=Path(repo_path), repo_id=repo_id, version=int(version),
            pusher=pusher, pusher_signing_key=self._caller_sk)

    def push_deliver(self, transfer_ticket, token, manifest, *,
                     pool_id=None, source=None) -> dict:
        """Deliver a pushed bundle to the pool gateway (the ``/push repo
        --deliver <ticket> --token <grant>`` verb).

        The grant is verified one-time (consumed on success); the ticket
        must still be claimed by the grant's key (released/completed ⇒
        refused). The contributor signature, membership and claimant are
        checked, the bytes are fetched over the configured transport,
        decrypted with the maintainer's wrapped DEK, BLAKE3-verified and
        sandbox-audited. Clean ⇒ :meth:`accept` appends the
        maintainer-signed accepted version and the task completes;
        flagged ⇒ the push is refused, the task stays claimed, and a
        ``push_flagged`` event records the scan hits. This synchronous
        audit is the only gate — there is no queue.
        """
        resolved = self._resolve_pool(pool_id, source=source)
        self._check_not_frozen(resolved)
        self._require_authorized(resolved, source)
        grant = L.get_push_grant(self._conn, token)
        if grant is None or grant.get("consumed_at") is not None:
            raise PermissionError("push deliver refused: unknown or replayed grant token")
        if grant["pool_id"] != resolved:
            raise PermissionError(
                "push deliver refused: grant is for a different pool")
        ticket = self.show_ticket(grant["ticket_id"])
        if ticket["state"] != "claimed" or ticket.get("claimant_key_id") != grant["key_id"]:
            raise PermissionError(
                f"push deliver refused: ticket {ticket['ticket_id']!r} is "
                f"{ticket['state']!r} (grant expired — released or completed)")
        if not manifest:
            raise ValueError("push deliver needs the contributor-signed manifest (--manifest <json>)")
        if manifest.get("transfer_ticket") != transfer_ticket:
            raise PermissionError(
                "push deliver refused: manifest transfer_ticket does not match the delivered ticket")
        if manifest.get("repo_id") != ticket["repo_id"]:
            raise PermissionError(
                "push deliver refused: manifest repo does not match the claimed ticket")
        if manifest.get("submitter_key_id") != grant["key_id"]:
            raise PermissionError(
                "push deliver refused: manifest submitter is not the grant claimant")
        prop = self._propagator(resolved)
        submitter = prop._members.get(manifest.get("submitter_key_id", ""))
        if submitter is None:
            raise PermissionError(
                "push deliver refused: submitter is not a pool member")
        from propagation import _verify_key
        if not C.verify_manifest(_verify_key(submitter), manifest):
            raise RuntimeError("push manifest signature does not verify")
        maintainer = prop._members.get(prop._maintainer_key_id)
        if maintainer is None:
            raise RuntimeError(
                f"maintainer {prop._maintainer_key_id!r} is not a pool member")
        aud = prop.audit(manifest=manifest, maintainer=maintainer)
        report = aud["report"]
        if report.get("verdict") != "clean":
            hits = report.get("hits", [])
            L.record_event(self._conn, resolved, "push_flagged",
                           {"ticket_id": ticket["ticket_id"],
                            "repo_id": manifest["repo_id"],
                            "version": manifest.get("version"),
                            "report_hash": aud["report_hash"], "hits": hits})
            notice = (f"push refused for ticket {ticket['ticket_id']}: "
                      f"audit flagged {len(hits)} hit(s); task stays claimed")
            self._queue_notice(resolved, notice, kind="push_flagged")
            return {"ok": False, "error": notice, "ticket_id": ticket["ticket_id"],
                    "state": ticket["state"], "verdict": report.get("verdict"),
                    "report_hash": aud["report_hash"], "hits": hits,
                    "notice": notice}
        accepted = prop.accept(
            manifest=manifest, report_hash=aud["report_hash"],
            maintainer=maintainer,
            maintainer_signing_key=self._caller_sk)
        consumed = L.consume_push_grant(self._conn, token)
        if consumed is None:
            raise PermissionError(
                "push deliver refused: grant was consumed concurrently (replay)")
        updated = L.complete_ticket(self._conn, ticket["ticket_id"], grant["key_id"])
        notice = (f"{STATE_MARKER.format(state='completed')} ticket "
                  f"{updated['ticket_id']} completed by push of "
                  f"{manifest['repo_id']} v{accepted['manifest']['version']} "
                  f"({accepted['manifest_hash'][:12]})")
        self._queue_notice(resolved, notice, kind="ticket_completed")
        return {"ok": True, "pool_id": resolved, "ticket_id": updated["ticket_id"],
                "state": updated["state"], "repo_id": manifest["repo_id"],
                "version": accepted["manifest"]["version"],
                "manifest_hash": accepted["manifest_hash"],
                "manifest": accepted["manifest"],
                "transfer_id": accepted.get("transfer_id", ""),
                "dek_rotated": accepted.get("dek_rotated", False),
                "report_hash": aud["report_hash"],
                "audit_id": aud["audit_id"], "notice": notice}

    # -- pool stewardship ------------------------------------------

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
            raise RuntimeError("push / submit need a transport backend")
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

    # -- audit -------------------------------------------------------

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
