"""Moderation tools for B_Media Community Bot: custom slowmode."""

import discord
from discord import app_commands
from discord.ext import commands

MAX_SLOWMODE = 21600  # Discord API max: 6 hours

# Channel types that support slowmode
SlowmodeChannel = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.ForumChannel | discord.Thread


def fmt_duration(seconds: int) -> str:
    if seconds == 0:
        return "off"
    parts = []
    for unit, size in (("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts)


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def apply_slowmode(self, channel, seconds: int, actor: discord.abc.User) -> discord.Embed:
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {actor} ({actor.id})")
        if seconds == 0:
            desc = f"🐇 Slowmode **disabled** in {channel.mention}."
        else:
            desc = f"🐢 Slowmode set to **{fmt_duration(seconds)}** in {channel.mention}."
        return discord.Embed(description=desc, color=discord.Color.blurple())

    # /slowmode seconds [channel]
    @app_commands.command(name="slowmode", description="Set custom slowmode (any seconds, e.g. 3) for a channel")
    @app_commands.describe(
        seconds="Seconds between messages per user (0 = off, max 21600)",
        channel="Channel to change (defaults to this one)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)  # hidden from members without permission
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, MAX_SLOWMODE],
        channel: SlowmodeChannel | None = None,
    ):
        channel = channel or interaction.channel
        if not hasattr(channel, "slowmode_delay"):
            await interaction.response.send_message("That channel doesn't support slowmode.", ephemeral=True)
            return
        embed = await self.apply_slowmode(channel, seconds, interaction.user)
        await interaction.response.send_message(embed=embed)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Channels** permission to do that."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = "I need the **Manage Channels** permission in that channel."
        elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
            msg = "I don't have permission to edit that channel."
        else:
            raise error
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # !slowmode <seconds> [#channel]
    @commands.command(name="slowmode")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def prefix_slowmode(self, ctx: commands.Context, seconds: int, channel: discord.TextChannel = None):
        if not 0 <= seconds <= MAX_SLOWMODE:
            await ctx.send(f"Seconds must be between 0 and {MAX_SLOWMODE}.")
            return
        embed = await self.apply_slowmode(channel or ctx.channel, seconds, ctx.author)
        await ctx.send(embed=embed)

    @prefix_slowmode.error
    async def prefix_slowmode_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need the **Manage Channels** permission to do that.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("I need the **Manage Channels** permission here.")
        elif isinstance(error, commands.BadArgument):
            p = ctx.clean_prefix
            await ctx.send(f"Usage: `{p}slowmode <seconds> [#channel]` — e.g. `{p}slowmode 3`")
        elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
            await ctx.send("I don't have permission to edit that channel.")
        # MissingRequiredArgument falls through to the global handler in main.py


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
