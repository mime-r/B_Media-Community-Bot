"""Shared look for every embed: palette, branded footer, timestamps.

Not an extension: imported by main.py and the cogs. Call set_icon() once in on_ready.
"""

from datetime import datetime

import discord

BRAND = "B_Media Community"

GOLD = discord.Color.from_rgb(212, 175, 55)    # community features (setups, help, info)
LIME = discord.Color.from_rgb(200, 245, 60)    # IEMRanking data
GAME = discord.Color.from_rgb(155, 89, 182)    # games
OK = discord.Color.from_rgb(46, 204, 113)
WARN = discord.Color.from_rgb(241, 196, 15)
BAD = discord.Color.from_rgb(231, 76, 60)

_icon_url: str | None = None


def set_icon(url: str | None):
    """Bot avatar used as the footer icon on branded embeds."""
    global _icon_url
    _icon_url = url


def brand_footer(embed: discord.Embed, text: str | None = None) -> discord.Embed:
    embed.set_footer(text=f"{BRAND} · {text}" if text else BRAND, icon_url=_icon_url)
    return embed


def rel(dt: datetime | None) -> str:
    """'12 Mar 2024 · 2 years ago' as Discord timestamps (each viewer sees their own timezone)."""
    if dt is None:
        return "Unknown"
    return f"{discord.utils.format_dt(dt, 'D')} · {discord.utils.format_dt(dt, 'R')}"


def check(ok: bool) -> str:
    return "✅" if ok else "❌"
