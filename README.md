# Discord Invoice & Finance Bot

Python bot using **discord.py 2.6+ Components V2**: PDF invoices, payment tracking, ticket delivery, income/expense bookkeeping, monthly reports and a Profit & Loss statement.

## Running on a Pterodactyl panel
1. Create a server with the **Python Generic** egg (Python 3.10 or newer; pick it under *Startup*).
2. Upload everything in this folder to the server's file manager (root = `/home/container`).
3. Rename `.env.example` to `.env` and fill it in. Pterodactyl can't add custom variables without editing the egg, so the `.env` file is the simplest way.
4. In the *Startup* tab set **App py file** to `bot.py` (or leave the default `app.py`, a shim is included) and **Requirements file** to `requirements.txt`. Turn on auto-install if your egg has that option.
5. Start the server. Slash commands sync on startup; watch the console for `Logged in as ...`.

Notes for the panel:
- Everything is pure Python (PDFs use ReportLab's built-in fonts), so no system packages are needed.
- Data lives in `data/bot.db`, relative to the bot folder. Back it up from the file manager or the panel's Backups tab. Don't wipe `data/` on reinstall.
- Stopping/restarting from the panel is safe: every change is committed immediately.
- `TIMEZONE` (e.g. `Asia/Kolkata`) controls what counts as "today" for due dates and monthly reports.

## Setup (anywhere else)
1. Python 3.10+. `pip install -r requirements.txt`
2. Create an app at https://discord.com/developers/applications, add a bot, copy the token.
3. Invite it with scopes `bot` + `applications.commands` (without the second one you get `403 Missing Access`; the bot also prints a correct invite link in the console) and permissions: View Channels, Send Messages, Send Messages in Threads, Attach Files. (No privileged intents needed.)
4. `cp .env.example .env` and fill in `DISCORD_TOKEN`, `GUILD_ID`, `STAFF_ROLE_IDS`, business details and `CURRENCY`.
5. `python bot.py`

## Commands (staff only; server Administrators always allowed)
| Command | What it does |
|---|---|
| `/invoice create` | Opens an items form, one item per line: `name \| qty \| price \| spec; spec; spec` (specs become bullet points on the PDF and are optional). Options: client, client contact (phone/email), due days, tax %, discount, income category, ticket channel |
| `/invoice view`, `/invoice pdf` | Show the invoice card / download the PDF |
| `/invoice list [status] [month]` | Filter by unpaid, partial, overdue, paid, void |
| `/invoice pay` | Record a full or partial payment |
| `/invoice send-ticket` | Post card + PDF into a ticket channel/thread and link it |
| `/invoice void` | Void (kept for audit, removed from reports) |
| `/finance income`, `/finance expense` | Record transactions by category |
| `/finance category-add` | Add custom categories |
| `/finance transactions [month] [kind]` | Transaction history |
| `/finance dashboard [month]` | Revenue, expenses, net profit (with Prev/Next/PDF buttons) |
| `/finance pnl [month]` | Profit & Loss PDF |
| `/finance audit` | Audit log |

Invoice card buttons: **Download PDF** (staff or the invoiced client), **Mark Paid**, **Partial Payment**, **Send to Ticket**, **Void**.

## How it works
- Money is stored as integer cents; invoice numbers come from the database (restart-safe).
- Status is derived from payments and due date: `unpaid`, `partial`, `overdue`, `paid`, `void`. Overdue + partly paid shows both.
- Every payment against an invoice auto-creates an income transaction, so invoices and books always match.
- Nothing is hard-deleted. Voids and every create/payment/change are written to `audit_log`.
- An hourly task posts an alert in the linked ticket and `LOG_CHANNEL_ID` when an invoice becomes overdue.
- Buttons on invoice messages keep working after restarts.

## Notes
- Single currency (set `CURRENCY`). Dates use UTC.
- Back up `data/bot.db` regularly.
- Components V2 messages can't also contain plain `content`/embeds, so the PDF is posted as a separate attachment message.

## Branding the invoice
- **Logo:** replace `assets/logo.png` (a transparent PNG works best; it is trimmed and scaled automatically).
- **Company block and terms:** set `BUSINESS_NAME`, `BUSINESS_ADDRESS`, `BUSINESS_PHONE`, `BUSINESS_EMAIL`, `BUSINESS_TAX_ID` (e.g. `GSTIN: ...`), `PAYMENT_INSTRUCTIONS`, `REFUND_POLICY` and `TERMS` (points separated by `|`) in `.env`. `BRAND_COLOR` sets the accent colour.
- **Signature:** optionally add `assets/signature.png` to print a signature above 'Authorized Signatory'.
- The invoice follows a standard business layout: company details, invoice number/date/terms/status, bill-to, amount due, itemised table, totals, amount in words, payments received, notes, terms, signatory and 'Page X of Y'. It is titled TAX INVOICE when a tax rate is applied.
- **Currency:** `CURRENCY=INR` prints `₹` with Indian digit grouping (`₹1,00,000`). USD, EUR and GBP symbols work too.
- Bundled font: Noto Sans (SIL Open Font License) in `fonts/`, so the ₹ symbol renders on any server.
- Upgrading: existing `data/bot.db` files are migrated automatically on startup.
