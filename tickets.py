"""Client ticket system: numbered channels, claim/close/reopen, transcripts and linked follow-ups."""
import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from database import ticket_number
from utils import (TICKET_KINDS, TICKET_PRIORITIES, days_since, fmt, is_ticket_staff,
                   safe_channel_name, staff_interaction_check, to_cents)
from views import TicketOpenPanelView, build_ticket_view, simple_card

log = logging.getLogger("invoice-bot.tickets")


class Tickets(commands.GroupCog, group_name="ticket", group_description="Client tickets and follow-ups"):
    def __init__(self, bot):
        self.bot = bot
        self.bot.tickets = self  # views call back into this cog
        super().__init__()
        self.stale_digest.start()

    async def cog_load(self):
        # Re-attach button handlers so panels keep working after a restart.
        self.bot.add_view(TicketOpenPanelView(self.bot))
        rows = await self.bot.db.fetchall("SELECT id FROM tickets ORDER BY id DESC LIMIT 300")
        for row in rows:
            view = await build_ticket_view(self.bot, row["id"])
            if view:
                self.bot.add_view(view)
        log.info("Restored %s ticket panels", len(rows))

    async def cog_unload(self):
        self.stale_digest.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _overwrites(guild: discord.Guild, client_id: int | None, closed: bool = False):
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        member = guild.get_member(client_id) if client_id else None
        target = member or (discord.Object(id=client_id) if client_id else None)
        if target:
            overwrites[target] = discord.PermissionOverwrite(
                view_channel=not closed, send_messages=not closed, attach_files=not closed,
                read_message_history=True, add_reactions=not closed, embed_links=not closed,
            )
        for role_id in config.TICKET_STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=not closed, attach_files=not closed,
                    read_message_history=True, manage_messages=True, add_reactions=not closed,
                )
        me = guild.me
        if me:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True,
                manage_channels=True, manage_messages=True, embed_links=True,
            )
        return overwrites

    @staticmethod
    def _category(guild: discord.Guild, category_id):
        if not category_id:
            return None
        channel = guild.get_channel(category_id)
        return channel if isinstance(channel, discord.CategoryChannel) else None

    async def _respond(self, interaction: discord.Interaction, text: str, *, ephemeral: bool = True):
        try:
            await interaction.response.send_message(text, ephemeral=ephemeral,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except discord.InteractionResponded:
            await interaction.followup.send(text, ephemeral=ephemeral,
                                            allowed_mentions=discord.AllowedMentions.none())

    async def _channel(self, channel_id):
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.HTTPException, TypeError):
            return None

    async def _resolve(self, interaction: discord.Interaction, ticket_id: int | None) -> dict:
        ticket = await self.bot.db.get_ticket(ticket_id) if ticket_id else \
            await self.bot.db.ticket_by_channel(interaction.channel_id)
        if not ticket:
            raise ValueError("Ticket not found. Use this inside a ticket channel or pass a ticket ID.")
        return ticket

    # ------------------------------------------------------------------ create
    async def open_ticket(self, interaction: discord.Interaction, *, client: Optional[discord.Member],
                          kind: str = "purchase", subject: str = "", details: str = "",
                          service: str = "", budget: int = 0, parent_id: Optional[int] = None,
                          priority: str = "normal") -> Optional[int]:
        """Create the channel, the ticket row and the panel. Used by commands and panels."""
        guild = interaction.guild
        if guild is None:
            return None
        kind = (kind or "support").lower()
        if kind not in TICKET_KINDS:
            await self._respond(interaction, "❌ Unknown ticket type.")
            return None
        priority = (priority or "normal").lower()
        if priority not in TICKET_PRIORITIES:
            priority = "normal"
        subject = (subject or "").strip()[:120]
        if not subject:
            await self._respond(interaction, "❌ Add a short subject.")
            return None

        parent = None
        if parent_id:
            parent = await self.bot.db.get_ticket(parent_id)
            if not parent:
                await self._respond(interaction, "❌ The ticket you are following up doesn't exist.")
                return None
            if client is None and parent.get("client_discord_id"):
                client = guild.get_member(parent["client_discord_id"])
                if client is None:
                    try:
                        client = await guild.fetch_member(parent["client_discord_id"])
                    except discord.HTTPException:
                        client = None
            service = service or ""
            if not details:
                details = f"Follow-up of {ticket_number(parent['id'])} — {parent['subject']}"

        if client is None:
            await self._respond(interaction, "❌ Pick the client for this ticket.")
            return None
        if not is_ticket_staff(interaction.user):
            open_count = await self.bot.db.count_open_tickets(client.id)
            if config.TICKET_MAX_OPEN_PER_USER and open_count >= config.TICKET_MAX_OPEN_PER_USER:
                await self._respond(
                    interaction,
                    f"❌ {client.mention} already has {open_count} open tickets. "
                    "Close one first or raise `TICKET_MAX_OPEN_PER_USER`.",
                )
                return None

        category = self._category(guild, config.TICKET_CATEGORY_ID)
        channel = None
        try:
            channel = await guild.create_text_channel(
                safe_channel_name(f"{config.TICKET_PREFIX}-{subject}", 90),
                category=category,
                overwrites=self._overwrites(guild, client.id),
                topic=f"{kind.title()} · {subject[:100]} · client {client}"[:1024],
                reason=f"Ticket opened by {interaction.user}",
            )
            ticket_id = await self.bot.db.create_ticket(
                interaction.user.id, subject=subject, kind=kind, priority=priority,
                client_discord_id=client.id, client_name=client.display_name,
                channel_id=channel.id, parent_ticket_id=parent["id"] if parent else None,
                budget=budget, service=service[:120], note=details,
            )
        except discord.HTTPException as e:
            if channel:
                try:
                    await channel.delete(reason="Ticket creation failed")
                except discord.HTTPException:
                    pass
            log.exception("Could not create ticket channel")
            await self._respond(interaction, f"❌ Could not create the ticket channel: {e}")
            return None
        except Exception as e:
            if channel:
                try:
                    await channel.delete(reason="Ticket creation failed")
                except discord.HTTPException:
                    pass
            log.exception("Could not create ticket")
            await self._respond(interaction, f"❌ Could not create the ticket: {e}")
            return None

        label = ticket_number(ticket_id)
        try:
            await channel.edit(name=f"{config.TICKET_PREFIX}-{ticket_id:04d}",
                               topic=f"{label} · {kind.title()} · {subject[:90]} · client {client}"[:1024])
        except discord.HTTPException:
            log.warning("Could not rename ticket channel %s", channel.id)

        # Acknowledge first — channel creation is the slow part. A failed reply must
        # never prevent the channel/panel from being set up.
        try:
            await self._respond(
                interaction,
                f"✅ Ticket {label} created: {channel.mention}\n"
                f"Client: {client.mention} · Type: **{kind}** · "
                f"{('Follow-up of ' + ticket_number(parent['id'])) if parent else 'New ticket'}",
            )
        except discord.HTTPException:
            log.warning("Ticket %s created but the confirmation reply failed", ticket_id)

        intro = (f"{client.mention} your ticket **{label}** is open. "
                 f"A staff member will reply here — no need to open another ticket.")
        if details:
            clean = discord.utils.escape_mentions(details.strip())[:1500]
            intro += f"\n\n**Request details**\n{clean}"
        if parent:
            intro += f"\n\n🔗 Follow-up of **{ticket_number(parent['id'])}**."
        try:
            await channel.send(intro, allowed_mentions=discord.AllowedMentions(users=[client]))
            view = await build_ticket_view(self.bot, ticket_id)
            if view:
                await channel.send(view=view)
        except discord.HTTPException:
            log.exception("Ticket %s created but the panel could not be posted", ticket_id)
        return ticket_id

    # ------------------------------------------------------------------ close / reopen
    async def save_transcript(self, ticket_id: int, guild: Optional[discord.Guild] = None,
                              channel=None) -> str:
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if channel is None:
            channel = self.bot.get_channel(ticket.get("channel_id") or 0)
            if channel is None and guild:
                try:
                    channel = await self.bot.fetch_channel(ticket["channel_id"])
                except (discord.HTTPException, TypeError):
                    channel = None
        if channel is None:
            raise ValueError("The ticket channel no longer exists, so there is nothing to transcribe.")
        os.makedirs(config.TICKET_TRANSCRIPT_DIR, exist_ok=True)
        path = os.path.join(config.TICKET_TRANSCRIPT_DIR, f"{ticket_number(ticket_id)}.txt")
        lines = [
            f"Ticket {ticket_number(ticket_id)} — {ticket['subject']}",
            f"Client: {ticket['client_name']} ({ticket.get('client_discord_id') or 'unknown'})",
            f"Type: {ticket['kind']} · Priority: {ticket['priority']} · Status: {ticket['status']}",
            f"Opened: {ticket['created_at']} · Last activity: {ticket['last_activity']}",
            f"Claimed by: {ticket.get('claimed_by') or 'nobody'}",
            "-" * 70,
            "",
        ]
        count = 0
        try:
            async for message in channel.history(limit=5000, oldest_first=True):
                count += 1
                body = (message.clean_content or "").strip().replace("\n", "\n    ")
                if body:
                    lines.append(f"[{message.created_at:%Y-%m-%d %H:%M}] {message.author} ({message.author.id}): {body}")
                for attachment in message.attachments:
                    lines.append(f"    attachment: {attachment.url}")
        except discord.HTTPException as e:
            lines.append(f"[transcript incomplete: {e}]")
        if not count:
            lines.append("(no messages)")
        text = "\n".join(lines) + "\n"

        def _write():
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)

        await asyncio.to_thread(_write)
        return path

    async def _log(self, text: str, file_path: str | None = None):
        channel = self.bot.get_channel(config.TICKET_LOG_CHANNEL_ID) if config.TICKET_LOG_CHANNEL_ID else None
        if channel is None:
            return
        try:
            if file_path and os.path.exists(file_path):
                await channel.send(text, file=discord.File(file_path,
                                                            filename=os.path.basename(file_path)))
            else:
                await channel.send(text)
        except discord.HTTPException:
            log.exception("Could not post to the ticket log channel")
            try:
                await channel.send(text)
            except discord.HTTPException:
                pass

    async def close_ticket(self, guild: discord.Guild, ticket_id: int, actor, reason: str = "") -> dict:
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if ticket["status"] == "closed":
            raise ValueError("That ticket is already closed.")
        channel = await self._channel(ticket.get("channel_id"))
        transcript = None
        if channel:
            try:
                transcript = await self.save_transcript(ticket_id, guild, channel)
            except (ValueError, OSError) as e:
                log.warning("Transcript failed for ticket %s: %s", ticket_id, e)
            kwargs = dict(
                name=f"{config.TICKET_CLOSED_PREFIX}-{ticket_id:04d}",
                overwrites=self._overwrites(guild, ticket.get("client_discord_id"), closed=True),
                reason=f"Closed by {actor}",
            )
            closed_category = self._category(guild, config.TICKET_CLOSED_CATEGORY_ID)
            if closed_category:
                kwargs["category"] = closed_category
            try:
                await channel.edit(**kwargs)
                await channel.send(f"🔒 Ticket closed by {actor.mention}. "
                                   f"Reason: {discord.utils.escape_mentions(reason or 'not given')}")
            except discord.HTTPException:
                log.exception("Could not lock ticket channel %s", ticket_id)
        await self.bot.db.set_ticket_status(actor.id, ticket_id, "closed", reason, transcript)
        await self._log(
            f"⚫ **{ticket_number(ticket_id)}** closed by {actor.mention} · client {ticket['client_name']} · "
            f"type {ticket['kind']} · reason: {reason or '—'}", transcript)
        return await self.bot.db.get_ticket(ticket_id)

    async def reopen_ticket(self, guild: discord.Guild, ticket_id: int, actor) -> dict:
        ticket = await self.bot.db.get_ticket(ticket_id)
        if not ticket:
            raise ValueError("Ticket not found.")
        if ticket["status"] != "closed":
            raise ValueError("That ticket is not closed.")
        channel = await self._channel(ticket.get("channel_id"))
        if channel:
            kwargs = dict(
                name=f"{config.TICKET_PREFIX}-{ticket_id:04d}",
                overwrites=self._overwrites(guild, ticket.get("client_discord_id"), closed=False),
                reason=f"Reopened by {actor}",
            )
            category = self._category(guild, config.TICKET_CATEGORY_ID)
            if category:
                kwargs["category"] = category
            try:
                await channel.edit(**kwargs)
                await channel.send(f"🔓 Ticket reopened by {actor.mention}.")
            except discord.HTTPException:
                log.exception("Could not reopen ticket channel %s", ticket_id)
        await self.bot.db.set_ticket_status(actor.id, ticket_id, "open")
        await self._log(f"🔓 **{ticket_number(ticket_id)}** reopened by {actor.mention} · client {ticket['client_name']}")
        if channel:
            view = await build_ticket_view(self.bot, ticket_id)
            if view:
                try:
                    await channel.send(view=view)
                except discord.HTTPException:
                    pass
        return await self.bot.db.get_ticket(ticket_id)

    # ------------------------------------------------------------------ commands
    @app_commands.command(name="open", description="Open a numbered ticket channel for a client")
    @app_commands.choices(kind=[app_commands.Choice(name=k.title(), value=k) for k in TICKET_KINDS],
                          priority=[app_commands.Choice(name=p.title(), value=p) for p in TICKET_PRIORITIES])
    async def open_cmd(self, interaction: discord.Interaction, client: discord.Member, subject: str,
                       kind: str = "purchase", priority: str = "normal",
                       budget: app_commands.Range[float, 0.0, 100_000_000.0] = 0.0,
                       service: Optional[str] = None, details: Optional[str] = None,
                       parent: Optional[int] = None):
        await self.open_ticket(
            interaction, client=client, kind=kind, subject=subject, details=details or "",
            service=service or "", budget=to_cents(budget), parent_id=parent, priority=priority,
        )

    @app_commands.command(name="panel", description="Post the public ticket-opening panel")
    async def panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        target = channel or interaction.channel
        if target is None:
            return await interaction.response.send_message("❌ Pick a channel for the panel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await target.send(view=TicketOpenPanelView(self.bot))
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ Could not post the panel: {e}", ephemeral=True)
        await interaction.followup.send(f"✅ Ticket panel posted in {target.mention}.", ephemeral=True)

    @app_commands.command(name="info", description="Show a ticket panel")
    async def info(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        view = await build_ticket_view(self.bot, row["id"])
        if not view:
            return await interaction.response.send_message("❌ Ticket not found.", ephemeral=True)
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="list", description="List tickets (filter by status, type or client)")
    @app_commands.choices(status=[app_commands.Choice(name=s.title(), value=s)
                                  for s in ("open", "pending", "closed", "all")],
                          kind=[app_commands.Choice(name=k.title(), value=k) for k in TICKET_KINDS])
    async def list_cmd(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None,
                       kind: Optional[app_commands.Choice[str]] = None,
                       client: Optional[discord.Member] = None):
        rows = await self.bot.db.list_tickets(status.value if status else "open",
                                              kind.value if kind else None,
                                              client.id if client else None, limit=25)
        if not rows:
            return await interaction.response.send_message(
                view=simple_card("Tickets", "No tickets match that filter."), ephemeral=True)
        lines = []
        for row in rows:
            icon = {"open": "🟢", "pending": "🟡", "closed": "⚫"}.get(row["status"], "•")
            claim = f" · <@{row['claimed_by']}>" if row.get("claimed_by") else " · unclaimed"
            channel = f"<#{row['channel_id']}>" if row.get("channel_id") else "no channel"
            lines.append(f"{icon} **{ticket_number(row['id'])}** · {row['kind']} · {row['priority']} · "
                         f"{row['client_name'][:20]} · {channel}{claim} · {row['last_activity'][:10]}")
        await interaction.response.send_message(
            view=simple_card("Tickets · " + (status.value if status else "open"), "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="claim", description="Claim (or unclaim) a ticket")
    async def claim(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        claimed = None if row.get("claimed_by") == interaction.user.id else interaction.user.id
        await self.bot.db.set_ticket_claimed(interaction.user.id, row["id"], claimed)
        await interaction.response.send_message(
            f"{'🙋 Claimed' if claimed else '↩️ Released'} **{ticket_number(row['id'])}**.", ephemeral=True)

    @app_commands.command(name="assign", description="Assign a ticket to a staff member")
    async def assign(self, interaction: discord.Interaction, staff: discord.Member,
                     ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        if not is_ticket_staff(staff):
            return await interaction.response.send_message(
                "❌ That member has no ticket staff role. Add their role to `TICKET_STAFF_ROLE_IDS` first.",
                ephemeral=True)
        await self.bot.db.set_ticket_claimed(interaction.user.id, row["id"], staff.id)
        await interaction.response.send_message(
            f"🙋 **{ticket_number(row['id'])}** assigned to {staff.mention}.", ephemeral=True)

    @app_commands.command(name="note", description="Save an internal note on a ticket")
    async def note(self, interaction: discord.Interaction, note: str, ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
            await self.bot.db.add_ticket_note(interaction.user.id, row["id"], note)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message("📝 Note saved.", ephemeral=True)

    @app_commands.command(name="priority", description="Change a ticket's priority")
    @app_commands.choices(priority=[app_commands.Choice(name=p.title(), value=p) for p in TICKET_PRIORITIES])
    async def priority_cmd(self, interaction: discord.Interaction, priority: app_commands.Choice[str],
                           ticket: Optional[int] = None):
        try:
            row = await self._resolve(interaction, ticket)
            await self.bot.db.set_ticket_priority(interaction.user.id, row["id"], priority.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message(
            f"✅ **{ticket_number(row['id'])}** is now **{priority.value}**.", ephemeral=True)

    @app_commands.command(name="close", description="Close a ticket, lock the channel and save a transcript")
    async def close_cmd(self, interaction: discord.Interaction, ticket: Optional[int] = None,
                        reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._resolve(interaction, ticket)
            await self.close_ticket(interaction.guild, row["id"], interaction.user, reason or "")
        except (ValueError, discord.HTTPException) as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send(f"⚫ **{ticket_number(row['id'])}** closed.", ephemeral=True)

    @app_commands.command(name="reopen", description="Reopen a closed ticket")
    async def reopen_cmd(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._resolve(interaction, ticket)
            await self.reopen_ticket(interaction.guild, row["id"], interaction.user)
        except (ValueError, discord.HTTPException) as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send(f"🔓 **{ticket_number(row['id'])}** reopened.", ephemeral=True)

    @app_commands.command(name="transcript", description="Download a ticket transcript")
    async def transcript(self, interaction: discord.Interaction, ticket: Optional[int] = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self._resolve(interaction, ticket)
            path = await self.save_transcript(row["id"], interaction.guild)
        except (ValueError, OSError, discord.HTTPException) as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send(file=discord.File(path), ephemeral=True)

    @app_commands.command(name="dashboard", description="Ticket and sales overview")
    async def dashboard(self, interaction: discord.Interaction):
        stats = await self.bot.db.ticket_stats()
        pipeline = await self.bot.db.pipeline()
        stale = await self.bot.db.stale_tickets(config.TICKET_STALE_DAYS)
        lines = [
            f"**Open:** {stats['open']}  ·  **Pending:** {stats['pending']}  ·  "
            f"**Closed:** {stats['closed']}  ·  **Unclaimed:** {stats['unclaimed']}",
            "",
            "**Sales pipeline**",
        ]
        for row in pipeline:
            if row["stage"] in ("won", "lost"):
                continue
            lines.append(f"• {row['stage'].title()}: **{row['count']}** deals · "
                         f"value {fmt(row['value'])} · weighted {fmt(row['weighted'])}")
        total = sum(row["count"] for row in pipeline)
        if not total:
            lines.append("• No deals yet — buying-enquiry tickets create them automatically.")
        lines += ["", f"**Stale tickets** (no activity for {config.TICKET_STALE_DAYS}+ days): {len(stale)}"]
        for row in stale[:8]:
            link = f"<#{row['channel_id']}>" if row.get("channel_id") else ticket_number(row["id"])
            idle = days_since((row.get("last_activity") or "")[:10])
            lines.append(f"• {link} · {row['client_name'][:20]} · "
                         f"{idle if idle is not None else '?'}d · {row['subject'][:40]}")
        await interaction.response.send_message(view=simple_card("🎫 Ticket dashboard", "\n".join(lines)),
                                                ephemeral=True)

    # ------------------------------------------------------------------ background
    @tasks.loop(hours=24)
    async def stale_digest(self):
        try:
            stale = await self.bot.db.stale_tickets(config.TICKET_STALE_DAYS)
            if not stale:
                return
            lines = [f"⏰ **{len(stale)} ticket(s)** have had no activity for "
                     f"{config.TICKET_STALE_DAYS}+ days:"]
            for row in stale[:15]:
                channel = f"<#{row['channel_id']}>" if row.get("channel_id") else ticket_number(row["id"])
                lines.append(f"• {channel} · {row['client_name']} · {row['subject'][:60]} · "
                             f"last {row['last_activity'][:10]}")
            await self._log("\n".join(lines))
        except Exception:
            log.exception("Stale ticket digest failed")

    @stale_digest.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Close the ticket row when its channel is deleted manually."""
        try:
            ticket = await self.bot.db.ticket_by_channel(channel.id)
            if not ticket or ticket["status"] == "closed":
                return
            actor_id = self.bot.user.id if self.bot.user else 0
            await self.bot.db.set_ticket_status(actor_id, ticket["id"], "closed", "Channel deleted")
            await self._log(f"⚫ **{ticket_number(ticket['id'])}** channel was deleted; ticket marked closed.")
        except Exception:
            log.exception("Could not close ticket for deleted channel %s", channel.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        try:
            if await self.bot.db.ticket_by_channel(message.channel.id):
                await self.bot.db.touch_ticket(message.channel.id)
        except Exception:
            log.debug("Could not touch ticket activity", exc_info=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
