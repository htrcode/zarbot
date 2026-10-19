"""Discord Components V2 views (LayoutView / Container / TextDisplay / Separator / ActionRow)."""
import asyncio
import io

import discord

from database import invoice_number
from pdf_gen import invoice_pdf, pnl_pdf
from utils import fmt, is_staff, shift_month, to_cents

STATUS_STYLE = {
    "paid": ("✅ Paid", 0x2ECC71),
    "partial": ("🟡 Partially paid", 0xF1C40F),
    "unpaid": ("🔵 Unpaid", 0x3498DB),
    "overdue": ("🔴 Overdue", 0xE74C3C),
    "void": ("⚫ Void", 0x95A5A6),
}


async def make_pdf_file(inv: dict) -> discord.File:
    data = await asyncio.to_thread(invoice_pdf, inv)
    return discord.File(io.BytesIO(data), filename=f"{inv['number']}.pdf")


def _clip(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n*…truncated*"


def _esc(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def simple_card(title: str, body: str, color: int = 0x5865F2) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView()
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"## {title}"),
        discord.ui.Separator(),
        discord.ui.TextDisplay(_clip(body) or "Nothing to show."),
        accent_colour=color,
    ))
    return view


def invoice_text(inv: dict) -> tuple[str, str, str]:
    cur = inv["currency"]
    label, _ = STATUS_STYLE[inv["status"]]
    if inv["status"] == "overdue" and inv["is_partial"]:
        label += " (partially paid)"
    client = _esc(inv["client_name"]) + (f" (<@{inv['client_discord_id']}>)" if inv["client_discord_id"] else "")
    header = (f"## 🧾 Invoice {inv['number']}  ·  {label}\n"
              f"**Client:** {client}\n"
              f"**Issued:** {inv['issue_date']}  ·  **Due:** {inv['due_date']}  ·  **Category:** {inv['category']}")
    lines = []
    for it in inv["items"][:12]:
        lines.append(f"`{it['quantity']:g}×` {_esc(it['description'][:60])} — **{fmt(it['line_total'], cur)}**")
    if len(inv["items"]) > 12:
        lines.append(f"*…and {len(inv['items']) - 12} more (see the PDF)*")
    items = "\n".join(lines)
    tot = [f"Subtotal: {fmt(inv['subtotal'], cur)}"]
    if inv["discount"]:
        tot.append(f"Discount: -{fmt(inv['discount'], cur)}")
    if inv["tax_percent"]:
        tot.append(f"Tax ({inv['tax_percent']:g}%): {fmt(inv['tax'], cur)}")
    tot.append(f"**Total: {fmt(inv['total'], cur)}**")
    tot.append(f"Paid: {fmt(inv['paid'], cur)}  ·  **Balance: {fmt(inv['balance'], cur)}**")
    return header, items, "\n".join(tot)


async def send_invoice_to_channel(bot, channel, inv: dict):
    """Post the invoice card and the PDF into a ticket channel/thread."""
    await channel.send(view=InvoiceView(bot, inv["id"], inv))
    await channel.send(content=f"📎 PDF for {inv['number']}", file=await make_pdf_file(inv))


async def refresh_invoice(bot, interaction: discord.Interaction, invoice_id: int, note: str):
    inv = await bot.db.get_invoice(invoice_id)
    try:
        await interaction.response.edit_message(view=InvoiceView(bot, invoice_id, inv))
        await interaction.followup.send(note, ephemeral=True)
    except (discord.HTTPException, discord.InteractionResponded, AttributeError):
        if interaction.response.is_done():
            await interaction.followup.send(note, ephemeral=True)
        else:
            await interaction.response.send_message(note, ephemeral=True)


class PartialPaymentModal(discord.ui.Modal, title="Record payment"):
    amount = discord.ui.TextInput(label="Amount received", placeholder="25.00", max_length=15)
    method = discord.ui.TextInput(label="Method", placeholder="PayPal / Card / Crypto", required=False, max_length=40)
    note = discord.ui.TextInput(label="Note (optional)", required=False, max_length=200)

    def __init__(self, bot, invoice_id: int):
        super().__init__()
        self.bot, self.invoice_id = bot, invoice_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.bot.db.add_payment(interaction.user.id, self.invoice_id, to_cents(self.amount.value),
                                          self.method.value or "manual", self.note.value or "")
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await refresh_invoice(self.bot, interaction, self.invoice_id, "💰 Payment recorded.")


class VoidModal(discord.ui.Modal, title="Void invoice"):
    reason = discord.ui.TextInput(label="Reason", max_length=200)

    def __init__(self, bot, invoice_id: int):
        super().__init__()
        self.bot, self.invoice_id = bot, invoice_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.bot.db.void_invoice(interaction.user.id, self.invoice_id, self.reason.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await refresh_invoice(self.bot, interaction, self.invoice_id, "⚫ Invoice voided.")


class InvoiceView(discord.ui.LayoutView):
    """Persistent: buttons use custom_ids like inv:pdf:12, re-registered on startup."""

    def __init__(self, bot, invoice_id: int, inv: dict | None = None):
        super().__init__(timeout=None)
        self.bot, self.invoice_id = bot, invoice_id
        closed = inv is None or inv["status"] in ("paid", "void")
        voided = inv is not None and inv["status"] == "void"

        def btn(label, action, style, cb, disabled=False):
            b = discord.ui.Button(label=label, style=style, custom_id=f"inv:{action}:{invoice_id}", disabled=disabled)
            b.callback = cb
            return b

        row1 = discord.ui.ActionRow(
            btn("Download PDF", "pdf", discord.ButtonStyle.primary, self.on_pdf),
            btn("Mark Paid", "paid", discord.ButtonStyle.success, self.on_paid, disabled=inv is not None and closed),
            btn("Partial Payment", "part", discord.ButtonStyle.secondary, self.on_partial, disabled=inv is not None and closed),
        )
        row2 = discord.ui.ActionRow(
            btn("Send to Ticket", "ticket", discord.ButtonStyle.secondary, self.on_ticket, disabled=voided),
            btn("Void", "void", discord.ButtonStyle.danger, self.on_void, disabled=voided),
        )
        if inv is None:
            children = [discord.ui.TextDisplay(f"Invoice {invoice_number(invoice_id)}")]
            color = 0x5865F2
        else:
            header, items, totals = invoice_text(inv)
            children = [discord.ui.TextDisplay(header), discord.ui.Separator(),
                        discord.ui.TextDisplay(items), discord.ui.Separator(),
                        discord.ui.TextDisplay(totals)]
            color = STATUS_STYLE[inv["status"]][1]
        self.add_item(discord.ui.Container(*children, discord.ui.Separator(), row1, row2, accent_colour=color))

    # ---- callbacks ----
    async def on_pdf(self, interaction: discord.Interaction):
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv:
            return await interaction.response.send_message("Invoice not found.", ephemeral=True)
        if not (is_staff(interaction.user) or interaction.user.id == inv["client_discord_id"]):
            return await interaction.response.send_message("🚫 Only staff or the invoiced client can download this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(file=await make_pdf_file(inv), ephemeral=True)

    async def _staff_only(self, interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("🚫 Staff only.", ephemeral=True)
        return False

    async def on_paid(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv or inv["balance"] <= 0 or inv["voided"]:
            return await interaction.response.send_message("Nothing left to pay on this invoice.", ephemeral=True)
        try:
            await self.bot.db.add_payment(interaction.user.id, self.invoice_id, inv["balance"], "manual", "Marked as paid")
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await refresh_invoice(self.bot, interaction, self.invoice_id, "✅ Invoice marked as paid.")

    async def on_partial(self, interaction: discord.Interaction):
        if await self._staff_only(interaction):
            await interaction.response.send_modal(PartialPaymentModal(self.bot, self.invoice_id))

    async def on_void(self, interaction: discord.Interaction):
        if await self._staff_only(interaction):
            await interaction.response.send_modal(VoidModal(self.bot, self.invoice_id))

    async def on_ticket(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv["ticket_channel_id"]:
            return await interaction.response.send_message(
                f"No ticket linked yet. Use `/invoice send-ticket invoice_id:{inv['id']} channel:#ticket`.", ephemeral=True)
        try:
            channel = self.bot.get_channel(inv["ticket_channel_id"]) or await self.bot.fetch_channel(inv["ticket_channel_id"])
            await send_invoice_to_channel(self.bot, channel, inv)
        except discord.HTTPException:
            return await interaction.response.send_message("❌ I couldn't post in the linked ticket (deleted or no permission).", ephemeral=True)
        await interaction.response.send_message(f"📨 Sent to {channel.mention}.", ephemeral=True)


class DashboardView(discord.ui.LayoutView):
    def __init__(self, bot, report: dict, outstanding: int):
        super().__init__(timeout=900)
        self.bot, self.report, self.month = bot, report, report["month"]
        net = report["net"]
        inc = "\n".join(f"• {n}: **{fmt(a)}**" for n, a in report["income"]) or "• none"
        exp = "\n".join(f"• {n}: **{fmt(a)}**" for n, a in report["expenses"]) or "• none"
        row = discord.ui.ActionRow()
        for label, cb in (("◀ Prev", self.on_prev), ("Next ▶", self.on_next)):
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            b.callback = cb
            row.add_item(b)
        pdf = discord.ui.Button(label="P&L PDF", style=discord.ButtonStyle.primary, emoji="📄")
        pdf.callback = self.on_pdf
        row.add_item(pdf)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## 📊 Finance · {self.month}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**Revenue: {fmt(report['revenue'])}**\n{inc}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**Expenses: {fmt(report['expense_total'])}**\n{exp}"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"**{'Net profit' if net >= 0 else 'Net loss'}: {fmt(net)}**\n"
                                   f"Outstanding invoices (all time): {fmt(outstanding)}"),
            discord.ui.Separator(),
            row,
            accent_colour=0x2ECC71 if net >= 0 else 0xE74C3C,
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message("🚫 Staff only.", ephemeral=True)
        return False

    async def _go(self, interaction, delta):
        month = shift_month(self.month, delta)
        report = await self.bot.db.month_report(month)
        await interaction.response.edit_message(view=DashboardView(self.bot, report, await self.bot.db.outstanding()))

    async def on_prev(self, interaction):
        await self._go(interaction, -1)

    async def on_next(self, interaction):
        await self._go(interaction, 1)

    async def on_pdf(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await asyncio.to_thread(pnl_pdf, self.report)
        await interaction.followup.send(file=discord.File(io.BytesIO(data), filename=f"PnL-{self.month}.pdf"), ephemeral=True)
