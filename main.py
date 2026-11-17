"""B_Media Community Bot - simple Discord bot for Pella hosting."""

import os
import logging

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env when running locally; harmless on Pella
except ImportError:
    pass

BOT_NAME = "B_Media Community Bot"
TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(BOT_NAME)

intents = discord.Intents.default()
intents.message_content = True  # needed for prefix commands (privileged)
intents.members = True          # needed for /whohas member lookups (privileged)


class CommunityBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)

    async def setup_hook(self):
        await self.load_extension("cogs.setups")
        await self.load_extension("cogs.moderation")
        await self.load_extension("cogs.iemranking")
        synced = await self.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))


bot = CommunityBot()


# ---------- Events ----------

@bot.event
async def on_ready():
    log.info("%s online as %s (ID: %s)", BOT_NAME, bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Game(name=f"{PREFIX}help | /help"))


# ---------- Shared embed builders ----------

def help_embed() -> discord.Embed:
    e = discord.Embed(title=f"{BOT_NAME} - Commands", color=discord.Color.blurple())
    e.add_field(name="ping", value="Check bot latency", inline=False)
    e.add_field(name="hello", value="Say hello", inline=False)
    e.add_field(name="serverinfo", value="Info about this server", inline=False)
    e.add_field(name="userinfo [member]", value="Info about a member", inline=False)
    e.add_field(
        name="🎧 Setup profiles",
        value=(
            "`/setup edit` — add/edit your gear, IEMs, speakers & extras\n"
            "`/setup view [member]` — show a setup\n"
            "`/setup clear` — delete your setup\n"
            "Right-click a user → Apps → **View Setup**"
        ),
        inline=False,
    )
    e.add_field(name="🔍 whohas <gear>", value="Find members who own a piece of gear", inline=False)
    e.add_field(
        name="📈 IEMRanking",
        value=(
            "`/iem <name>` — IEM/headphone profile: scores, rank, price, photo\n"
            "`/iemtop [sort] [type] [price] [tuning] [brand]` — top lists (best bass, value…)\n"
            "`/iemcompare <first> <second>` — head-to-head"
        ),
        inline=False,
    )
    e.add_field(name="🐢 slowmode <seconds> [channel]", value="*Mods:* custom slowmode, any seconds (e.g. 3). 0 = off", inline=False)
    e.set_footer(text=f"Use /command or {PREFIX}command")
    return e


def serverinfo_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(title=guild.name, color=discord.Color.green())
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.add_field(name="Owner", value=str(guild.owner) if guild.owner else "Unknown")
    e.add_field(name="Members", value=str(guild.member_count))
    e.add_field(name="Channels", value=str(len(guild.channels)))
    e.add_field(name="Roles", value=str(len(guild.roles)))
    e.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "D"))
    return e


def userinfo_embed(member: discord.Member) -> discord.Embed:
    e = discord.Embed(title=str(member), color=member.color)
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="ID", value=str(member.id))
    e.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "Unknown")
    e.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "D"))
    roles = [r.mention for r in member.roles if r != member.guild.default_role]
    e.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles) or "None", inline=False)
    return e


# ---------- Slash commands ----------

@bot.tree.command(name="help", description="Show bot commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=help_embed(), ephemeral=True)


@bot.tree.command(name="ping", description="Check bot latency")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)} ms")


@bot.tree.command(name="hello", description="Say hello")
async def slash_hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}! 👋")


@bot.tree.command(name="serverinfo", description="Info about this server")
@app_commands.guild_only()
async def slash_serverinfo(interaction: discord.Interaction):
    await interaction.response.send_message(embed=serverinfo_embed(interaction.guild))


@bot.tree.command(name="userinfo", description="Info about a member")
@app_commands.guild_only()
@app_commands.describe(member="Member to look up (defaults to you)")
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member | None = None):
    await interaction.response.send_message(embed=userinfo_embed(member or interaction.user))


# ---------- Prefix commands ----------

@bot.command(name="help")
async def prefix_help(ctx: commands.Context):
    await ctx.send(embed=help_embed())


@bot.command(name="ping")
async def prefix_ping(ctx: commands.Context):
    await ctx.send(f"Pong! {round(bot.latency * 1000)} ms")


@bot.command(name="hello")
async def prefix_hello(ctx: commands.Context):
    await ctx.send(f"Hello {ctx.author.mention}! 👋")


@bot.command(name="serverinfo")
@commands.guild_only()
async def prefix_serverinfo(ctx: commands.Context):
    await ctx.send(embed=serverinfo_embed(ctx.guild))


@bot.command(name="userinfo")
@commands.guild_only()
async def prefix_userinfo(ctx: commands.Context, member: discord.Member = None):
    await ctx.send(embed=userinfo_embed(member or ctx.author))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing `{error.param.name}`. Usage: `{PREFIX}{ctx.command.qualified_name} {ctx.command.signature}`")
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("That command only works in a server.")
        return
    log.exception("Command error", exc_info=error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set. Add it as an environment variable (or in a .env file).")
    bot.run(TOKEN, log_handler=None)
