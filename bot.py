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
from views import InvoiceView

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("invoice-bot")


class _NoVoiceNoise(logging.Filter):
    def filter(self, record):
        return "voice will NOT be supported" not in record.getMessage()


logging.getLogger("discord.client").addFilter(_NoVoiceNoise())

# View Channels + Send Messages + Send Messages in Threads + Attach Files
INVITE_PERMISSIONS = 1024 + 2048 + 274877906944 + 32768


class InvoiceBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.db = Database(config.DB_PATH)

    async def setup_hook(self):
        await self.db.connect()
        await self.load_extension("cogs.invoices")
        await self.load_extension("cogs.finance")
        # Re-attach buttons on old invoice messages so they keep working after a restart
        for row in await self.db.fetchall("SELECT id FROM invoices WHERE voided=0"):
            self.add_view(InvoiceView(self, row["id"]))
        await self.sync_commands()

    def invite_url(self) -> str:
        return (f"https://discord.com/oauth2/authorize?client_id={self.application_id}"
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
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def main():
    if not config.TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in the .env file next to bot.py (see .env.example).")
    bot.run(config.TOKEN, log_handler=None)  # we configured logging above; output goes to the panel console


if __name__ == "__main__":
    main()
