"""Agent Pool plugin — registration (Phase 1: foundations).

Exposes ``pool_status`` (tool) and ``/pool`` (command). Real pool operations
(submit/fetch/claim/integrate) land in Phase 2+; this phase wires config,
the ledger, local identity, and the gateway abstraction.
"""
import json
import logging

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
        status = {"ok": True, "plugin": "agent-pool", "phase": 1,
                  "relay_url": relay_url, "budgets": budgets,
                  "ledger": "ready", "identity": "ready"}
        return json.dumps(status)
    except Exception as exc:  # never raise from a handler
        return json.dumps({"ok": False, "error": str(exc)})


def _handle_pool(ctx, raw_args: str):
    return _pool_status(ctx=ctx)


_POOL_STATUS_SCHEMA = {
    "name": "pool_status",
    "description": ("Report Agent Pool plugin status: relay URL, per-claim budgets, "
                    "and ledger/identity readiness."),
    "parameters": {"type": "object", "properties": {}},
}


def register(ctx):
    """Wire the Phase 1 surface. Fail-soft: Hermes disables on exception, so
    optional pieces are probed rather than assumed."""
    # Ledger + identity are created lazily by their modules (plugin_db /
    # plugin_data_dir); register() only needs config + surface wiring here.
    try:
        ctx.register_tool(name="pool_status", toolset="agent_pool",
                          schema=_POOL_STATUS_SCHEMA,
                          handler=lambda args=None, **kw: _pool_status(args, ctx=ctx, **kw))
    except Exception as exc:
        logger.warning("agent-pool: tool registration failed: %s", exc)
    try:
        ctx.register_command(
            "pool",
            lambda raw="": _handle_pool(ctx, raw),
            description="Agent Pool status (Phase 1 foundations)",
        )
    except Exception as exc:
        logger.warning("agent-pool: command registration failed: %s", exc)
