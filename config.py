import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # panel env vars still win (override=False)


def _ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").split(",") if x.strip().isdigit()]


def _path(name: str, *candidates: str) -> str:
    """First existing candidate (or the ENV value) so old and new layouts both work."""
    raw = os.getenv(name, "").strip()
    if raw:
        p = Path(raw)
        resolved = p if p.is_absolute() else BASE_DIR / p
        if resolved.exists():
            return str(resolved)
    for candidate in candidates:
        p = BASE_DIR / candidate
        if p.exists():
            return str(p)
    return str(BASE_DIR / candidates[-1])


TOKEN = os.environ["DISCORD_TOKEN"] if "DISCORD_TOKEN" in os.environ else ""
GUILD_IDS = _ids("GUILD_ID")  # one or more server IDs, comma-separated
STAFF_ROLE_IDS = _ids("STAFF_ROLE_IDS")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0) or None

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "My Company")
BUSINESS_TAGLINE = os.getenv("BUSINESS_TAGLINE", "")
BUSINESS_ADDRESS = os.getenv("BUSINESS_ADDRESS", "")
BUSINESS_EMAIL = os.getenv("BUSINESS_EMAIL", "")
BUSINESS_PHONE = os.getenv("BUSINESS_PHONE", "")
BUSINESS_TAX_ID = os.getenv("BUSINESS_TAX_ID", "")  # e.g. "GSTIN: 32ABCDE1234F1Z5" (printed under the address)
TERMS = os.getenv("TERMS", "")  # terms & conditions, separate points with |
PAYMENT_INSTRUCTIONS = os.getenv("PAYMENT_INSTRUCTIONS", "")
REFUND_POLICY = os.getenv("REFUND_POLICY", "")
FOOTER_SERVICES = os.getenv("FOOTER_SERVICES", "")  # e.g. "web hosting, game server hosting and virtual machines"
BRAND_COLOR = os.getenv("BRAND_COLOR", "#6D28D9")  # accent colour used on PDFs
SIGNATURE_PATH = _path("SIGNATURE_PATH", "assets/signature.png", "signature.png")
LOGO_PATH = _path("LOGO_PATH", "assets/logo.png", "logo.png")

CURRENCY = os.getenv("CURRENCY", "USD")
INVOICE_PREFIX = os.getenv("INVOICE_PREFIX", "INV")
TIMEZONE = os.getenv("TIMEZONE", "UTC")

_db = Path(os.getenv("DB_PATH", "data/bot.db"))
DB_PATH = str(_db if _db.is_absolute() else BASE_DIR / _db)

# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------
# Where new ticket channels are created. Leave empty to use the server root.
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID") or 0) or None
# Where closed tickets are moved (optional). Permissions are kept read-only.
TICKET_CLOSED_CATEGORY_ID = int(os.getenv("TICKET_CLOSED_CATEGORY_ID") or 0) or None
# Staff who can see every ticket. Falls back to STAFF_ROLE_IDS.
TICKET_STAFF_ROLE_IDS = _ids("TICKET_STAFF_ROLE_IDS") or STAFF_ROLE_IDS
# Channel that receives transcripts, stale-ticket digests and close/reopen logs.
TICKET_LOG_CHANNEL_ID = int(os.getenv("TICKET_LOG_CHANNEL_ID") or 0) or LOG_CHANNEL_ID
TICKET_PREFIX = os.getenv("TICKET_PREFIX", "ticket")
TICKET_CLOSED_PREFIX = os.getenv("TICKET_CLOSED_PREFIX", "closed")
# Tickets with no activity for this many days show up in /ticket dashboard.
TICKET_STALE_DAYS = int(os.getenv("TICKET_STALE_DAYS") or 3)
# 0 = no limit. Guards against one user flooding the server with tickets.
TICKET_MAX_OPEN_PER_USER = int(os.getenv("TICKET_MAX_OPEN_PER_USER") or 3)
_ticket_dir = Path(os.getenv("TICKET_TRANSCRIPT_DIR", "data/transcripts"))
TICKET_TRANSCRIPT_DIR = str(_ticket_dir if _ticket_dir.is_absolute() else BASE_DIR / _ticket_dir)

# ---------------------------------------------------------------------------
# Sales pipeline (buying-enquiry follow-ups)
# ---------------------------------------------------------------------------
# Channel that receives follow-up reminders. Falls back to ticket log / log channel.
SALES_CHANNEL_ID = int(os.getenv("SALES_CHANNEL_ID") or 0) or TICKET_LOG_CHANNEL_ID or LOG_CHANNEL_ID
FOLLOWUP_CHECK_MINUTES = int(os.getenv("FOLLOWUP_CHECK_MINUTES") or 30)
# How many days ahead /sales pipeline looks when counting upcoming follow-ups.
SALES_LOOKAHEAD_DAYS = int(os.getenv("SALES_LOOKAHEAD_DAYS") or 7)

# ---------------------------------------------------------------------------
# Transaction details feed (the "Payment Received-35" style posts)
# ---------------------------------------------------------------------------
# One or more channel IDs, comma-separated. Falls back to LOG_CHANNEL_ID.
TRANSACTION_CHANNEL_IDS = _ids("TRANSACTION_CHANNEL_IDS") or ([LOG_CHANNEL_ID] if LOG_CHANNEL_ID else [])
# Include the "Gross Sales" running total on transaction posts.
SHOW_GROSS_SALES = os.getenv("SHOW_GROSS_SALES", "true").strip().lower() not in ("0", "false", "no", "off")
