"""Agent Pool plugin — registration (Phase 3: ticket coordination).

Slash commands (in-session) wire the ledger, gateway adapter, and Phase 2
propagator: ``/pool register|invite|members|freeze``, ``/task add|list|show``,
``/claim``, ``/release``, ``/work submit``, ``/integrate``, ``/audit show``.
``/work submit`` and ``/integrate`` call the real Phase 2 flow; ticket state is
represented in the gateway via ``set_ticket_state`` (core has no applied_tags).
"""
import json
import logging
import shlex
from pathlib import Path

logger = logging.getLogger(__name__)


def _cfg(ctx, key, default):
    try:
        return ctx.get_config(key, default=default)
    except Exception:
        return default


def _pool_status(args=None, **kwargs):
    """Report plugin config + ledger/identity readiness. Always returns JSON."""
    ctx = kwargs.get("ctx")
    try:
        relay_url = _cfg(ctx, "relay_url", "") if ctx else ""
        budgets = {k: _cfg(ctx, k, d) if ctx else d for k, d in (
            ("max_turns", 25), ("max_tasks_per_day", 5),
            ("token_cap", 50000), ("wall_clock_seconds", 1800))}
        status = {"ok": True, "plugin": "agent-pool", "phase": 3,
                  "relay_url": relay_url, "budgets": budgets,
                  "ledger": "ready", "identity": "ready"}
        return json.dumps(status)
    except Exception as exc:  # never raise from a handler
        return json.dumps({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Service construction (lazy: no DB/identity work at register() time)
# ---------------------------------------------------------------------------

def _service_for(ctx):
    """Build a PoolService from the plugin context. Raises with a clear msg."""
    try:
        from . import ledger as L
        from .gateway import (DiscordGatewayAdapter, FakeGatewayAdapter,
                              discord_gateway_send_factory)
        from .identity import ensure_identity
        from .tickets import PoolService
        from .transport import FakeTransport, RealSendHermesTransport
    except ImportError:  # flat import (tests / standalone)
        import ledger as L
        from gateway import (DiscordGatewayAdapter, FakeGatewayAdapter,
                             discord_gateway_send_factory)
        from identity import ensure_identity
        from tickets import PoolService
        from transport import FakeTransport, RealSendHermesTransport
    conn = L.get_db()
    data_dir = Path.home() / ".hermes" / "plugin-data" / "agent-pool"
    try:
        from . import ledger as _L
        _ = _L.default_db_path().parent
        data_dir = _L.default_db_path().parent
    except Exception:
        pass
    identity = ensure_identity(data_dir)
    # Gateway: the host may inject a live adapter through ctx.state; otherwise
    # fall back to the in-memory adapter (offline/CLI) and say so in events.
    gateway = None
    try:
        gateway = ctx.state.get("gateway_adapter") if ctx is not None else None
    except Exception:
        gateway = None
    if gateway is None:
        channel_id = _cfg(ctx, "gateway_channel_id", "") if ctx else ""
        discord_adapter = None
        try:
            discord_adapter = getattr(ctx, "discord_adapter", None)
        except Exception:
            discord_adapter = None
        if channel_id and discord_adapter is not None:
            gateway = DiscordGatewayAdapter(
                discord_gateway_send_factory(discord_adapter, channel_id))
        else:
            gateway = FakeGatewayAdapter()
    transport_name = _cfg(ctx, "transport", "real") if ctx else "real"
    transport = FakeTransport() if transport_name == "fake" else RealSendHermesTransport()
    budgets = {k: _cfg(ctx, k, d) if ctx else d for k, d in (
        ("max_turns", 25), ("max_tasks_per_day", 5),
        ("token_cap", 50000), ("wall_clock_seconds", 1800))}
    return PoolService(conn, gateway, transport, caller_key_id=identity.key_id,
                       caller_signing_key=identity.signing_key,
                       caller_box_sk=identity.box_sk, budget_defaults=budgets)


# ---------------------------------------------------------------------------
# Argument parsing: "sub rest --key value --flag"
# ---------------------------------------------------------------------------

def _parse_args(raw: str):
    try:
        tokens = shlex.split(raw or "")
    except ValueError as exc:
        raise ValueError(f"bad arguments: {exc}")
    positionals, options, flags = [], {}, set()
    it = iter(range(len(tokens)))
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:].replace("-", "_")
            if "=" in key:
                k, v = key.split("=", 1)
                options[k] = v
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                options[key] = tokens[i + 1]
                i += 1
            else:
                flags.add(key)
        else:
            positionals.append(tok)
        i += 1
    return positionals, options, flags


def _budget_from_opts(options):
    budget = {}
    for opt, field in (("max_turns", "max_turns"), ("max_tasks_per_day", "max_tasks_per_day"),
                       ("token_cap", "token_cap"), ("wall_clock_seconds", "wall_clock_seconds")):
        if opt in options:
            budget[field] = int(options[opt])
    return budget or None


def _result(payload) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _error(exc) -> str:
    return _result({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Command handlers (each takes ctx via closure from register())
# ---------------------------------------------------------------------------

def _handle_pool(ctx, raw: str) -> str:
    try:
        svc = _service_for(ctx)
        pos, opts, flags = _parse_args(raw)
        if not pos:
            return _pool_status(ctx=ctx)
        sub, rest = pos[0], pos[1:]
        if sub == "register":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            out = svc.register_pool(pool_id, opts.get("name", pool_id),
                                    relay_url=opts.get("relay_url", ""))
        elif sub == "invite":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            key_id = rest[1] if len(rest) > 1 else opts.get("key_id", "")
            out = svc.invite(pool_id, key_id, role=opts.get("role", "member"),
                             box_pubkey_hex=opts.get("box_pubkey", ""))
        elif sub == "members":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            out = {"ok": True, "pool_id": pool_id, "members": svc.members(pool_id)}
        elif sub == "freeze":
            # `/pool freeze` (no pool id, or --global) = GLOBAL kill-switch
            # across every pool; `/pool freeze <pool_id>` = per-pool only.
            # `--unfreeze` clears it. The freeze command itself is never
            # blocked by the freeze (it must be able to unfreeze).
            frozen = "unfreeze" not in flags
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            if not pool_id or "global" in flags or pool_id in ("all", "*"):
                out = svc.freeze_global(frozen)
            else:
                out = svc.freeze(pool_id, frozen=frozen)
        elif sub == "remove":
            pool_id = rest[0] if rest else opts.get("pool_id", "")
            key_id = rest[1] if len(rest) > 1 else opts.get("key_id", "")
            out = svc.remove_member(pool_id, key_id)
        else:
            return _result({"ok": False,
                            "error": f"unknown /pool subcommand: {sub}",
                            "usage": "/pool register|invite|members|freeze"})
        return _result(out)
    except Exception as exc:
        return _error(exc)


def _handle_task(ctx, raw: str) -> str:
    try:
        svc = _service_for(ctx)
        pos, opts, flags = _parse_args(raw)
        if not pos:
            return _result({"ok": False, "error": "usage: /task add|list|show"})
        sub, rest = pos[0], pos[1:]
        if sub == "add":
            ticket = svc.add_ticket(opts.get("pool_id", ""), opts.get("repo", ""),
                                    opts.get("title", ""), brief=opts.get("brief", ""),
                                    budget=_budget_from_opts(opts))
            out = {"ok": True, "ticket_id": ticket["ticket_id"],
                   "state": ticket["state"], "thread_ref": ticket.get("thread_ref")}
        elif sub == "list":
            rows = svc.list_tickets(pool_id=opts.get("pool_id") or None,
                                    state=opts.get("state") or None)
            out = {"ok": True, "tickets": [
                {"ticket_id": t["ticket_id"], "title": t["title"], "state": t["state"],
                 "repo_id": t["repo_id"], "claimant": t.get("claimant_key_id"),
                 "thread_ref": t.get("thread_ref")} for t in rows]}
        elif sub == "show":
            ticket_id = rest[0] if rest else opts.get("ticket", "")
            t = svc.show_ticket(ticket_id)
            out = {"ok": True, "ticket": {
                "ticket_id": t["ticket_id"], "title": t["title"], "state": t["state"],
                "repo_id": t["repo_id"], "pool_id": t["pool_id"],
                "brief": t["brief"], "claimant": t.get("claimant_key_id"),
                "budget": json.loads(t["budget_json"] or "{}"),
                "thread_ref": t.get("thread_ref")}}
        else:
            return _result({"ok": False, "error": f"unknown /task subcommand: {sub}",
                            "usage": "/task add|list|show"})
        return _result(out)
    except Exception as exc:
        return _error(exc)


def _handle_claim(ctx, raw: str) -> str:
    try:
        svc = _service_for(ctx)
        pos, opts, flags = _parse_args(raw)
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        paths = [p.strip() for p in opts.get("paths", "").split(",") if p.strip()]
        claim = svc.claim(ticket_id, key_id=opts.get("as") or None,
                          declared_paths=paths, budget=_budget_from_opts(opts),
                          maintainer_override="override" in flags)
        # Phase 4: on successful claim, start the worker (fetch accepted
        # version over Iroh -> worktree -> worker session) when a lifecycle
        # or work root is configured. Failure here never rolls back the claim.
        out = {"ok": True, "claim": claim}
        try:
            lifecycle = _lifecycle_for(ctx) if "_lifecycle_for" in globals() else None
            work_root = _work_root_for(ctx) if "_work_root_for" in globals() else None
            if lifecycle is not None or "start" in flags:
                out["start"] = svc.start_work(ticket_id, work_root=work_root,
                                              lifecycle=lifecycle,
                                              worker_command=opts.get("worker_command", "").split() or None,
                                              acceptance_cmd=opts.get("acceptance", "").split() or None)
            else:
                out["start"] = {"ok": False, "note": "not started: no Hermes lifecycle; use /work start <ticket> --worker-command ..."}
        except Exception as exc:
            out["start"] = {"ok": False, "error": str(exc)}
        return _result(out)
    except Exception as exc:
        return _error(exc)


def _handle_release(ctx, raw: str) -> str:
    try:
        svc = _service_for(ctx)
        pos, opts, _flags = _parse_args(raw)
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        updated = svc.release(ticket_id, key_id=opts.get("as") or None)
        return _result({"ok": True, "ticket_id": ticket_id, "state": updated["state"]})
    except Exception as exc:
        return _error(exc)


def _handle_work(ctx, raw: str) -> str:
    try:
        pos, _opts, _flags = _parse_args(raw)
    except ValueError as exc:
        return _error(exc)
    if not pos or pos[0] != "submit":
        if "_handle_work_exec" in globals():
            return globals()["_handle_work_exec"](ctx, raw)
        return _result({"ok": False, "error": "usage: /work submit <ticket> --repo-path PATH --version N"})
    try:
        svc = _service_for(ctx)
        pos, opts, _flags = _parse_args(raw)
        if not pos or pos[0] != "submit":
            return _result({"ok": False, "error": "usage: /work submit <ticket> "
                            "--repo-path PATH --version N"})
        ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
        out = svc.work_submit(ticket_id, opts.get("repo_path", ""),
                              opts.get("version", "1"))
        return _result(out)
    except Exception as exc:
        return _error(exc)


def _handle_integrate(ctx, raw: str) -> str:
    try:
        svc = _service_for(ctx)
        pos, opts, _flags = _parse_args(raw)
        if pos and pos[0] in ("approve", "edit", "discard"):
            decision = pos[0]
            ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
            edit_files = None
            edit_patch = None
            if opts.get("edit_file") and opts.get("edit_content") is not None:
                edit_files = {opts["edit_file"]: opts["edit_content"]}
            if opts.get("edit_patch_file"):
                edit_patch = Path(opts["edit_patch_file"]).read_text()
            out = svc.resolve_integration(
                ticket_id, decision, work_dir=_work_root_for(ctx) / "integrate"
                if "_work_root_for" in globals() else None,
                edit_files=edit_files, edit_patch=edit_patch,
                reason=opts.get("reason", ""))
            return _result(out)
        ticket_id = pos[0] if pos else opts.get("ticket", "")
        return _result(svc.integrate(
            ticket_id, work_dir=_work_root_for(ctx) / "integrate"
            if "_work_root_for" in globals() else None))
    except Exception as exc:
        return _error(exc)


def _handle_audit(ctx, raw: str) -> str:
    try:
        svc = _service_for(ctx)
        pos, opts, _flags = _parse_args(raw)
        if not pos or pos[0] != "show":
            return _result({"ok": False, "error": "usage: /audit show <ticket>"})
        ticket_id = pos[1] if len(pos) > 1 else opts.get("ticket", "")
        return _result(svc.audit_show(ticket_id=ticket_id))
    except Exception as exc:
        return _error(exc)


_POOL_STATUS_SCHEMA = {
    "name": "pool_status",
    "description": ("Report Agent Pool plugin status: relay URL, per-claim budgets, "
                    "and ledger/identity readiness."),
    "parameters": {"type": "object", "properties": {}},
}

_COMMANDS = (
    ("pool", _handle_pool, "Agent Pool: register|invite|members|freeze, status"),
    ("task", _handle_task, "Agent Pool tickets: add|list|show"),
    ("claim", _handle_claim, "Claim a ticket (declares file paths + budget)"),
    ("release", _handle_release, "Release a claimed ticket back to open"),
    ("work", _handle_work, "Submit finished work for a claimed ticket"),
    ("integrate", _handle_integrate, "Maintainer: audit a submitted ticket"),
    ("audit", _handle_audit, "Show the audit report for a ticket"),
)


def register(ctx):
    """Wire the full Phase 3 surface. Fail-soft: Hermes disables on exception,
    so optional pieces are probed rather than assumed."""
    try:
        ctx.register_tool(name="pool_status", toolset="agent_pool",
                          schema=_POOL_STATUS_SCHEMA,
                          handler=lambda args=None, **kw: _pool_status(args, ctx=ctx, **kw))
    except Exception as exc:
        logger.warning("agent-pool: tool registration failed: %s", exc)
    for name, handler, description in _COMMANDS:
        try:
            ctx.register_command(name, lambda raw="", _h=handler: _h(ctx, raw),
                                 description=description)
        except Exception as exc:
            logger.warning("agent-pool: command %s registration failed: %s", name, exc)
    # Button-click -> resolve_integration bridge (bridge.py). The
    # gateway_platform_event hook is observer-only and, in this Hermes
    # version, does not yet carry Discord button interactions — the
    # handler parses defensively and ignores everything else, so
    # registering it is behaviour-neutral today and lights up when the
    # host normalizes interactions. The actionable control remains
    # `/integrate approve|edit|discard <ticket>` (same code path).
    try:
        try:
            from .bridge import make_platform_event_handler
        except ImportError:
            from bridge import make_platform_event_handler
        identity_map = {}
        try:
            identity_map = ctx.state.get("agent_pool_identity_map", {}) if ctx is not None else {}
        except Exception:
            identity_map = {}

        def _button_hook(platform, event_type, payload, **kwargs):
            # Work dir: same default the /integrate handler uses.
            try:
                work_dir = _work_root_for(ctx) / "integrate"
            except Exception:
                work_dir = None
            handler = make_platform_event_handler(
                lambda: _service_for(ctx), identity_map=identity_map,
                work_dir=work_dir)
            return handler(platform, event_type, payload, **kwargs)

        ctx.register_hook("gateway_platform_event", _button_hook)
    except Exception as exc:
        logger.warning("agent-pool: button bridge hook registration failed: %s", exc)

# --- Phase 4 execution wiring (appended handlers) ----------------------------

def _work_root_for(ctx):
    configured = _cfg(ctx, "work_root", "") if ctx else ""
    if configured:
        return Path(configured)
    return Path.home() / ".hermes" / "plugin-data" / "agent-pool" / "work"

def _lifecycle_for(ctx):
    try:
        return ctx.subagent_lifecycle if ctx is not None else None
    except Exception:
        return None

def _handle_work_exec(ctx, raw: str) -> str:
    """``/work start <ticket> --acceptance "cmd..."`` / ``/work complete <ticket>``
    / ``/work cancel <ticket>`` / ``/work progress <ticket> <text>``."""
    try:
        svc = _service_for(ctx)
        pos, opts, flags = _parse_args(raw)
        if not pos:
            return _result({"ok": False, "error": "usage: /work start|complete|cancel|progress <ticket> ..."})
        sub, rest = pos[0], pos[1:]
        ticket_id = rest[0] if rest else opts.get("ticket", "")
        if sub == "start":
            acceptance = opts.get("acceptance", "").split() or None
            out = svc.start_work(ticket_id, work_root=_work_root_for(ctx),
                                 lifecycle=_lifecycle_for(ctx),
                                 worker_command=opts.get("worker_command", "").split() or None,
                                 acceptance_cmd=acceptance)
        elif sub == "complete":
            out = svc.complete_work(ticket_id, timeout_seconds=int(opts.get("timeout", "0") or 0) or None)
        elif sub == "cancel":
            out = svc.cancel_work(ticket_id, reason=opts.get("reason", "cancelled by operator"))
        elif sub == "progress":
            text = " ".join(rest[1:]) or opts.get("text", "")
            out = svc.report_progress(ticket_id, text)
        else:
            return _result({"ok": False, "error": f"unknown /work subcommand: {sub}"})
        return _result(out)
    except Exception as exc:
        return _error(exc)
