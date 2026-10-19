from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import LOG_CHANNEL_ID
from utils import (PRODUCT_TYPES, fmt, is_ticket_staff, normalize_product, parse_items,
                   staff_interaction_check, to_cents, valid_month)
from views import (InvoiceView, STATUS_STYLE, deliver_invoice, make_pdf_file,
                   send_invoice_to_channel, simple_card)

TicketChannel = Union[discord.TextChannel, discord.Thread]


class ItemsModal(discord.ui.Modal, title="Invoice items"):
    items = discord.ui.TextInput(
        label="Items (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="VPS 4GB | 1 | 700 | type=VPS; node=Ryzen; plan=4GB; link=https://panel...",
        max_length=1800,
    )
    discount = discord.ui.TextInput(label="Discount amount (0 for none)", required=False, max_length=15)
    tax = discord.ui.TextInput(label="Tax % (leave empty for no tax)", required=False, max_length=6)
    notes = discord.ui.TextInput(label="Notes (optional)", style=discord.TextStyle.paragraph,
                                 required=False, max_length=500)

    def __init__(self, cog, opts: dict):
        super().__init__()
        self.cog, self.opts = cog, opts
        self.discount.default = f"{opts['discount_cents'] / 100:g}"
        self.tax.default = f"{opts['tax_percent']:g}" if opts["tax_percent"] else ""

    async def on_submit(self, interaction: discord.Interaction):
        bot = self.cog.bot
        try:
            items = parse_items(self.items.value)
            opts = dict(self.opts)
            opts["discount_cents"] = to_cents(self.discount.value or "0")
            try:
                opts["tax_percent"] = float((self.tax.value or "0").replace("%", "").strip())
            except ValueError:
                raise ValueError("Tax % must be a number, like `18`.")
            if opts["discount_cents"] < 0:
                raise ValueError("Discount cannot be negative.")
            if not 0 <= opts["tax_percent"] <= 100:
                raise ValueError("Tax % must be between 0 and 100.")
            default_product = opts.pop("product", None)
            if default_product:
                for item in items:
                    if not item["product_type"]:
                        item["product_type"] = normalize_product(default_product)
            invoice_id = await bot.db.create_invoice(
                interaction.user.id, items=items, notes=self.notes.value, **opts)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        inv = await bot.db.get_invoice(invoice_id)
        await interaction.response.send_message(view=InvoiceView(bot, invoice_id, inv), ephemeral=True)
        ticket_id = self.opts["ticket_channel_id"]
        if ticket_id:
            try:
                channel = bot.get_channel(ticket_id) or await bot.fetch_channel(ticket_id)
                await send_invoice_to_channel(bot, channel, inv)
                await interaction.followup.send(f"📨 Also posted in {channel.mention}.", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("⚠️ Created, but I couldn't post in that ticket channel.", ephemeral=True)


@app_commands.guild_only()
class Invoices(commands.GroupCog, group_name="invoice", group_description="Create and manage invoices"):
    def __init__(self, bot):
        self.bot = bot
        self.bot.invoices = self  # ticket/deal panels call back into this cog
        super().__init__()
        self.overdue_check.start()

    async def cog_unload(self):
        self.overdue_check.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await staff_interaction_check(interaction)

    # ---------- reusable invoice drafts (called by ticket and sales panels) ----------
    def _draft_opts(self, *, client_id, client_name, ticket_channel_id, product=None) -> dict:
        return dict(client_discord_id=client_id, client_name=(client_name or "Client")[:100],
                    client_contact="", due_days=7, tax_percent=0.0, discount_cents=0,
                    category="Hosting Revenue", ticket_channel_id=ticket_channel_id,
                    product=product)

    async def open_draft_for_ticket(self, interaction: discord.Interaction, ticket: dict):
        if not is_ticket_staff(interaction.user):
            return await interaction.response.send_message("🚫 Ticket staff only.", ephemeral=True)
        opts = self._draft_opts(client_id=ticket.get("client_discord_id"),
                                client_name=ticket.get("client_name") or "Client",
                                ticket_channel_id=ticket.get("channel_id"))
        await interaction.response.send_modal(ItemsModal(self, opts))

    async def open_draft_for_deal(self, interaction: discord.Interaction, deal: dict):
        if not is_ticket_staff(interaction.user):
            return await interaction.response.send_message("🚫 Sales staff only.", ephemeral=True)
        opts = self._draft_opts(client_id=deal.get("client_discord_id"),
                                client_name=deal.get("client_name") or "Client",
                                ticket_channel_id=deal.get("ticket_channel_id"),
                                product=deal.get("service"))
        await interaction.response.send_modal(ItemsModal(self, opts))

    # ---------- create ----------
    @app_commands.command(name="create", description="Create a detailed invoice (items are entered in the next form)")
    @app_commands.describe(client_name="Name printed on the invoice", client="Discord user being billed",
                           due_days="Days until due", tax_percent="Tax rate in %", discount="Flat discount amount",
                           contact="Client phone/email shown on the invoice", category="Income category",
                           ticket="Ticket channel/thread to send it to",
                           product="Default digital product type for items without type=...")
    @app_commands.choices(product=[app_commands.Choice(name=p, value=p) for p in PRODUCT_TYPES])
    async def create(self, interaction: discord.Interaction, client_name: str,
                     client: Optional[discord.Member] = None,
                     due_days: app_commands.Range[int, 0, 365] = 14,
                     tax_percent: app_commands.Range[float, 0.0, 100.0] = 0.0,
                     discount: app_commands.Range[float, 0.0, 1_000_000.0] = 0.0,
                     category: str = "Hosting Revenue",
                     contact: Optional[str] = None,
                     ticket: Optional[TicketChannel] = None,
                     product: Optional[app_commands.Choice[str]] = None):
        try:
            await self.bot.db.category_id(category, "income")
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        opts = dict(client_discord_id=client.id if client else None, client_name=client_name[:100],
                    client_contact=(contact or "")[:80],
                    due_days=due_days, tax_percent=tax_percent, discount_cents=to_cents(discount),
                    category=category, ticket_channel_id=ticket.id if ticket else None,
                    product=product.value if product else None)
        await interaction.response.send_modal(ItemsModal(self, opts))

    @create.autocomplete("category")
    async def _cat_ac(self, interaction: discord.Interaction, current: str):
        cats = await self.bot.db.categories("income")
        return [app_commands.Choice(name=c["name"], value=c["name"])
                for c in cats if current.lower() in c["name"].lower()][:25]

    # ---------- read ----------
    @app_commands.command(name="view", description="Show an invoice")
    async def view(self, interaction: discord.Interaction, invoice_id: int):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await interaction.response.send_message("❌ Invoice not found.", ephemeral=True)
        await interaction.response.send_message(view=InvoiceView(self.bot, invoice_id, inv), ephemeral=True)

    @app_commands.command(name="pdf", description="Download an invoice as PDF")
    async def pdf(self, interaction: discord.Interaction, invoice_id: int):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await interaction.response.send_message("❌ Invoice not found.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(file=await make_pdf_file(inv), ephemeral=True)

    @app_commands.command(name="list", description="List invoices (filter by status or month)")
    @app_commands.choices(status=[app_commands.Choice(name=n, value=n)
                                  for n in ("unpaid", "partial", "overdue", "paid", "void")])
    async def list_cmd(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None,
                       month: Optional[str] = None):
        try:
            month = valid_month(month) if month else None
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        rows = await self.bot.db.list_invoices(status.value if status else None, month, limit=20)
        if not rows:
            return await interaction.response.send_message(view=simple_card("Invoices", "No invoices found."), ephemeral=True)
        lines = []
        for i in rows:
            label = STATUS_STYLE[i["status"]][0].split(" ", 1)[0]
            extra = " · partial" if i["is_partial"] and i["status"] == "overdue" else ""
            delivered = " · 🔗 delivered" if i.get("delivered_at") else ""
            lines.append(f"{label} **{i['number']}** · {i['client_name'][:25]} · {fmt(i['total'], i['currency'])} "
                         f"(due {fmt(i['balance'], i['currency'])}) · due {i['due_date']}{extra}{delivered}")
        title = f"Invoices{' · ' + status.value if status else ''}{' · ' + month if month else ''}"
        await interaction.response.send_message(view=simple_card(title, "\n".join(lines)), ephemeral=True)

    # ---------- payments / admin ----------
    @app_commands.command(name="pay", description="Record a (partial) payment on an invoice")
    async def pay(self, interaction: discord.Interaction, invoice_id: int, amount: float,
                  method: str = "manual", note: str = ""):
        try:
            await self.bot.db.add_payment(interaction.user.id, invoice_id, to_cents(amount), method, note)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        inv = await self.bot.db.get_invoice(invoice_id)
        await interaction.response.send_message(view=InvoiceView(self.bot, invoice_id, inv), ephemeral=True)

    @app_commands.command(name="void", description="Void an invoice (keeps history, removes it from reports)")
    async def void(self, interaction: discord.Interaction, invoice_id: int, reason: str):
        try:
            await self.bot.db.void_invoice(interaction.user.id, invoice_id, reason)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message(f"⚫ Invoice #{invoice_id} voided.", ephemeral=True)

    @app_commands.command(name="send-ticket", description="Send an invoice (card + PDF) into a ticket channel")
    async def send_ticket(self, interaction: discord.Interaction, invoice_id: int, channel: TicketChannel):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await interaction.response.send_message("❌ Invoice not found.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await send_invoice_to_channel(self.bot, channel, inv)
        except discord.HTTPException:
            return await interaction.followup.send("❌ I can't post there. Check my permissions.", ephemeral=True)
        await self.bot.db.set_ticket(interaction.user.id, invoice_id, channel.id)
        await interaction.followup.send(f"📨 Sent {inv['number']} to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="deliver", description="Post digital delivery links (panel, bot file, VPS access)")
    async def deliver(self, interaction: discord.Interaction, invoice_id: int,
                      channel: Optional[TicketChannel] = None):
        inv = await self.bot.db.get_invoice(invoice_id)
        if not inv:
            return await interaction.response.send_message("❌ Invoice not found.", ephemeral=True)
        target = channel
        if target is None and inv.get("ticket_channel_id"):
            target = self.bot.get_channel(inv["ticket_channel_id"])
        if target is None:
            return await interaction.response.send_message(
                "❌ No ticket linked. Pass a channel: `/invoice deliver invoice_id:... channel:#ticket`.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await deliver_invoice(self.bot, inv, target, interaction.user.id)
        except ValueError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send("❌ I can't post there. Check my permissions.", ephemeral=True)
        if channel is None and inv.get("ticket_channel_id"):
            await self.bot.db.set_ticket(interaction.user.id, invoice_id, target.id)
        await interaction.followup.send(f"🔗 Delivered {count} item(s) in {target.mention}.", ephemeral=True)

    # ---------- overdue watcher ----------
    @tasks.loop(hours=1)
    async def overdue_check(self):
        for inv in await self.bot.db.overdue_to_notify():
            text = (f"🔴 **{inv['number']}** for {inv['client_name']} is overdue. "
                    f"Balance: **{fmt(inv['balance'], inv['currency'])}** (was due {inv['due_date']}).")
            targets = [inv["ticket_channel_id"], LOG_CHANNEL_ID]
            for cid in filter(None, targets):
                try:
                    ch = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                    await ch.send(text)
                except discord.HTTPException:
                    pass
            await self.bot.db.mark_notified(inv["id"])

    @overdue_check.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Invoices(bot))
