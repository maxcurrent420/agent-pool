"""Discord button gate — gateway-native (spec §4).

Two paths share PoolService.resolve_integration:
 (a) persistent discord.ui.View via ctx.register_platform_handler("discord", factory)
 (c) ctx.ask_choice instant fallback inside /integrate (see __init__).

`discord` is imported INSIDE the factory, never at module import time.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def make_discord_button_factory(dispatch_fn):
    """Return _discord_button_factory(native, adapter) for register_platform_handler.

    dispatch_fn(decision, ticket_id, discord_user_id) -> dict
    """
    def _discord_button_factory(native, adapter):
        import discord  # noqa: F401  (imported inside factory per spec)

        # Reuse bridge parsing (extended to strip agent-pool: prefix).
        try:
            from .bridge import parse_button_event
        except ImportError:
            from bridge import parse_button_event

        class TicketDecisionView(discord.ui.View):
            def __init__(self, ticket_id: str):
                super().__init__(timeout=None)
                for decision, label in (("approve", "Approve"),
                                        ("edit", "Edit"),
                                        ("discard", "Discard")):
                    btn = discord.ui.Button(
                        label=label,
                        custom_id=f"agent-pool:{decision}:{ticket_id}",
                        style=(discord.ButtonStyle.success
                               if decision == "approve" else
                               discord.ButtonStyle.danger if decision == "discard"
                               else discord.ButtonStyle.secondary),
                    )
                    btn.callback = self._make_callback(decision)
                    self.add_item(btn)

            @staticmethod
            def _make_callback(decision):
                async def _cb(interaction):
                    return await _handle_interaction(interaction, decision)
                return _cb

        async def _handle_interaction(interaction, decision_from_view=None):
            # Must respond within 3s: defer immediately.
            try:
                if not getattr(interaction.response, "_responded", False):
                    try:
                        await interaction.response.defer(ephemeral=False)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                data = getattr(interaction, "data", {}) or {}
                custom_id = data.get("custom_id", "") if isinstance(data, dict) else ""
                parsed = parse_button_event({"custom_id": custom_id}) if custom_id else None
                if parsed is None:
                    return
                user = getattr(interaction, "user", None)
                user_id = str(getattr(user, "id", "")) if user is not None else ""
                result = dispatch_fn(parsed["decision"], parsed["ticket_id"], user_id)
                msg = f"Decision recorded: {parsed['decision']} on {parsed['ticket_id']}"
                if isinstance(result, dict) and result.get("error"):
                    msg = f"Refused: {result['error']}"
                try:
                    await interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    try:
                        await interaction.channel.send(msg)
                    except Exception:
                        pass
            except PermissionError as exc:
                try:
                    await interaction.followup.send(str(exc), ephemeral=True)
                except Exception:
                    pass
            except Exception as exc:  # never break the gateway
                logger.warning("agent-pool button: %s", exc)
                try:
                    await interaction.followup.send(f"Error: {exc}", ephemeral=True)
                except Exception:
                    pass

        async def _on_interaction(interaction):
            try:
                from discord import InteractionType
                itype = getattr(interaction, "type", None)
                if itype != InteractionType.component:
                    return
            except Exception:
                return
            try:
                data = getattr(interaction, "data", {}) or {}
                custom_id = data.get("custom_id", "") if isinstance(data, dict) else ""
                if not custom_id.startswith("agent-pool:"):
                    return
            except Exception:
                return
            await _handle_interaction(interaction)

        native.add_listener(_on_interaction, "on_interaction")
        # Persistent views would be added via native.add_view(TicketDecisionView(...))
        # for known tickets; listeners cover new messages.

    return _discord_button_factory


def parse_custom_id(custom_id: str):
    """Parse 'agent-pool:{decision}:{ticket_id}' -> (decision, ticket_id) or None."""
    try:
        from .bridge import parse_button_event
    except ImportError:
        from bridge import parse_button_event
    parsed = parse_button_event({"custom_id": custom_id})
    if parsed is None:
        return None
    return parsed["decision"], parsed["ticket_id"]
