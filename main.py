"""B_Media Community Bot - simple Discord bot for Pella hosting."""

import os
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from cogs.style import BAD, GOLD, OK, WARN, brand_footer, rel, set_icon

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
# No privileged intents (avoids Discord's intent review):
# - no message_content: prefix commands only work via @mention (and in DMs)
# - no members: /whohas shows raw mentions instead of using the member cache


class CommunityBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned_or(PREFIX), intents=intents, help_command=None)

    async def setup_hook(self):
        await self.load_extension("cogs.setups")
        await self.load_extension("cogs.moderation")
        await self.load_extension("cogs.iemranking")
        await self.load_extension("cogs.games")
        synced = await self.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))


bot = CommunityBot()


# ---------- Events ----------

@bot.event
async def on_ready():
    log.info("%s online as %s (ID: %s)", BOT_NAME, bot.user, bot.user.id)
    set_icon(bot.user.display_avatar.url)
    await bot.change_presence(activity=discord.Game(name="/help"))


# ---------- Help ----------

# (key, emoji, label, tagline, [(usage, description)], mods only)
HELP = [
    ("general", "ℹ️", "General", "Basics and server info", [
        ("/help", "This menu"),
        ("/ping", "Bot latency"),
        ("/hello", "Say hello"),
        ("/serverinfo", "Stats about this server"),
        ("/userinfo [member]", "Info about a member, with a link to their setup"),
    ], False),
    ("setups", "🎧", "Setup profiles", "Share your gear and find owners", [
        ("/setup edit", "Add or edit your source, DAC, amp, headphones, IEMs & extras"),
        ("/setup view [member]", "Show a setup in the channel"),
        ("/setup clear", "Delete your setup"),
        ("/whohas <gear>", "Find members who own something (fuzzy: `hd800s` = `HD 800 S`)"),
        ("Right-click → Apps → View Setup", "Peek at anyone's setup privately"),
    ], False),
    ("iemranking", "📈", "IEMRanking", "Scores, rankings & recommendations", [
        ("/search <name>", "Product profile: reviewer & user score, rank, price, photo"),
        ("/top [sort] [type] [price] [tuning] [brand]", "Ranking lists: best bass, best value…"),
        ("/compare <first> <second>", "Head to head, winner per metric"),
        ("/recommend", "Answer 4 questions, get your top 5 picks"),
        ("/upgrade [current] [budget]", "Better-scoring options near what you own"),
    ], False),
    ("games", "🎮", "Games", "Daily puzzle, brackets & leaderboard", [
        ("/iemdle play", "Today's guess-the-IEM puzzle and rules"),
        ("/iemdle guess <iem>", "Make a guess (6 tries, hints after each)"),
        ("/iemdle stats", "Your streak and guess distribution"),
        ("/iemcup status", "Current IEM Cup bracket"),
        ("/leaderboard [game]", "Top players in this server"),
    ], False),
    ("moderation", "🛡️", "Moderation", "Staff tools, every action saved as a case", [
        ("/warn <member> <reason>", "Warn and DM them"),
        ("/kick <member> [reason]", "Remove from the server"),
        ("/ban <user> [reason] [delete_messages]", "Ban, works by user ID too"),
        ("/softban <user> [reason]", "Ban + unban to wipe recent messages"),
        ("/unban <user> [reason]", "Lift a ban"),
        ("/mute <member> <duration> [reason]", "Timeout, e.g. `10m`, `2d` (max 28d)"),
        ("/unmute <member> [reason]", "End a timeout"),
        ("/cases <user>", "Someone's moderation history"),
        ("/case view|reason|delete <number>", "Look up or edit a case"),
        ("/slowmode <seconds> [channel]", "Custom slowmode, any seconds"),
        ("/setup remove <user>", "Delete someone's setup"),
        ("/iemcup start|cancel", "Run an IEM Cup (top of a list, random draw or hand-picked)"),
        ("/modconfig", "*Admins:* log channel, mod roles, setup check"),
        ("/gamesconfig channel|view|off", "*Admins:* game channel (bot-run, one thread per game)"),
    ], True),
]
HELP_INFO = {h[0]: h for h in HELP}


def viewer_is_mod(user: discord.abc.User) -> bool:
    mod = bot.get_cog("Moderation")
    return isinstance(user, discord.Member) and mod is not None and mod.is_mod(user)


def help_overview(is_mod: bool) -> discord.Embed:
    e = discord.Embed(
        title=f"🎧 {BOT_NAME}",
        description="Your audio community helper: share setups, look up gear, play games.\n"
                    "Pick a category below to see its commands.",
        color=GOLD,
    )
    for key, emoji, label, tagline, cmds, mod_only in HELP:
        if mod_only and not is_mod:
            continue
        e.add_field(name=f"{emoji} {label}", value=f"{tagline}\n-# {len(cmds)} commands", inline=True)
    if bot.user:
        e.set_thumbnail(url=bot.user.display_avatar.url)
    return brand_footer(e, "Tip: @mention me + a command works too, e.g. @bot ping")


def help_page(key: str) -> discord.Embed:
    _, emoji, label, tagline, cmds, _ = HELP_INFO[key]
    e = discord.Embed(title=f"{emoji} {label}", description=f"*{tagline}*\n\n" + "\n".join(
        f"`{usage}`\n-# {desc}" for usage, desc in cmds
    ), color=GOLD)
    return brand_footer(e, "<required> [optional]")


class HelpView(discord.ui.View):
    def __init__(self, owner_id: int, is_mod: bool):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        options = [discord.SelectOption(label="Overview", value="overview", emoji="🏠")] + [
            discord.SelectOption(label=label, value=key, emoji=emoji, description=tagline)
            for key, emoji, label, tagline, _, mod_only in HELP if is_mod or not mod_only
        ]
        select = discord.ui.Select(placeholder="Pick a category…", options=options)
        select.callback = self.on_select
        self.select, self.is_mod = select, is_mod
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Open your own menu with `/help`.", ephemeral=True)
            return
        key = self.select.values[0]
        for opt in self.select.options:
            opt.default = opt.value == key
        embed = help_overview(self.is_mod) if key == "overview" else help_page(key)
        await interaction.response.edit_message(embed=embed, view=self)


def help_message(user: discord.abc.User) -> dict:
    is_mod = viewer_is_mod(user)
    return {"embed": help_overview(is_mod), "view": HelpView(user.id, is_mod)}


# ---------- Info embeds ----------

def ping_embed(gateway_ms: int, roundtrip_ms: int | None = None) -> discord.Embed:
    worst = max(gateway_ms, roundtrip_ms or 0)
    color, mood = (OK, "Smooth 🟢") if worst < 200 else (WARN, "A bit slow 🟡") if worst < 500 else (BAD, "Laggy 🔴")
    e = discord.Embed(title="🏓 Pong!", description=mood, color=color)
    e.add_field(name="📡 Gateway", value=f"`{gateway_ms} ms`")
    e.add_field(name="↩️ Round trip", value=f"`{roundtrip_ms} ms`" if roundtrip_ms is not None else "`…`")
    return e


def serverinfo_embed(guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(title=guild.name, description=guild.description, color=GOLD)
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        e.set_image(url=guild.banner.with_size(1024).url)
    e.add_field(name="👑 Owner", value=f"<@{guild.owner_id}>")
    e.add_field(name="👥 Members", value=f"{guild.member_count:,}" if guild.member_count else "?")
    e.add_field(name="🚀 Boosts", value=f"Level {guild.premium_tier} · {guild.premium_subscription_count or 0} boosts")
    channels = (f"💬 {len(guild.text_channels)} text · 🔊 {len(guild.voice_channels)} voice"
                + (f" · 🗂️ {len(guild.forums)} forum" if guild.forums else ""))
    e.add_field(name="📺 Channels", value=channels)
    e.add_field(name="🎭 Roles", value=str(len(guild.roles) - 1))
    e.add_field(name="😀 Emojis", value=f"{len(guild.emojis)} / {guild.emoji_limit}")
    setups = bot.get_cog("Setups")
    if setups:
        e.add_field(name="🎧 Setups shared", value=f"{setups.db.count():,}")
    e.add_field(name="🔒 Verification", value=str(guild.verification_level).replace("_", " ").title())
    e.add_field(name="📅 Created", value=rel(guild.created_at), inline=False)
    return brand_footer(e, f"Server ID {guild.id}")


def userinfo_embed(member: discord.Member) -> tuple[discord.Embed, bool]:
    """(embed, has_setup)"""
    color = member.color if member.color.value else GOLD
    e = discord.Embed(color=color)
    e.set_author(name=f"{member.display_name} (@{member.name})", icon_url=member.display_avatar.url)
    e.set_thumbnail(url=member.display_avatar.url)

    badges = []
    if member.bot:
        badges.append("🤖 Bot")
    if member.id == member.guild.owner_id:
        badges.append("👑 Owner")
    elif viewer_is_mod(member):
        badges.append("🛡️ Moderator")
    if member.premium_since:
        badges.append(f"🚀 Boosting since {discord.utils.format_dt(member.premium_since, 'D')}")
    if member.is_timed_out():
        badges.append(f"🔇 Timed out until {discord.utils.format_dt(member.timed_out_until, 'f')}")
    setups = bot.get_cog("Setups")
    has_setup = bool(setups and setups.db.get(member.id))
    badges.append("🎧 Setup shared" if has_setup else "🎧 No setup yet")
    e.description = " · ".join(badges)

    e.add_field(name="📥 Joined server", value=rel(member.joined_at), inline=False)
    e.add_field(name="🗓️ Account created", value=rel(member.created_at), inline=False)
    roles = [r for r in reversed(member.roles) if not r.is_default()]
    if roles:
        shown = " ".join(r.mention for r in roles[:15]) + (f" +{len(roles) - 15} more" if len(roles) > 15 else "")
        e.add_field(name=f"🎭 Roles ({len(roles)})", value=shown, inline=False)
    return brand_footer(e, f"User ID {member.id}"), has_setup


class UserInfoView(discord.ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=300)
        self.member = member

    @discord.ui.button(label="View setup", emoji="🎧", style=discord.ButtonStyle.secondary)
    async def view_setup(self, interaction: discord.Interaction, _b: discord.ui.Button):
        from cogs.setups import send_setup
        setups = bot.get_cog("Setups")
        row = setups.db.get(self.member.id) if setups else None
        if row is None:
            await interaction.response.send_message("That setup no longer exists.", ephemeral=True)
            return
        await send_setup(interaction, setups.db, self.member, row, shareable=True, ephemeral=True)


def userinfo_message(member: discord.Member) -> dict:
    embed, has_setup = userinfo_embed(member)
    return {"embed": embed, "view": UserInfoView(member) if has_setup else discord.utils.MISSING,
            "allowed_mentions": discord.AllowedMentions.none()}


# ---------- Slash commands ----------

@bot.tree.command(name="help", description="Show bot commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(**help_message(interaction.user), ephemeral=True)


@bot.tree.command(name="ping", description="Check bot latency")
async def slash_ping(interaction: discord.Interaction):
    gateway = round(bot.latency * 1000)
    start = time.perf_counter()
    await interaction.response.send_message(embed=ping_embed(gateway))
    await interaction.edit_original_response(embed=ping_embed(gateway, round((time.perf_counter() - start) * 1000)))


@bot.tree.command(name="hello", description="Say hello")
async def slash_hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}! 👋 Try `/help` to see what I can do.")


@bot.tree.command(name="serverinfo", description="Info about this server")
@app_commands.guild_only()
async def slash_serverinfo(interaction: discord.Interaction):
    await interaction.response.send_message(embed=serverinfo_embed(interaction.guild))


@bot.tree.command(name="userinfo", description="Info about a member")
@app_commands.guild_only()
@app_commands.describe(member="Member to look up (defaults to you)")
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member | None = None):
    await interaction.response.send_message(**userinfo_message(member or interaction.user))


# ---------- Prefix commands ----------

@bot.command(name="help")
async def prefix_help(ctx: commands.Context):
    await ctx.send(**help_message(ctx.author))


@bot.command(name="ping")
async def prefix_ping(ctx: commands.Context):
    gateway = round(bot.latency * 1000)
    start = time.perf_counter()
    msg = await ctx.send(embed=ping_embed(gateway))
    await msg.edit(embed=ping_embed(gateway, round((time.perf_counter() - start) * 1000)))


@bot.command(name="hello")
async def prefix_hello(ctx: commands.Context):
    await ctx.send(f"Hello {ctx.author.mention}! 👋 Try `/help` to see what I can do.")


@bot.command(name="serverinfo")
@commands.guild_only()
async def prefix_serverinfo(ctx: commands.Context):
    await ctx.send(embed=serverinfo_embed(ctx.guild))


@bot.command(name="userinfo")
@commands.guild_only()
async def prefix_userinfo(ctx: commands.Context, member: discord.Member = None):
    await ctx.send(**userinfo_message(member or ctx.author))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("Couldn't find that member.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing `{error.param.name}`. Usage: `{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`")
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("That command only works in a server.")
        return
    log.exception("Command error", exc_info=error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set. Add it as an environment variable (or in a .env file).")
    bot.run(TOKEN, log_handler=None)
