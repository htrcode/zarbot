# Discord Invoice, Ticket & Sales Bot

Python bot built on **discord.py 2.6+ Components V2**. It handles PDF invoices, payment tracking,
numbered client tickets, a buying-enquiry sales pipeline, digital delivery links, income/expense
bookkeeping, monthly reports and a Profit & Loss statement.

## What it does

- **Invoices** — itemised PDF invoices, partial payments, overdue tracking, voiding, ticket delivery.
- **Digital products** — VPS, game panel, Discord bot, plugin, fork and web hosting items can carry
  a panel/file link, server name, node, plan and server ID.
- **Tickets** — numbered `ticket-0057` channels, claim/close/reopen, internal notes, transcripts,
  stale-ticket digest, and a public panel customers use to open their own tickets.
- **Linked tickets** — a purchase ticket can spawn a follow-up ticket for the same client
  (renewal, delivery, upgrade), with a parent/child link and shared client details.
- **Sales pipeline** — every purchase or buying-enquiry ticket automatically creates a deal.
  Track stages, budget, quoted value, weighted forecast, notes and follow-up dates.
- **Transaction feed** — creating an invoice and recording a payment posts a
  `Payment Received-35` style receipt to your finance channel, including server/node/plan details
  and the running gross sales.
- **Finance** — income/expense categories, monthly dashboard, P&L PDF, audit log.

## Quick start

1. Python **3.10+** (`python --version`).
2. `pip install -r requirements.txt`
3. Create an application at <https://discord.com/developers/applications>, add a bot and copy its token.
4. In **Bot → Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**.
   It is required so ticket transcripts contain message text. The bot refuses to start without it.
5. Invite the bot with scopes `bot` and `applications.commands`. Permissions used:
   View Channels, Send Messages, Send Messages in Threads, Attach Files, Embed Links,
   Read Message History, Manage Channels, Manage Messages, Add Reactions.
6. `cp .env.example .env` and fill in `DISCORD_TOKEN`, `GUILD_ID`, `STAFF_ROLE_IDS`,
   ticket/sales channel IDs and your business details.
7. `python bot.py` (or `python app.py` on panels whose default file is `app.py`).

On startup the bot syncs slash commands to every server in `GUILD_ID` and prints
`Logged in as ...`. If a sync fails it prints the correct invite link.

## Running on a Pterodactyl panel

1. Create a **Python Generic** server (Python 3.10 or newer) and upload this folder to the server root.
2. Rename `.env.example` to `.env` and fill it in (Pterodactyl variables are optional; `.env` is simplest).
3. Set **App py file** to `bot.py` and **Requirements file** to `requirements.txt`.
4. Enable **MESSAGE CONTENT INTENT** in the Discord Developer Portal for this bot.
5. Start the server. Everything is pure Python; PDFs use ReportLab's bundled fonts, so no system
   packages are needed.
6. Data lives in `data/bot.db`; transcripts in `data/transcripts/`. Back up `data/` and never wipe it
   on reinstall.

## Tickets

Create the categories once (e.g. `Tickets` and `Closed Tickets`) and put their IDs in
`TICKET_CATEGORY_ID` / `TICKET_CLOSED_CATEGORY_ID`. Put your support role IDs in
`TICKET_STAFF_ROLE_IDS` so those roles can see every ticket.

Post the customer-facing panel in your help channel:

```
/ticket panel channel:#open-a-ticket
```

Customers press **Purchase**, **Buying Enquiry** or **Support**, fill in the short form and a private
channel such as `ticket-0057` opens. The client is added automatically; the rest of the server cannot
see it. Every ticket gets a persistent panel with buttons:

| Button | What it does |
|---|---|
| Claim / Unclaim | Assigns the ticket to you (or releases it) |
| Create Invoice | Opens the invoice item form prefilled with the client and ticket |
| Linked Ticket | Creates a follow-up ticket for the same client, linked to the parent |
| Add Note | Saves a staff-only note (shown on the panel) |
| Transcript | Downloads the full channel transcript |
| Sales | Opens the linked deal card (purchase/enquiry tickets only) |
| Close / Reopen | Locks the channel, files it under the closed category, saves a transcript |

Staff commands:

| Command | What it does |
|---|---|
| `/ticket open client subject [kind] [priority] [budget] [service] [details] [parent]` | Opens a ticket for a client. `parent:<ticket id>` makes it a linked follow-up. |
| `/ticket panel [channel]` | Posts the public ticket-opening panel |
| `/ticket info [ticket]` | Shows the ticket panel anywhere |
| `/ticket list [status] [kind] [client]` | Filter tickets |
| `/ticket claim [ticket]` | Claim or unclaim (defaults to the current ticket) |
| `/ticket assign staff [ticket]` | Assign the ticket to another ticket-staff member |
| `/ticket note ticket note` | Add an internal note |
| `/ticket priority priority [ticket]` | Change priority |
| `/ticket close [ticket] [reason]` | Close, lock, move and save a transcript |
| `/ticket reopen [ticket]` | Restore client access and move it back |
| `/ticket transcript [ticket]` | Download a transcript |
| `/ticket dashboard` | Open/pending/closed/unclaimed counts, sales forecast and stale tickets |

Closing works from the panel button too; it asks for a resolution reason. A daily digest lists
tickets with no activity for `TICKET_STALE_DAYS` days in `TICKET_LOG_CHANNEL_ID`.

## Sales pipeline (buying enquiries)

A **purchase** or **enquiry** ticket automatically creates a deal at stage `new`, using the budget,
service and notes from the ticket. Support tickets do not create deals.

Stages and win probabilities used for the weighted forecast:

| Stage | Probability |
|---|---|
| New enquiry | 10% |
| Contacted | 25% |
| Quoted | 50% |
| Negotiation | 75% |
| Won | 100% |
| Lost | 0% |

Automation:

- Creating an invoice in a linked ticket sets the deal to **Quoted** and stores the invoice value.
- Recording the **final payment** on that invoice marks the deal **Won**.
- Follow-up dates become reminders in `SALES_CHANNEL_ID` and in the linked ticket at most once per day.

| Command | What it does |
|---|---|
| `/sales pipeline` | Stage counts, pipeline value, weighted forecast, won/lost totals and follow-ups due |
| `/sales list [stage]` | List deals |
| `/sales deal deal_id` | Open the deal card with buttons: Advance Stage, Follow-up, Add Note, Mark Won/Lost, Create Invoice, Open Ticket |
| `/sales ticket [ticket]` | Open the deal linked to a ticket |
| `/sales add [client] [service] [budget] [ticket] [stage] [followup] [note]` | Track a deal manually |
| `/sales stage deal_id stage [detail]` | Move a deal |
| `/sales followup deal_id when [note]` | Schedule a follow-up (`tomorrow`, `+3d`, `2026-10-24`) |
| `/sales note deal_id note` | Add a note to the deal timeline |
| `/sales won deal_id [invoice_id]` | Mark won and optionally link the invoice |
| `/sales lost deal_id [reason]` | Mark lost |
| `/sales due [ahead]` | Follow-ups due today (or within `ahead` days) |

The deal card also shows a timeline of stage changes, payments, invoices and notes.

## Digital services and delivery links

Each invoice item line accepts an optional details column:

```
name | quantity | unit price | type=VPS; server=Hector-VPS-12; node=Ryzen; plan=4GB; id=HX-1122; link=https://panel.example.com/login
```

Recognised keys:

| Key | Stored as |
|---|---|
| `type`, `product` | Product type: `VPS`, `Game Panel`, `Bot`, `Plugin`, `Fork`, `Web Hosting`, `Other` (aliases like `vps`, `panel`, `bot file`, `plugin`, `fork`, `hosting` are normalised) |
| `server`, `server_name`, `service` | Server/service name |
| `node`, `location` | Node name |
| `plan`, `ram` | Plan |
| `id`, `server_id`, `sid` | Server ID |
| `link`, `panel`, `delivery`, `url`, `access` | Delivery link |

Any other `key=value` or plain token becomes a bullet point on the invoice PDF. If the optional
column is omitted, the item is a normal service line.

You can also pick a default product for the whole invoice:

```
/invoice create client_name:"Anirudh" client:@user ticket:#ticket-0057 product:VPS
```

After saving, post the links with the **Deliver Links** button on the invoice card, or:

```
/invoice deliver invoice_id:12 channel:#ticket-0057
```

This posts a **Digital delivery** card with each item's product, plan/node/server ID and link, then
marks the invoice as delivered. Links are only posted to the selected ticket/channel; they are not
printed on the PDF.

Useful item examples:

```
Ryzen 4GB VPS | 1 | 700 | type=VPS; server=Hector-VPS-12; node=Ryzen; plan=4GB; id=HX-1122; link=https://panel.example.com
Minecraft Plugin Pack | 1 | 250 | type=Plugin; link=https://cdn.example.com/plugin.zip
Discord Bot Setup | 1 | 600 | type=Bot; link=https://github.com/example/bot/releases
Fork License | 1 | 1500 | type=Fork; link=https://example.com/fork-license
```

## Transaction details feed

Set one or more channel IDs (comma-separated) in `TRANSACTION_CHANNEL_IDS`. If empty, `LOG_CHANNEL_ID`
is used. Invoice creation and every payment are posted like this:

```
**Payment Received-35**
Customer Name : Anirudh Ravichander (<@1234567890>)
Product : VPS
Server Name : Hector-VPS-12
Payment Date : 18-10-26
Node : Ryzen
Server ID : HX-1122
Plan : 4GB
Due date : 03-11-26
Amount : ₹170
Gross Sales : ₹4,498
Invoice : INV-00035
Ticket : ticket-0057
```

- `Payment Received-N` uses the payment record number, so it is unique and restart-safe.
- **Gross Sales** is the month-to-date total of recorded income (set `SHOW_GROSS_SALES=false` to hide it).
- `(Late Renewal)` is added when a payment arrives after the invoice due date.
- Invoice creation posts a `New Invoice-N` receipt with the same server details.
- Posts are sent asynchronously so a slow Discord request never delays a command.

## Invoice and finance commands

| Command | What it does |
|---|---|
| `/invoice create` | Opens the item form: client, due days, tax %, discount, category, ticket, default product |
| `/invoice view`, `/invoice pdf` | Show the invoice card / download the PDF |
| `/invoice list [status] [month]` | Filter unpaid, partial, overdue, paid, void |
| `/invoice pay` | Record a full or partial payment (auto-creates income) |
| `/invoice deliver` | Post digital delivery links |
| `/invoice send-ticket` | Post the card + PDF into a ticket and link it |
| `/invoice void` | Void an invoice (history kept, reports exclude it) |
| `/finance income`, `/finance expense` | Record transactions by category |
| `/finance category-add` | Add custom categories |
| `/finance transactions [month] [kind]` | Transaction history |
| `/finance dashboard [month]` | Revenue, expenses, net profit, P&L PDF |
| `/finance pnl [month]` | Profit & Loss PDF |
| `/finance audit` | Audit log |

Invoice card buttons: **Download PDF** (staff or the invoiced client), **Mark Paid**,
**Partial Payment**, **Send to Ticket**, **Deliver Links**, **New Ticket** (opens a fresh or linked
client ticket for that order) and **Void**.

## How it works

- Money is stored as integer cents; invoice numbers come from the database (restart-safe).
- Invoice status is derived: `unpaid`, `partial`, `overdue`, `paid`, `void`.
- Every invoice payment auto-creates an income transaction, so invoices and books never drift.
- Tickets and deals are never hard-deleted. Closes, payments, stage changes and notes go to the
  audit log; deal history is kept in `deal_events`.
- Buttons on invoice and ticket panels survive restarts: persistent views are re-registered on startup.
- Existing `data/bot.db` files are migrated automatically (new ticket/deal tables and item columns).
- Timezone (`TIMEZONE`) controls what counts as "today" for due dates, follow-ups and reports.

## Notes

- Single currency (`CURRENCY`). Dates are stored as ISO strings.
- The bot needs **Message Content** enabled; without it Discord refuses the connection.
- Keep the bot role above the roles it needs to manage, or simply grant Administrator if your staff
  roles are high in the hierarchy.
- Back up `data/bot.db` and `data/transcripts/`.

## Branding the invoice

- **Logo:** replace `assets/logo.png` (transparent PNG; the bot also finds the bundled root `logo.png`).
- **Company block and terms:** set `BUSINESS_NAME`, `BUSINESS_ADDRESS`, `BUSINESS_PHONE`,
  `BUSINESS_EMAIL`, `BUSINESS_TAX_ID`, `PAYMENT_INSTRUCTIONS`, `REFUND_POLICY` and `TERMS`
  (points separated by `|`). `BRAND_COLOR` sets the accent colour.
- **Signature:** optionally add `assets/signature.png` to print it above "Authorized Signatory".
- Bundled font: Noto Sans (SIL Open Font License), so `₹` renders on any server.
- The invoice follows a standard business layout: company details, invoice number/date/terms/status,
  bill-to, amount due, itemised table with service metadata, totals, amount in words, payments,
  notes, terms, signatory and "Page X of Y". It is titled TAX INVOICE when tax is applied.

## Tests

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

The tests use a temporary SQLite database and check item parsing, the ticket → deal → invoice →
won flow, transaction receipt formatting, digital delivery and PDF generation.
