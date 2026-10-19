"""ZarBunex-style invoice, ticket and sales bot.

Start with `python bot.py` (or app.py on panels whose default file is app.py).
"""
import sys

if sys.version_info < (3, 10):
    raise SystemExit(
        f"Python 3.10+ is required (this server runs {sys.version.split()[0]}). "
        "In the Pterodactyl panel open Startup and pick a newer Python version/image."
    )

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database
from views import InvoiceView, post_transaction_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("invoice-bot")


class _NoVoiceNoise(logging.Filter):
    def filter(self, record):
        return "voice will NOT be supported" not in record.getMessage()


logging.getLogger("discord.client").addFilter(_NoVoiceNoise())

# View Channels + Send Messages + Send Messages in Threads + Attach Files + Embed Links
# + Manage Channels (tickets) + Manage Messages (ticket transcripts)
INVITE_PERMISSIONS = 1024 + 2048 + 274877906944 + 32768 + 16384 + 65536 + 8192 + 64 + 16

EXTENSIONS = ("invoices", "finance", "tickets", "sales")


class InvoiceBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Needed so ticket transcripts contain message text. Enable it in the
        # Developer Portal (Bot -> Privileged Gateway Intents -> Message Content).
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())
        self.db = Database(config.DB_PATH)

    async def setup_hook(self):
        await self.db.connect()
        self.db.subscribe(self._on_database_event)

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

        # Re-attach buttons on old invoice messages so they keep working after a restart.
        for row in await self.db.fetchall("SELECT id FROM invoices"):
            self.add_view(InvoiceView(self, row["id"]))

        await self.sync_commands()

    async def _on_database_event(self, event: str, payload: dict):
        """Money events are posted to the finance/transaction channel(s)."""
        await post_transaction_event(self, event, payload)

    def invite_url(self) -> str:
        return (f"https://discord.com/oauth2/authorize?client_id={self.application_id or '{CLIENT_ID}'}"
                f"&scope=bot%20applications.commands&permissions={INVITE_PERMISSIONS}")

    async def sync_commands(self):
        """Never crash on a sync problem: try each configured server, then fall back to global."""
        synced = False
        for gid in config.GUILD_IDS:
            guild = discord.Object(gid)
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                synced = True
                log.info("Slash commands synced to server %s", gid)
            except discord.Forbidden:
                log.error(
                    "Missing Access for server %s. Fix: (1) check GUILD_ID is the right server ID, "
                    "(2) make sure the bot is IN that server, (3) re-invite it with the "
                    "'applications.commands' scope using this link: %s", gid, self.invite_url())
            except discord.HTTPException as e:
                log.error("Could not sync commands to server %s: %s", gid, e)
        if synced:
            # Remove leftover GLOBAL copies (e.g. from an earlier global sync) so commands don't appear twice.
            try:
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                log.info("Cleared old global commands (prevents duplicate commands in the slash menu).")
            except discord.HTTPException as e:
                log.error("Could not clear old global commands: %s", e)
        else:
            try:
                await self.tree.sync()
                log.warning("Synced commands globally instead (can take a few minutes to appear).")
            except discord.HTTPException as e:
                log.error("Global command sync failed: %s", e)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        missing = [g for g in config.GUILD_IDS if self.get_guild(g) is None]
        if missing:
            log.warning("The bot is not in server(s) %s. Invite it with: %s", missing, self.invite_url())
        if not config.TRANSACTION_CHANNEL_IDS:
            log.warning("TRANSACTION_CHANNEL_IDS / LOG_CHANNEL_ID is not set: transaction details will not be posted.")
        if not config.TICKET_STAFF_ROLE_IDS:
            log.warning("TICKET_STAFF_ROLE_IDS and STAFF_ROLE_IDS are empty: only administrators can manage tickets.")
        if not config.SALES_CHANNEL_ID:
            log.warning("SALES_CHANNEL_ID is not set: follow-up reminders cannot be posted.")

    async def close(self):
        await self.db.close()
        await super().close()


bot = InvoiceBot()


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # already answered by the staff check
    log.exception("Command error", exc_info=error)
    msg = "⚠️ Something went wrong. Check the bot logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


def main():
    if not config.TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in the .env file next to bot.py (see .env.example).")
    try:
        bot.run(config.TOKEN, log_handler=None)  # we configured logging above; output goes to the panel console
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "MESSAGE CONTENT INTENT is disabled. Open https://discord.com/developers/applications, "
            "select your bot -> Bot -> Privileged Gateway Intents -> enable MESSAGE CONTENT, then restart. "
            "It is required so ticket transcripts include message text."
        )


if __name__ == "__main__":
    main()
