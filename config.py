import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # panel env vars still win (override=False)


def _ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").split(",") if x.strip().isdigit()]


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
_sig = Path(os.getenv("SIGNATURE_PATH", "assets/signature.png"))  # optional signature image
SIGNATURE_PATH = str(_sig if _sig.is_absolute() else BASE_DIR / _sig)
_logo = Path(os.getenv("LOGO_PATH", "assets/logo.png"))
LOGO_PATH = str(_logo if _logo.is_absolute() else BASE_DIR / _logo)

CURRENCY = os.getenv("CURRENCY", "USD")
INVOICE_PREFIX = os.getenv("INVOICE_PREFIX", "INV")
TIMEZONE = os.getenv("TIMEZONE", "UTC")

_db = Path(os.getenv("DB_PATH", "data/bot.db"))
DB_PATH = str(_db if _db.is_absolute() else BASE_DIR / _db)
