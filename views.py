"""Discord Components V2 views (LayoutView / Container / TextDisplay / Separator / ActionRow)."""
import asyncio
import io
import logging

import discord

from config import SHOW_GROSS_SALES, TRANSACTION_CHANNEL_IDS
from database import invoice_number, primary_service, ticket_number
from pdf_gen import invoice_pdf, pnl_pdf
from utils import (STAGE_META, days_since, dmy, fmt, is_staff, is_ticket_staff,
                   shift_month, to_cents)

log = logging.getLogger("invoice-bot.views")

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
        line = f"`{it['quantity']:g}×` {_esc(it['description'][:60])} — **{fmt(it['line_total'], cur)}**"
        meta = " · ".join(x for x in (it.get("product_type"), it.get("plan"), it.get("node"),
                                       "link ready" if it.get("delivery_link") else "") if x)
        if meta:
            line += f"\n> {_esc(meta)}"
        lines.append(line)
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


# ---------------------------------------------------------------------------
# Transaction details feed — the "Payment Received-35" posts in finance channels
# ---------------------------------------------------------------------------
def _field(label: str, value) -> str:
    text = str(value).strip() if value not in (None, "") else "—"
    return f"{label} : {_esc(text[:180])}"


def _service_lines(inv: dict, item: dict) -> list[str]:
    lines = []
    if item.get("product_type"):
        lines.append(_field("Product", item["product_type"]))
    lines.append(_field("Server Name", item.get("service_name") or item.get("description")))
    if item.get("node"):
        lines.append(_field("Node", item["node"]))
    lines.append(_field("Server ID", item.get("server_id")))
    if item.get("plan"):
        lines.append(_field("Plan", item["plan"]))
    return lines


def transaction_details(inv: dict, *, payment: dict | None = None, gross: int = 0,
                       ticket_label: str | None = None) -> str:
    """Plain-text receipt matching the finance channel layout:

    Payment Received-35
    Customer Name : ...
    Server Name : ...
    Payment Date : 18-10-26 (Late Renewal)
    ...
    Amount : 170 INR
    Gross Sales : 4498 INR
    """
    item = primary_service(inv)
    cur = inv["currency"]
    if payment:
        title = f"Payment Received-{payment['id']}"
        date_label = "Payment Date"
        paid_on = payment["paid_at"]
        late = paid_on > inv["due_date"]
        date_value = f"{dmy(paid_on)}{' (Late Renewal)' if late else ''}"
        amount = payment["amount"]
        lines = _service_lines(inv, item)
        insert_at = 2 if item.get("product_type") else 1
        lines.insert(insert_at, _field(date_label, date_value))
    else:
        title = f"New Invoice-{inv['id']}"
        date_label = "Invoice Date"
        amount = inv["total"]
        lines = _service_lines(inv, item)
        insert_at = 2 if item.get("product_type") else 1
        lines.insert(insert_at, _field(date_label, dmy(inv["issue_date"])))

    client = _esc(inv["client_name"][:80])
    if inv.get("client_discord_id"):
        client += f" (<@{inv['client_discord_id']}>)"
    rows = [f"**{title}**", _field("Customer Name", client), *lines,
            _field("Due date", dmy(inv["due_date"])),
            _field("Amount", fmt(amount, cur))]
    if SHOW_GROSS_SALES:
        rows.append(_field("Gross Sales", fmt(gross, cur)))
    rows.append(_field("Invoice", inv["number"]))
    if ticket_label:
        rows.append(_field("Ticket", ticket_label))
    return "\n".join(rows)


async def post_transaction_event(bot, event: str, payload: dict):
    """Database event listener. Posts transaction details to TRANSACTION_CHANNEL_IDS."""
    if not TRANSACTION_CHANNEL_IDS or event not in ("invoice.created", "payment"):
        return
    inv = await bot.db.get_invoice(payload["invoice_id"])
    if not inv:
        return
    payment = None
    if event == "payment":
        payment = next((p for p in inv["payments"] if p["id"] == payload.get("payment_id")), None)
        if not payment:
            return
    month = (payment["paid_at"] if payment else inv["issue_date"])[:7]
    gross = await bot.db.gross_sales(month)
    ticket = None
    if inv.get("ticket_channel_id"):
        ticket = await bot.db.ticket_by_channel(inv["ticket_channel_id"])
    ticket_label = ticket_number(ticket["id"]) if ticket else None
    text = transaction_details(inv, payment=payment, gross=gross, ticket_label=ticket_label)
    for channel_id in TRANSACTION_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning("Transaction channel %s is missing; skipping post.", channel_id)
                continue
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not post transaction details to channel %s", channel_id)


# ---------------------------------------------------------------------------
# Digital delivery (panel links, bot files, plugins, VPS access)
# ---------------------------------------------------------------------------
def delivery_items(inv: dict) -> list[dict]:
    """Items that describe a digital product (or carry a delivery link)."""
    return [it for it in inv["items"]
            if it.get("delivery_link") or it.get("product_type") or it.get("service_name")]


def delivery_text(inv: dict) -> str:
    lines = []
    for it in delivery_items(inv):
        meta = " · ".join(x for x in (it.get("product_type"), it.get("plan"),
                                       f"Node: {it['node']}" if it.get("node") else "",
                                       f"ID: {it['server_id']}" if it.get("server_id") else "") if x)
        head = f"**{_esc(it['description'][:80])}**" + (f"\n{_esc(meta)}" if meta else "")
        if it.get("delivery_link"):
            # Do not markdown-escape URLs: underscores/asterisks in a link must stay intact.
            url = discord.utils.escape_mentions(it["delivery_link"][:300])
            head += f"\n🔗 {url}"
        else:
            head += "\n*No delivery link saved for this item.*"
        lines.append(head)
    return "\n\n".join(lines)


async def deliver_invoice(bot, inv: dict, channel, actor_id: int = 0) -> int:
    """Post the saved panel/file links and mark the invoice delivered."""
    items = delivery_items(inv)
    if not any(it.get("delivery_link") for it in items):
        raise ValueError("No delivery links saved. Add `link=https://...` to an invoice item first.")
    header = f"## 🔗 Digital delivery · {inv['number']}\n" \
             f"**Client:** {_esc(inv['client_name'])}  ·  **Total:** {fmt(inv['total'], inv['currency'])}"
    body = delivery_text(inv)
    if len(body) > 3000:
        body = body[:3000].rsplit("\n", 1)[0] + "\n*…see the PDF for the full item list.*"
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(header), discord.ui.Separator(),
        discord.ui.TextDisplay(body), discord.ui.Separator(),
        discord.ui.TextDisplay("_Links are shown only to people with access to this channel._"),
        accent_colour=0x2ECC71,
    ))
    await channel.send(view=view)
    await bot.db.mark_delivered(actor_id, inv["id"], f"Delivered {len(items)} item(s)")
    return len(items)


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
            btn("Deliver Links", "deliver", discord.ButtonStyle.success, self.on_deliver, disabled=voided),
            btn("New Ticket", "newticket", discord.ButtonStyle.secondary, self.on_new_ticket, disabled=voided),
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

    async def on_deliver(self, interaction: discord.Interaction):
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv:
            return await interaction.response.send_message("Invoice not found.", ephemeral=True)
        channel = None
        if inv.get("ticket_channel_id"):
            channel = self.bot.get_channel(inv["ticket_channel_id"])
        if channel is None:
            return await interaction.response.send_message(
                f"No ticket linked. Use `/invoice deliver invoice_id:{inv['id']}` and pick a channel.",
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await deliver_invoice(self.bot, inv, channel, interaction.user.id)
        except ValueError as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except discord.HTTPException:
            return await interaction.followup.send("❌ I couldn't post in the linked ticket.", ephemeral=True)
        await interaction.followup.send(f"🔗 Delivered {count} digital item(s) in {channel.mention}.", ephemeral=True)

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

    async def on_new_ticket(self, interaction: discord.Interaction):
        """Open a fresh (or linked) client ticket straight from the invoice."""
        if not await self._staff_only(interaction):
            return
        inv = await self.bot.db.get_invoice(self.invoice_id)
        if not inv or not inv.get("client_discord_id"):
            return await interaction.response.send_message(
                "This invoice has no Discord client saved. Use `/ticket open client:@user`.", ephemeral=True)
        service = getattr(self.bot, "tickets", None)
        if not service:
            return await interaction.response.send_message("Ticket system is still starting up.", ephemeral=True)
        client = interaction.guild.get_member(inv["client_discord_id"])
        if client is None:
            try:
                client = await interaction.guild.fetch_member(inv["client_discord_id"])
            except discord.HTTPException:
                client = None
        if client is None:
            return await interaction.response.send_message(
                "That client is no longer in this server. Use `/ticket open` with a current member.", ephemeral=True)
        ticket_row = None
        if inv.get("ticket_channel_id"):
            ticket_row = await self.bot.db.ticket_by_channel(inv["ticket_channel_id"])
        item = primary_service(inv)
        subject = f"Order {inv['number']} · {inv['client_name']}"[:120]
        details = (f"Invoice {inv['number']} for {fmt(inv['total'], inv['currency'])}.\n"
                   f"Product: {item.get('product_type') or item.get('description') or 'service'}")
        await service.open_ticket(
            interaction, client=client, kind="purchase", subject=subject, details=details,
            service=item.get("product_type") or "", parent_id=ticket_row["id"] if ticket_row else None,
        )


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


# ===========================================================================
# Tickets
# ===========================================================================
PRIORITY_ICON = {"urgent": "🔴", "high": "🟠", "normal": "🔵", "low": "⚪"}
STATUS_ICON = {"open": "🟢", "pending": "🟡", "closed": "⚫"}


def _who(user_id) -> str:
    return f"<@{user_id}>" if user_id else "—"


def ticket_text(ticket: dict, deal: dict | None = None, invoice: dict | None = None,
                notes: list | None = None, parent: dict | None = None) -> str:
    lines = [
        f"## 🎫 {ticket_number(ticket['id'])} · {ticket['kind'].title()} "
        f"{PRIORITY_ICON.get(ticket['priority'], '🔵')}",
        f"**Subject:** {_esc(ticket['subject'][:180])}",
        f"**Client:** {_esc(ticket['client_name'])} ({_who(ticket.get('client_discord_id'))})",
        f"**Status:** {STATUS_ICON.get(ticket['status'], '')} {ticket['status'].title()} "
        f"· **Priority:** {ticket['priority'].title()}",
        f"**Claimed by:** {_who(ticket.get('claimed_by'))}  ·  **Opened by:** {_who(ticket.get('opened_by'))}",
        f"**Opened:** {dmy((ticket.get('created_at') or '')[:10])} "
        f"· **Last activity:** {dmy((ticket.get('last_activity') or '')[:10])}",
    ]
    if parent:
        lines.append(f"**Follow-up of:** {ticket_number(parent['id'])} · {_esc(parent['subject'][:80])}")
    if deal:
        label = STAGE_META.get(deal["stage"], (deal["stage"], 0))[0]
        lines.append(f"**Sales:** {label} · budget {fmt(deal['budget'])} · value {fmt(deal['value'])}"
                     + (f" · follow-up {deal['next_follow_up']}" if deal.get("next_follow_up") else ""))
    if invoice:
        lines.append(f"**Invoice:** {invoice['number']} · {invoice['status']} · balance "
                     f"{fmt(invoice['balance'], invoice['currency'])}")
    if ticket.get("close_reason"):
        lines.append(f"**Close reason:** {_esc(ticket['close_reason'][:200])}")
    if notes:
        items = [f"• <@{n['author_id']}> {_esc(n['note'][:140])}" for n in notes[:3]]
        lines += ["", "**Internal notes**", *items]
    return _clip("\n".join(lines), 3200)


async def build_ticket_view(bot, ticket_id: int):
    ticket = await bot.db.get_ticket(ticket_id)
    if not ticket:
        return None
    deal = await bot.db.deal_by_ticket(ticket_id)
    invoice = await bot.db.get_invoice(deal["invoice_id"]) if deal and deal.get("invoice_id") else None
    notes = await bot.db.ticket_notes(ticket_id, 3)
    parent = await bot.db.ticket_parent(ticket_id)
    return TicketPanelView(bot, ticket, deal, invoice, notes, parent)


async def refresh_ticket_panel(bot, interaction: discord.Interaction, ticket_id: int):
    """Edit the message the button belongs to (works for slash replies and modals)."""
    view = await build_ticket_view(bot, ticket_id)
    if view is None:
        return
    try:
        await interaction.message.edit(view=view)
    except (discord.HTTPException, AttributeError):
        pass


class CloseTicketModal(discord.ui.Modal, title="Close ticket"):
    reason = discord.ui.TextInput(label="Reason / resolution", required=False, max_length=300,
                                  placeholder="Delivered the VPS, client confirmed everything works")

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await interaction.response.send_message("Ticket system is still starting up.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await service.close_ticket(interaction.guild, self.ticket_id, interaction.user, self.reason.value)
        except (ValueError, discord.HTTPException) as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send("⚫ Ticket closed and transcript saved.", ephemeral=True)
        await refresh_ticket_panel(interaction.client, interaction, self.ticket_id)


class TicketNoteModal(discord.ui.Modal, title="Internal note"):
    note = discord.ui.TextInput(label="Note (only staff can see this)", style=discord.TextStyle.paragraph,
                                max_length=1500)

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.client.db.add_ticket_note(interaction.user.id, self.ticket_id, self.note.value)
        await interaction.response.send_message("📝 Note saved.", ephemeral=True)
        await refresh_ticket_panel(interaction.client, interaction, self.ticket_id)


class FollowUpTicketModal(discord.ui.Modal, title="New linked ticket"):
    subject = discord.ui.TextInput(label="What is this follow-up about?", max_length=120,
                                   placeholder="Renewal / delivery / upgrade for the same client")
    details = discord.ui.TextInput(label="Handover notes", style=discord.TextStyle.paragraph,
                                   required=False, max_length=1500,
                                   placeholder="What was already discussed or purchased?")

    def __init__(self, parent_id: int):
        super().__init__()
        self.parent_id = parent_id

    async def on_submit(self, interaction: discord.Interaction):
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await interaction.response.send_message("Ticket system is still starting up.", ephemeral=True)
        await service.open_ticket(interaction, client=None, kind="purchase", subject=self.subject.value,
                                  details=self.details.value, parent_id=self.parent_id)


class TicketPanelView(discord.ui.LayoutView):
    """Persistent panel posted inside every ticket channel."""

    def __init__(self, bot, ticket: dict, deal=None, invoice=None, notes=None, parent=None):
        super().__init__(timeout=None)
        self.bot, self.ticket_id, self.ticket = bot, ticket["id"], ticket
        self.deal = deal
        closed = ticket["status"] == "closed"

        def btn(label, action, style, cb, disabled=False, emoji=None):
            b = discord.ui.Button(label=label, style=style, emoji=emoji,
                                  custom_id=f"ticket:{action}:{ticket['id']}", disabled=disabled)
            b.callback = cb
            return b

        claim_label = "Unclaim" if ticket.get("claimed_by") else "Claim"
        row1 = discord.ui.ActionRow(
            btn(claim_label, "claim", discord.ButtonStyle.secondary, self.on_claim, disabled=closed, emoji="🙋"),
            btn("Create Invoice", "invoice", discord.ButtonStyle.primary, self.on_invoice, emoji="🧾"),
            btn("Linked Ticket", "follow", discord.ButtonStyle.secondary, self.on_follow,
                disabled=closed, emoji="➕"),
        )
        close_btn = (btn("Reopen", "reopen", discord.ButtonStyle.success, self.on_reopen, emoji="🔓")
                     if closed else
                     btn("Close", "close", discord.ButtonStyle.danger, self.on_close, emoji="🔒"))
        row2 = discord.ui.ActionRow(
            btn("Add Note", "note", discord.ButtonStyle.secondary, self.on_note, emoji="📝"),
            btn("Transcript", "transcript", discord.ButtonStyle.secondary, self.on_transcript, emoji="📄"),
            btn("Sales", "sales", discord.ButtonStyle.secondary, self.on_sales,
                disabled=deal is None, emoji="📈"),
            close_btn,
        )
        color = 0x95A5A6 if closed else {
            "urgent": 0xE74C3C, "high": 0xE67E22, "normal": 0x3498DB, "low": 0x95A5A6,
        }.get(ticket["priority"], 0x5865F2)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(ticket_text(ticket, deal, invoice, notes, parent)),
            discord.ui.Separator(),
            row1,
            row2,
            accent_colour=color,
        ))

    async def _staff(self, interaction) -> bool:
        if is_ticket_staff(interaction.user):
            return True
        await interaction.response.send_message("🚫 Ticket staff only.", ephemeral=True)
        return False

    async def on_claim(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        ticket = await self.bot.db.get_ticket(self.ticket_id)
        if not ticket or ticket["status"] == "closed":
            return await interaction.response.send_message("This ticket is closed.", ephemeral=True)
        claimed = None if ticket.get("claimed_by") == interaction.user.id else interaction.user.id
        await self.bot.db.set_ticket_claimed(interaction.user.id, self.ticket_id, claimed)
        view = await build_ticket_view(self.bot, self.ticket_id)
        await interaction.response.edit_message(view=view)

    async def on_invoice(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "invoices", None)
        if not service:
            return await interaction.response.send_message("Invoice system is still starting up.", ephemeral=True)
        await service.open_draft_for_ticket(interaction, self.ticket)

    async def on_follow(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        await interaction.response.send_modal(FollowUpTicketModal(self.ticket_id))

    async def on_note(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        await interaction.response.send_modal(TicketNoteModal(self.ticket_id))

    async def on_sales(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        deal = await self.bot.db.deal_by_ticket(self.ticket_id)
        if not deal:
            return await interaction.response.send_message("This ticket has no sales deal.", ephemeral=True)
        view = await build_deal_view(self.bot, deal["id"])
        await interaction.response.send_message(view=view, ephemeral=True)

    async def on_close(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        await interaction.response.send_modal(CloseTicketModal(self.ticket_id))

    async def on_reopen(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "tickets", None)
        if not service:
            return await interaction.response.send_message("Ticket system is still starting up.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await service.reopen_ticket(interaction.guild, self.ticket_id)
        except (ValueError, discord.HTTPException) as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        view = await build_ticket_view(self.bot, self.ticket_id)
        try:
            await interaction.message.edit(view=view)
        except (discord.HTTPException, AttributeError):
            pass
        await interaction.followup.send("🔓 Ticket reopened.", ephemeral=True)

    async def on_transcript(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "tickets", None)
        if not service:
            return await interaction.response.send_message("Ticket system is still starting up.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            path = await service.save_transcript(self.ticket_id, interaction.guild)
        except (ValueError, discord.HTTPException, OSError) as e:
            return await interaction.followup.send(f"❌ {e}", ephemeral=True)
        await interaction.followup.send(file=discord.File(path), ephemeral=True)


class OpenTicketModal(discord.ui.Modal):
    subject = discord.ui.TextInput(label="Subject", max_length=120,
                                   placeholder="4GB VPS in India / bot setup / plugin help")
    details = discord.ui.TextInput(label="Details", style=discord.TextStyle.paragraph, required=False,
                                   max_length=1500, placeholder="Budget, location, plan, deadline...")
    service = discord.ui.TextInput(label="Service you want", required=False, max_length=100,
                                   placeholder="VPS, game panel, Discord bot, plugin, fork...")
    budget = discord.ui.TextInput(label="Budget (optional)", required=False, max_length=20,
                                  placeholder="500")

    def __init__(self, kind: str):
        super().__init__(title=f"Open a {kind} ticket"[:45])
        self.kind = kind

    async def on_submit(self, interaction: discord.Interaction):
        service = getattr(interaction.client, "tickets", None)
        if not service:
            return await interaction.response.send_message("Ticket system is still starting up.", ephemeral=True)
        budget = 0
        if self.budget.value.strip():
            try:
                budget = to_cents(self.budget.value)
            except ValueError as e:
                return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await service.open_ticket(interaction, client=interaction.user, kind=self.kind,
                                  subject=self.subject.value, details=self.details.value,
                                  service=self.service.value, budget=budget)


class TicketOpenPanelView(discord.ui.LayoutView):
    """Public panel customers use to open their own tickets."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

        def btn(label, kind, style, emoji):
            b = discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"ticketopen:{kind}")
            b.callback = getattr(self, f"on_{kind}")
            return b

        row = discord.ui.ActionRow(
            btn("Purchase", "purchase", discord.ButtonStyle.success, "🛒"),
            btn("Buying Enquiry", "enquiry", discord.ButtonStyle.primary, "💬"),
            btn("Support", "support", discord.ButtonStyle.secondary, "🛠️"),
        )
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## 🎫 Need help or want to buy?\n"
                                   "Open a ticket and our team will pick it up. "
                                   "Buying enquiries are tracked by our sales team so nothing gets lost."),
            discord.ui.Separator(),
            discord.ui.TextDisplay("🛒 **Purchase** — buy a VPS, panel, bot, plugin or fork\n"
                                   "💬 **Buying Enquiry** — ask about pricing, specs or availability\n"
                                   "🛠️ **Support** — help with an existing service"),
            discord.ui.Separator(),
            row,
            accent_colour=0x5865F2,
        ))

    async def on_purchase(self, interaction):
        await interaction.response.send_modal(OpenTicketModal("purchase"))

    async def on_enquiry(self, interaction):
        await interaction.response.send_modal(OpenTicketModal("enquiry"))

    async def on_support(self, interaction):
        await interaction.response.send_modal(OpenTicketModal("support"))


# ===========================================================================
# Sales pipeline
# ===========================================================================
def _followup_label(value: str | None) -> str:
    if not value:
        return "not set"
    age = days_since(value)
    if age is None:
        return value
    if age > 0:
        return f"{value} · overdue {age}d"
    if age == 0:
        return f"{value} · today"
    return f"{value} · in {-age}d"


def deal_text(deal: dict, invoice: dict | None = None, timeline: list | None = None) -> str:
    label, probability = STAGE_META.get(deal["stage"], (deal["stage"], 0))
    ticket = (f"<#{deal['ticket_channel_id']}> · {ticket_number(deal['ticket_id'])}"
              if deal.get("ticket_channel_id") else (ticket_number(deal["ticket_id"]) if deal.get("ticket_id") else "—"))
    lines = [
        f"## 🤝 Deal #{deal['id']} · {label}",
        f"**Client:** {_esc(deal['client_name'])} ({_who(deal.get('client_discord_id'))})",
        f"**Service:** {_esc(deal.get('service') or '—')}",
        f"**Budget:** {fmt(deal['budget'])}  ·  **Quoted value:** {fmt(deal['value'])}"
        f"  ·  **Win chance:** {int(probability * 100)}%",
        f"**Ticket:** {ticket}  ·  **Next follow-up:** {_followup_label(deal.get('next_follow_up'))}",
    ]
    if deal.get("follow_up_note"):
        lines.append(f"**Follow-up note:** {_esc(deal['follow_up_note'][:200])}")
    if invoice:
        lines.append(f"**Invoice:** {invoice['number']} · {invoice['status']} · "
                     f"balance {fmt(invoice['balance'], invoice['currency'])}")
    else:
        lines.append("**Invoice:** not created yet")
    if deal.get("notes"):
        lines += ["", "**Notes**", _esc(deal["notes"][:400])]
    if timeline:
        lines += ["", "**Timeline**"]
        for event in timeline[:6]:
            lines.append(f"• {event['created_at'][:16]} · <@{event['actor_id']}> · {_esc(event['detail'] or event['kind'])}")
    return _clip("\n".join(lines), 3200)


async def build_deal_view(bot, deal_id: int):
    deal = await bot.db.get_deal(deal_id)
    if not deal:
        return None
    invoice = await bot.db.get_invoice(deal["invoice_id"]) if deal.get("invoice_id") else None
    timeline = await bot.db.deal_timeline(deal_id, 6)
    return DealView(bot, deal, invoice, timeline)


async def refresh_deal_card(bot, interaction, deal_id: int):
    view = await build_deal_view(bot, deal_id)
    if view is None:
        return
    try:
        await interaction.message.edit(view=view)
    except (discord.HTTPException, AttributeError):
        pass


class DealFollowupModal(discord.ui.Modal, title="Schedule follow-up"):
    when = discord.ui.TextInput(label="When", max_length=20, placeholder="tomorrow / +3d / 2026-10-24")
    note = discord.ui.TextInput(label="What should happen?", required=False, max_length=300,
                                style=discord.TextStyle.paragraph, placeholder="Send revised quote for 8GB plan")

    def __init__(self, deal_id: int):
        super().__init__()
        self.deal_id = deal_id

    async def on_submit(self, interaction: discord.Interaction):
        from utils import parse_when
        try:
            when = parse_when(self.when.value)
            await interaction.client.db.set_deal_followup(interaction.user.id, self.deal_id, when, self.note.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message(f"📅 Follow-up set for {when}.", ephemeral=True)
        await refresh_deal_card(interaction.client, interaction, self.deal_id)


class DealNoteModal(discord.ui.Modal, title="Deal note"):
    note = discord.ui.TextInput(label="Note", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, deal_id: int):
        super().__init__()
        self.deal_id = deal_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.client.db.add_deal_note(interaction.user.id, self.deal_id, self.note.value)
        await interaction.response.send_message("📝 Deal note saved.", ephemeral=True)
        await refresh_deal_card(interaction.client, interaction, self.deal_id)


class DealLostModal(discord.ui.Modal, title="Mark deal lost"):
    reason = discord.ui.TextInput(label="Why was it lost?", max_length=300,
                                  placeholder="Budget too low / went with another host")

    def __init__(self, deal_id: int):
        super().__init__()
        self.deal_id = deal_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.client.db.set_deal_stage(interaction.user.id, self.deal_id, "lost", self.reason.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message("❌ Deal marked lost.", ephemeral=True)
        await refresh_deal_card(interaction.client, interaction, self.deal_id)


class DealView(discord.ui.LayoutView):
    """Stage controls for one buying-enquiry deal."""

    def __init__(self, bot, deal: dict, invoice=None, timeline=None):
        super().__init__(timeout=900)
        self.bot, self.deal_id, self.deal = bot, deal["id"], deal
        closed = deal["stage"] in ("won", "lost")

        def btn(label, action, style, cb, disabled=False, emoji=None):
            b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled,
                                  custom_id=f"deal:{action}:{deal['id']}")
            b.callback = cb
            return b

        row1 = discord.ui.ActionRow(
            btn("Advance Stage", "advance", discord.ButtonStyle.primary, self.on_advance,
                disabled=closed or deal["stage"] == "negotiation", emoji="⏭️"),
            btn("Follow-up", "followup", discord.ButtonStyle.secondary, self.on_followup,
                disabled=closed, emoji="📅"),
            btn("Add Note", "note", discord.ButtonStyle.secondary, self.on_note, emoji="📝"),
        )
        row2 = discord.ui.ActionRow(
            btn("Mark Won", "won", discord.ButtonStyle.success, self.on_won, disabled=closed, emoji="✅"),
            btn("Mark Lost", "lost", discord.ButtonStyle.danger, self.on_lost, disabled=closed, emoji="❌"),
            btn("Create Invoice", "invoice", discord.ButtonStyle.secondary, self.on_invoice, emoji="🧾"),
            btn("Open Ticket", "open", discord.ButtonStyle.secondary, self.on_open,
                disabled=not deal.get("ticket_channel_id"), emoji="🎫"),
        )
        color = {"won": 0x2ECC71, "lost": 0xE74C3C}.get(deal["stage"], 0xF1C40F)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(deal_text(deal, invoice, timeline)),
            discord.ui.Separator(),
            row1,
            row2,
            accent_colour=color,
        ))

    async def _staff(self, interaction) -> bool:
        if is_ticket_staff(interaction.user):
            return True
        await interaction.response.send_message("🚫 Sales staff only.", ephemeral=True)
        return False

    async def on_advance(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        try:
            await self.bot.db.advance_deal(interaction.user.id, self.deal_id)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        view = await build_deal_view(self.bot, self.deal_id)
        await interaction.response.edit_message(view=view)

    async def on_followup(self, interaction: discord.Interaction):
        if await self._staff(interaction):
            await interaction.response.send_modal(DealFollowupModal(self.deal_id))

    async def on_note(self, interaction: discord.Interaction):
        if await self._staff(interaction):
            await interaction.response.send_modal(DealNoteModal(self.deal_id))

    async def on_won(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        try:
            await self.bot.db.set_deal_stage(interaction.user.id, self.deal_id, "won", "Marked won")
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        view = await build_deal_view(self.bot, self.deal_id)
        await interaction.response.edit_message(view=view)

    async def on_lost(self, interaction: discord.Interaction):
        if await self._staff(interaction):
            await interaction.response.send_modal(DealLostModal(self.deal_id))

    async def on_invoice(self, interaction: discord.Interaction):
        if not await self._staff(interaction):
            return
        service = getattr(self.bot, "invoices", None)
        if not service:
            return await interaction.response.send_message("Invoice system is still starting up.", ephemeral=True)
        await service.open_draft_for_deal(interaction, self.deal)

    async def on_open(self, interaction: discord.Interaction):
        channel_id = self.deal.get("ticket_channel_id")
        if not channel_id:
            return await interaction.response.send_message("This deal is not linked to a ticket.", ephemeral=True)
        await interaction.response.send_message(f"🎫 Ticket: <#{channel_id}>", ephemeral=True)
