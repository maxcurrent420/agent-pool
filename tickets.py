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
import subprocess
from pathlib import Path

try:  # package import (Hermes loads plugins as a package)
    from . import ledger as L
    from .execution import (BudgetExhausted, BudgetTracker, LifecycleWorkerRunner,
                            LocalProcessRunner, SandboxPolicy, build_mandate_brief,
                            create_worktree, run_acceptance, run_worker_loop)
    from .gateway import GatewayAdapter
    from .integration import (apply_edit, merge_branch, rescan_tree, scan_diff)
    from .propagation import Member, RepoPropagator
    from .transport import TransportBackend
except ImportError:  # flat import (pytest from repo root)
    import ledger as L
    from execution import (BudgetExhausted, BudgetTracker, LifecycleWorkerRunner,
                           LocalProcessRunner, SandboxPolicy, build_mandate_brief,
                           create_worktree, run_acceptance, run_worker_loop)
    from gateway import GatewayAdapter
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
        self._workers: dict[str, dict] = {}  # ticket_id -> worker record
        self._integrations: dict[str, dict] = {}  # ticket_id -> gate context

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

    def remove_member(self, pool_id, key_id, *, actor_key_id=None) -> dict:
        """Revoke a member: registry removal + forward-separation notice.

        Future manifests omit the removed member's wrapped key (fresh DEK per
        version). Already-fetched versions are inherently unfetchable
        retroactively — the limitation is stated in the thread notice.
        """
        actor = actor_key_id or self._caller_key_id
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (pool_id,)).fetchone()
        if pool is None:
            raise ValueError(f"unknown pool: {pool_id!r}")
        if pool["maintainer_key_id"] != actor:
            raise PermissionError("only the pool maintainer may remove members")
        removed = L.remove_member(self._conn, pool_id, key_id)
        self._gateway.post_notice(
            pool_id,
            f"member removed: {key_id} (forward separation only: "
            f"{L.REVOCATION_LIMITATION})",
            kind="member_removed")
        return removed

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

    # -- Phase 4: agent execution --------------------------------------------

    def report_progress(self, ticket_id, text) -> dict:
        """Milestone progress to the ticket thread ONLY (4 KB guard, no bytes)."""
        ticket = self.show_ticket(ticket_id)
        short = f"ticket {ticket_id}: {text}"[:3500]
        self._gateway.post_notice(ticket["pool_id"], short, kind="progress")
        return {"ok": True, "ticket_id": ticket_id, "text": short}

    def _budget_exhausted(self, ticket, reason, usage) -> dict:
        """Structured waiting/blocked report + claim released (re-armable).

        The ticket goes claimed -> blocked with a machine-readable notice;
        the claim row is released so the ticket can be re-claimed later
        (blocked -> open is a legal transition).
        """
        report = (f"{STATE_MARKER.format(state='blocked')} [blocked] {reason} "
                  f"usage={json.dumps(usage or {}, sort_keys=True)} "
                  f"claim released; ticket re-armable")
        if ticket.get("thread_ref"):
            self._gateway.set_ticket_state(ticket["thread_ref"], "blocked")
        self._gateway.post_notice(ticket["pool_id"], report[:3500], kind="blocked_report")
        L.release_claim_for_ticket(self._conn, ticket["ticket_id"])
        if ticket["state"] == "claimed":
            L.set_ticket_state(self._conn, ticket["ticket_id"], "blocked")
        L.record_event(self._conn, ticket["pool_id"], "budget_exhausted",
                       {"ticket_id": ticket["ticket_id"], "reason": str(reason),
                        "usage": usage or {}})
        self._workers.pop(ticket["ticket_id"], None)
        return {"ok": False, "ticket_id": ticket["ticket_id"], "state": "blocked",
                "reason": str(reason), "usage": usage or {},
                "claim": "released"}

    def start_work(self, ticket_id, *, work_root, lifecycle=None, runner=None,
                   worker_command=None, acceptance_cmd=None) -> dict:
        """Fetch latest ACCEPTED version over Iroh, create the ticket worktree,
        and spawn the worker session seeded with mandate + budget + sandbox.

        ``runner`` overrides backend selection (tests). With no runner and no
        Hermes ``lifecycle``, a LocalProcessRunner over ``worker_command``
        is used (CLI / exit-demo path).
        """
        ticket = self.show_ticket(ticket_id)
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

    def integrate(self, ticket_id, key_id=None, *, work_dir=None) -> dict:
        """Phase 5 full gate: fetch branch -> dialog scan -> audit -> buttons.

        Steps (all real, network-off scan):
        a. fetch the contributor BRANCH bundle (``RepoPropagator.fetch_pending``)
           and the canonical baseline (latest accepted) over the transport;
        b. sandboxed scan of the branch DIFF (``integration.scan_diff``,
           bwrap ``--unshare-net``) — containment-by-construction;
        c. audit report posted to the ticket thread / pool channel;
        d. if clean: post approve/edit/discard buttons (``post_buttons``) and
           keep the ticket in ``review`` awaiting a human decision; if the
           scan is FLAGGED the verdict blocks the gate: no buttons are posted,
           the ticket goes to ``blocked`` and canonical is never touched.
        """
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
        # (c) audit report to the ticket thread / gateway.
        if ticket.get("thread_ref"):
            self._gateway.post_notice(
                ticket["pool_id"],
                f"audit {audit['audit_id']}: verdict={combined_verdict} "
                f"diff_scan={verdict} sandbox={diff_report.get('sandbox', '?')} "
                f"diff_files={len(diff_report.get('diff_files', []))} "
                f"hits={len(hits)} report_hash={audit['report_hash'][:16]}",
                kind="audit_report")
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
            if ticket.get("thread_ref"):
                self._gateway.post_notice(
                    ticket["pool_id"],
                    f"{STATE_MARKER.format(state='blocked')} integrate rejected before "
                    f"human review: scan verdict=flagged hits={json.dumps(hits)[:1200]}; "
                    f"no approve buttons posted; canonical unchanged",
                    kind="integrate_rejected")
            return {"ok": True, "ticket_id": ticket_id, "verdict": combined_verdict,
                    "report_hash": audit["report_hash"], "audit_id": audit["audit_id"],
                    "ticket_state": "blocked", "buttons_posted": False,
                    "hits": hits, "diff_files": ctx["diff_files"]}
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
        if ticket.get("thread_ref"):
            self._gateway.post_buttons(
                ticket["thread_ref"],
                f"Integration review ticket {ticket_id} repo {ticket['repo_id']} "
                f"v{pending['version']}: scan verdict=clean report_hash="
                f"{audit['report_hash'][:16]}. Approve merges the branch into "
                f"canonical with a rotated DEK; edit applies a maintainer patch "
                f"or file override first; discard rejects without merging.",
                buttons)
            self._gateway.post_notice(
                ticket["pool_id"],
                f"{STATE_MARKER.format(state='review')} awaiting maintainer decision "
                f"(approve/edit/discard buttons posted) ticket {ticket_id}",
                kind="decision_prompt")
        return {"ok": True, "ticket_id": ticket_id, "verdict": combined_verdict,
                "report_hash": audit["report_hash"], "audit_id": audit["audit_id"],
                "ticket_state": "review", "buttons_posted": True,
                "buttons": buttons, "branch_ref": branch["head"],
                "diff_files": ctx["diff_files"]}

    def resolve_integration(self, ticket_id, decision, *, key_id=None,
                            work_dir=None, edit_files=None, edit_patch=None,
                            reason="") -> dict:
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
        key = key_id or self._caller_key_id
        pool = self._conn.execute("SELECT * FROM pools WHERE pool_id=?",
                                  (ticket["pool_id"],)).fetchone()
        if pool["maintainer_key_id"] != key:
            raise PermissionError("integration decisions are maintainer-only")
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
            if ticket.get("thread_ref"):
                self._gateway.post_notice(
                    ticket["pool_id"],
                    f"{STATE_MARKER.format(state='blocked')} discarded ticket {ticket_id}: "
                    f"{reason or 'discarded by maintainer'}; canonical unchanged",
                    kind="integrate_discard")
            L.record_event(self._conn, ticket["pool_id"], "integrate_discarded",
                           {"ticket_id": ticket_id, "reason": reason})
            return {"ok": True, "ticket_id": ticket_id, "decision": "discard",
                    "ticket_state": "blocked", "merged": False}
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
            if ticket.get("thread_ref"):
                self._gateway.post_notice(
                    ticket["pool_id"],
                    f"{STATE_MARKER.format(state='blocked')} rebase needed ticket {ticket_id}: "
                    f"merge conflict; canonical_ref={merge['canonical_ref']} "
                    f"branch_ref={merge['branch_ref']} branch={merge['branch_name']}; "
                    f"canonical unchanged — contributor must rebase and resubmit",
                    kind="rebase_needed")
            L.record_event(self._conn, ticket["pool_id"], "integrate_conflict",
                           {"ticket_id": ticket_id, **merge})
            return {"ok": True, "ticket_id": ticket_id, "decision": decision,
                    "ticket_state": "blocked", "merged": False, "conflict": True,
                    "canonical_ref": merge["canonical_ref"],
                    "branch_ref": merge["branch_ref"], "branch_name": merge["branch_name"]}
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
                if ticket.get("thread_ref"):
                    self._gateway.post_notice(
                        ticket["pool_id"],
                        f"{STATE_MARKER.format(state='blocked')} maintainer edit rejected: "
                        f"re-scan flagged hits={json.dumps(rescan.get('hits', []))[:800]}; "
                        f"edit rolled back",
                        kind="integrate_rejected")
                return {"ok": True, "ticket_id": ticket_id, "decision": "edit",
                        "ticket_state": "blocked", "merged": False,
                        "reason": "edit re-scan flagged", "hits": rescan.get("hits", [])}
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
        if ticket.get("thread_ref"):
            self._gateway.post_notice(
                ticket["pool_id"],
                f"{STATE_MARKER.format(state='merged')} merged ticket {ticket_id}: "
                f"version {ctx['version']} manifest {published['manifest_hash'][:16]} "
                f"merge_commit={(edit_info or merge).get('edit_commit') or merge.get('merge_commit')}",
                kind="integrate_merged")
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
                "edit": edit_info}

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
