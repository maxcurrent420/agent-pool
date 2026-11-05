"""Ticket coordination service (Phase 3).

Thin service over the ledger + gateway adapter + Phase 2 propagator. Slash
commands in ``__init__`` delegate here so the same code paths run in tests
(injected fakes) and in Hermes (real ledger / real gateway).

Ticket STATE is represented in the gateway through ``set_ticket_state``
(thread title/state marker + notice line) because core Discord has no
``applied_tags`` support; no core file is edited.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

try:  # package import (Hermes loads plugins as a package)
    from . import ledger as L
    from .gateway import GatewayAdapter
    from .propagation import Member, RepoPropagator
    from .transport import TransportBackend
except ImportError:  # flat import (pytest from repo root)
    import ledger as L
    from gateway import GatewayAdapter
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


class PoolService:
    """Ticket coordination for one pool. Caller identity is injected."""

    def __init__(self, conn, gateway: GatewayAdapter, transport=None, *,
                 caller_key_id: str = "", caller_signing_key=None,
                 caller_box_sk=None, budget_defaults=None):
        self._conn = conn
        self._gateway = gateway
        self._transport = transport
        self._caller_key_id = caller_key_id
        self._caller_sk = caller_signing_key
        self._caller_box_sk = caller_box_sk
        self._budget_defaults = budget_defaults

    # -- pool membership ----------------------------------------------------

    def register_pool(self, pool_id, name, relay_url="", key_id=None) -> dict:
        maintainer = key_id or self._caller_key_id
        if not maintainer:
            raise ValueError("register needs a maintainer key_id")
        L.create_pool(self._conn, pool_id, name, maintainer, relay_url=relay_url)
        L.add_member(self._conn, pool_id, maintainer, role="maintainer")
        L.record_event(self._conn, pool_id, "pool_registered",
                       {"pool_id": pool_id, "maintainer": maintainer})
        return {"ok": True, "pool_id": pool_id, "name": name, "maintainer": maintainer}

    def invite(self, pool_id, key_id, role="member", box_pubkey_hex="") -> dict:
        if role not in ("member", "maintainer"):
            raise ValueError(f"role must be member|maintainer, got {role!r}")
        L.add_member(self._conn, pool_id, key_id, role=role, box_pubkey_hex=box_pubkey_hex)
        L.record_event(self._conn, pool_id, "invited",
                       {"key_id": key_id, "role": role})
        return {"ok": True, "pool_id": pool_id, "key_id": key_id, "role": role}

    def members(self, pool_id) -> list:
        return L.list_members(self._conn, pool_id)

    def freeze(self, pool_id, frozen: bool) -> dict:
        L.set_frozen(self._conn, pool_id, frozen)
        self._gateway.post_notice(pool_id, f"pool {pool_id} {'frozen' if frozen else 'unfrozen'}",
                                  kind="freeze")
        return {"ok": True, "pool_id": pool_id, "frozen": frozen}

    def is_frozen(self, pool_id) -> bool:
        return L.is_frozen(self._conn, pool_id)

    # -- tickets --------------------------------------------------------------

    def add_ticket(self, pool_id, repo_id, title, brief="", budget=None) -> dict:
        if not title.strip():
            raise ValueError("ticket title is required")
        if L.is_frozen(self._conn, pool_id):
            raise L.ClaimRefused(f"pool {pool_id!r} is frozen")
        L.ensure_repo(self._conn, repo_id, pool_id)
        full_budget = L.normalize_budget(budget, self._budget_defaults)
        ticket_id = f"t-{secrets.token_hex(4)}"
        L.create_ticket(self._conn, ticket_id, pool_id, repo_id, title.strip(),
                        brief=brief, budget=full_budget)
        ticket = L.get_ticket(self._conn, ticket_id)
        ref = self._gateway.open_ticket_thread(pool_id, _ticket_payload(ticket))
        self._conn.execute("UPDATE tickets SET thread_ref=? WHERE ticket_id=?",
                           (ref.ref, ticket_id))
        self._conn.commit()
        self._gateway.set_ticket_state(ref.ref, "open")
        L.record_event(self._conn, pool_id, "ticket_created",
                       {"ticket_id": ticket_id, "title": title, "thread_ref": ref.ref})
        return L.get_ticket(self._conn, ticket_id)

    def list_tickets(self, pool_id=None, state=None) -> list:
        return L.list_tickets(self._conn, pool_id=pool_id, state=state)

    def show_ticket(self, ticket_id) -> dict:
        return L.get_ticket_or_raise(self._conn, ticket_id)

    def _set_state_and_notify(self, ticket, state) -> dict:
        updated = L.set_ticket_state(self._conn, ticket["ticket_id"], state)
        if updated.get("thread_ref"):
            self._gateway.set_ticket_state(updated["thread_ref"], state)
            self._gateway.post_notice(
                updated["pool_id"],
                f"{STATE_MARKER.format(state=state)} ticket {updated['ticket_id']}: {updated['title']}",
                kind="ticket_state")
        return updated

    # -- claims ---------------------------------------------------------------

    def claim(self, ticket_id, key_id=None, declared_paths=None, budget=None,
              maintainer_override=False) -> dict:
        key = key_id or self._caller_key_id
        if not key:
            raise ValueError("claim needs a claimant key_id")
        claim = L.claim_ticket_enforced(
            self._conn, f"c-{secrets.token_hex(4)}", ticket_id, key,
            declared_paths=declared_paths, budget=budget,
            maintainer_override=maintainer_override)
        ticket = L.get_ticket(self._conn, ticket_id)
        if ticket.get("thread_ref"):
            self._gateway.set_ticket_state(ticket["thread_ref"], "claimed")
            self._gateway.post_notice(
                ticket["pool_id"],
                f"{STATE_MARKER.format(state='claimed')} ticket {ticket_id} claimed by {key}",
                kind="ticket_claim")
        return claim

    def release(self, ticket_id, key_id=None) -> dict:
        key = key_id or self._caller_key_id
        updated = L.release_ticket(self._conn, ticket_id, key)
        if updated.get("thread_ref"):
            self._gateway.set_ticket_state(updated["thread_ref"], "open")
            self._gateway.post_notice(
                updated["pool_id"],
                f"{STATE_MARKER.format(state='open')} ticket {ticket_id} released",
                kind="ticket_release")
        return updated

    # -- work submit / integrate (real Phase 2 flow) --------------------------

    def _members_for_propagator(self, pool_id) -> dict:
        """Member map from the members table + identities; private box key only
        for the local caller (never persisted here)."""
        members = {}
        for row in self._conn.execute(
                "SELECT m.key_id, m.role, i.verify_key_hex, m.box_pubkey_hex"
                " FROM members m LEFT JOIN identities i ON i.key_id = m.key_id"
                " WHERE m.pool_id=?", (pool_id,)):
            box_sk = self._caller_box_sk if row[0] == self._caller_key_id else None
            members[row[0]] = Member(key_id=row[0], role=row[1] or "member",
                                     verify_key_hex=row[2] or "",
                                     box_pubkey_hex=row[3] or "", box_sk=box_sk)
        if self._caller_key_id and self._caller_key_id not in members:
            members[self._caller_key_id] = Member(
                key_id=self._caller_key_id, role="member",
                box_pubkey_hex="", box_sk=self._caller_box_sk)
        return members

    def _propagator(self, ticket) -> RepoPropagator:
        if self._transport is None:
            raise RuntimeError("work submit / integrate need a transport backend")
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (ticket["pool_id"],)).fetchone()
        maintainer_id = pool["maintainer_key_id"]
        members = self._members_for_propagator(ticket["pool_id"])
        if maintainer_id not in members:
            raise RuntimeError(f"maintainer {maintainer_id!r} is not a pool member")
        maintainer_sk = self._caller_sk
        # RepoPropagator only uses maintainer_signing_key in accept() (Phase 5);
        # submit/audit run as the caller. The caller's key is a safe stand-in
        # until the maintainer gate lands.
        return RepoPropagator(
            self._conn, self._gateway, self._transport, pool_id=ticket["pool_id"],
            maintainer_key_id=maintainer_id, maintainer_signing_key=maintainer_sk,
            members=members)

    def work_submit(self, ticket_id, repo_path, version, key_id=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        key = key_id or self._caller_key_id
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
                "manifest_hash": result["manifest_hash"], "state": result["state"]}

    def integrate(self, ticket_id, key_id=None) -> dict:
        ticket = self.show_ticket(ticket_id)
        key = key_id or self._caller_key_id
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (ticket["pool_id"],)).fetchone()
        if pool["maintainer_key_id"] != key:
            raise PermissionError("integrate is maintainer-only")
        pending = L.get_pending_manifest(self._conn, ticket["repo_id"])
        if pending is None:
            raise ValueError(f"no pending submission for repo {ticket['repo_id']!r}")
        prop = self._propagator(ticket)
        maintainer = prop._members[key]
        audit = prop.audit(manifest=pending, maintainer=maintainer)
        report = audit["report"]
        if ticket.get("thread_ref"):
            self._gateway.post_notice(
                ticket["pool_id"],
                f"audit {audit['audit_id']}: verdict={report['verdict']} "
                f"sandbox={report.get('sandbox', '?')} hits={len(report.get('hits', []))} "
                f"report_hash={audit['report_hash'][:16]}",
                kind="audit_report")
        target = "review" if report["verdict"] == "clean" else "blocked"
        if ticket["state"] != target:
            self._set_state_and_notify(ticket, target)
        return {"ok": True, "ticket_id": ticket_id, "verdict": report["verdict"],
                "report_hash": audit["report_hash"], "audit_id": audit["audit_id"],
                "ticket_state": target}

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
