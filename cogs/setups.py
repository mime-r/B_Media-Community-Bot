"""Setup profiles + gear-owner lookup for B_Media Community Bot."""

import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "bmedia.db"))

GOLD = discord.Color.from_rgb(212, 175, 55)

# (key, label, emoji, placeholder, paragraph?, max_length)
GEAR_FIELDS = [
    ("source", "Source", "🎵", "e.g. Qobuz on PC, WiiM Pro, Rega Planar 3", False, 200),
    ("dac", "DAC", "🔌", "e.g. Topping D90SE, RME ADI-2 DAC", False, 200),
    ("amp", "Amp", "⚡", "e.g. Schiit Jotunheim 2, Cayin HA-6A", False, 200),
    ("headphones", "Headphones & IEMs", "🎧", "One per line, e.g.\nSennheiser HD 800 S\nMoondrop Blessing 3", True, 500),
    ("speakers", "Speakers", "🔊", "One per line, e.g.\nKEF LS50 Meta\nSVS SB-1000 Pro", True, 500),
]
EXTRA_FIELDS = [
    ("other", "Cables, Accessories & Other", "🧰", "One per line: cables, stands, power, tubes...", True, 500),
    ("genres", "Favorite Genres", "🎼", "e.g. Jazz, Classical, Prog Rock", False, 200),
    ("notes", "Notes", "📝", "EQ, target curve, dream gear, room details...", True, 500),
]
ALL_FIELDS = GEAR_FIELDS + EXTRA_FIELDS
FIELD_INFO = {f[0]: f for f in ALL_FIELDS}
# Fields that count as "owned gear" for /whohas
SEARCH_KEYS = ["source", "dac", "amp", "headphones", "speakers", "other"]


def norm(text: str | None) -> str:
    """Lowercase + strip non-alphanumerics so 'HD 800 S' matches 'hd800s'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def lines_of(value: str | None) -> list[str]:
    return [ln.strip(" •-\t") for ln in (value or "").splitlines() if ln.strip(" •-\t")]


# ---------- Database ----------

class SetupDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("norm", 1, norm, deterministic=True)
        cols = ", ".join(f"{key} TEXT" for key, *_ in ALL_FIELDS)
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS setups (user_id INTEGER PRIMARY KEY, {cols}, updated_at INTEGER)"
        )
        self.conn.commit()

    def get(self, user_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM setups WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def upsert(self, user_id: int, data: dict):
        cols = [c for c in data if c in FIELD_INFO]  # whitelist column names
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO setups (user_id, {', '.join(cols)}, updated_at) VALUES (?, {placeholders}, ?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {updates}, updated_at = excluded.updated_at",
            (user_id, *[data[c] for c in cols], int(time.time())),
        )
        self.conn.commit()

    def delete(self, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM setups WHERE user_id = ?", (user_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def search(self, query: str) -> list[dict]:
        where = " OR ".join(f"norm({k}) LIKE ?" for k in SEARCH_KEYS)
        rows = self.conn.execute(f"SELECT * FROM setups WHERE {where}", [f"%{query}%"] * len(SEARCH_KEYS))
        return [dict(r) for r in rows]

    def all_gear_lines(self) -> list[str]:
        rows = self.conn.execute(f"SELECT {', '.join(SEARCH_KEYS)} FROM setups").fetchall()
        return [ln for r in rows for k in SEARCH_KEYS for ln in lines_of(r[k])]


# ---------- Embeds ----------

def format_value(value: str) -> str:
    items = lines_of(value)
    if len(items) == 1:
        return clip(items[0], 1024)
    return clip("\n".join(f"• {i}" for i in items), 1024)


def build_setup_embed(member: discord.abc.User, row: dict) -> discord.Embed:
    color = getattr(member, "color", None)
    color = color if color and color.value else GOLD
    e = discord.Embed(title=f"🎧 {member.display_name}'s Setup", color=color)
    e.set_thumbnail(url=member.display_avatar.url)

    # Signal chain summary: first item of each stage
    chain = []
    for key in ("source", "dac", "amp"):
        if items := lines_of(row.get(key)):
            chain.append(f"`{clip(items[0], 30)}`")
    endpoints = [f"`{clip(lines_of(row.get(k))[0], 30)}`" for k in ("headphones", "speakers") if lines_of(row.get(k))]
    if endpoints:
        chain.append(" / ".join(endpoints))
    if len(chain) > 1:
        e.description = "**Signal chain**\n" + " ➜ ".join(chain)

    # Chain components side by side
    for key in ("source", "dac", "amp"):
        _, label, emoji, *_ = FIELD_INFO[key]
        e.add_field(name=f"{emoji} {label}", value=format_value(row[key]) if row.get(key) else "—", inline=True)

    # Endpoints + extras only when filled in
    for key in ("headphones", "speakers", "other", "genres"):
        if row.get(key):
            _, label, emoji, *_ = FIELD_INFO[key]
            e.add_field(name=f"{emoji} {label}", value=format_value(row[key]), inline=key in ("headphones", "speakers"))

    if row.get("notes"):
        quoted = "\n".join(f"> {ln}" for ln in row["notes"].splitlines())
        e.add_field(name="📝 Notes", value=clip(quoted, 1024), inline=False)

    if row.get("updated_at"):
        e.timestamp = datetime.fromtimestamp(row["updated_at"], tz=timezone.utc)
        e.set_footer(text="B_Media Community • Last updated")
    return e


def no_setup_embed(member: discord.abc.User, is_self: bool) -> discord.Embed:
    msg = (
        "You haven't shared your setup yet.\nUse **/setup edit** to add your gear!"
        if is_self
        else f"{member.mention} hasn't shared their setup yet."
    )
    return discord.Embed(description=f"🎧 {msg}", color=GOLD)


def build_whohas_embed(guild: discord.Guild, query: str, rows: list[dict]) -> discord.Embed:
    q = norm(query)
    entries = []
    for row in rows:
        member = guild.get_member(row["user_id"])
        if member is None:  # only show members of this server
            continue
        hits = []
        for key in SEARCH_KEYS:
            emoji = FIELD_INFO[key][2]
            hits += [f"{emoji} {clip(ln, 60)}" for ln in lines_of(row.get(key)) if q in norm(ln)]
        if hits:
            entries.append((member.display_name.lower(), f"{member.mention} — {' · '.join(hits)}"))
    entries.sort()

    e = discord.Embed(title=f"🔍 Who has “{clip(query, 50)}”?", color=GOLD)
    if not entries:
        e.description = "Nobody here has listed that yet.\nOwn it? Add it with **/setup edit**."
        return e

    shown, length = [], 0
    for _, line in entries:
        if length + len(line) + 1 > 3800 or len(shown) >= 25:
            break
        shown.append(line)
        length += len(line) + 1
    e.description = "\n".join(shown)
    extra = len(entries) - len(shown)
    footer = f"{len(entries)} member{'s' if len(entries) != 1 else ''} • Ask them for impressions!"
    if extra:
        footer = f"Showing {len(shown)} of {len(entries)} members • Try a more specific search"
    e.set_footer(text=footer)
    return e


# ---------- UI ----------

class ProfileModal(discord.ui.Modal):
    def __init__(self, db: SetupDB, title: str, fields: list, current: dict):
        super().__init__(title=title)
        self.db = db
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for key, label, _emoji, placeholder, paragraph, max_len in fields:
            ti = discord.ui.TextInput(
                label=label,
                placeholder=clip(placeholder, 100),
                default=current.get(key) or None,
                required=False,
                max_length=max_len,
                style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
            )
            self.add_item(ti)
            self.inputs[key] = ti

    async def on_submit(self, interaction: discord.Interaction):
        data = {k: (ti.value.strip() or None) for k, ti in self.inputs.items()}
        self.db.upsert(interaction.user.id, data)
        embed = build_setup_embed(interaction.user, self.db.get(interaction.user.id))
        await interaction.response.send_message(
            "✅ Setup saved! Here's how it looks:", embed=embed, ephemeral=True
        )


class ConfirmClear(discord.ui.View):
    def __init__(self, db: SetupDB, user_id: int):
        super().__init__(timeout=60)
        self.db, self.user_id = db, user_id

    @discord.ui.button(label="Delete my setup", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.db.delete(self.user_id)
        await interaction.response.edit_message(content="🗑️ Your setup has been deleted.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled. Your setup is safe.", view=None)


# ---------- Cog ----------

class Setups(commands.Cog):
    setup_group = app_commands.Group(name="setup", description="Share and view audio setups")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = SetupDB(DB_PATH)
        self.view_menu = app_commands.ContextMenu(name="View Setup", callback=self.view_setup_menu)

    async def cog_load(self):
        self.bot.tree.add_command(self.view_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.view_menu.name, type=self.view_menu.type)
        self.db.conn.close()

    # /setup view
    @setup_group.command(name="view", description="View someone's audio setup")
    @app_commands.describe(member="Whose setup to view (defaults to you)")
    async def setup_view(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        row = self.db.get(member.id)
        if row is None:
            await interaction.response.send_message(
                embed=no_setup_embed(member, member == interaction.user), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=build_setup_embed(member, row))

    # /setup edit
    @setup_group.command(name="edit", description="Add or edit your gear chain")
    async def setup_edit(self, interaction: discord.Interaction):
        current = self.db.get(interaction.user.id) or {}
        await interaction.response.send_modal(ProfileModal(self.db, "Your Audio Chain", GEAR_FIELDS, current))

    # /setup extras
    @setup_group.command(name="extras", description="Add accessories, favorite genres and notes")
    async def setup_extras(self, interaction: discord.Interaction):
        current = self.db.get(interaction.user.id) or {}
        await interaction.response.send_modal(ProfileModal(self.db, "Extras", EXTRA_FIELDS, current))

    # /setup clear
    @setup_group.command(name="clear", description="Delete your setup profile")
    async def setup_clear(self, interaction: discord.Interaction):
        if self.db.get(interaction.user.id) is None:
            await interaction.response.send_message("You don't have a setup saved.", ephemeral=True)
            return
        await interaction.response.send_message(
            "⚠️ Delete your setup profile? This can't be undone.",
            view=ConfirmClear(self.db, interaction.user.id),
            ephemeral=True,
        )

    # Right-click user → Apps → View Setup
    async def view_setup_menu(self, interaction: discord.Interaction, member: discord.Member):
        row = self.db.get(member.id)
        if row is None:
            await interaction.response.send_message(
                embed=no_setup_embed(member, member == interaction.user), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=build_setup_embed(member, row), ephemeral=True)

    # /whohas
    @app_commands.command(name="whohas", description="Find members who own a piece of gear")
    @app_commands.describe(gear="Gear to search for, e.g. HD800S, D90, LS50")
    @app_commands.guild_only()
    async def whohas(self, interaction: discord.Interaction, gear: str):
        if len(norm(gear)) < 2:
            await interaction.response.send_message("Search needs at least 2 letters/numbers.", ephemeral=True)
            return
        embed = build_whohas_embed(interaction.guild, gear, self.db.search(norm(gear)))
        await interaction.response.send_message(embed=embed)

    @whohas.autocomplete("gear")
    async def whohas_autocomplete(self, _interaction: discord.Interaction, current: str):
        q = norm(current)
        seen, choices = set(), []
        for ln in self.db.all_gear_lines():
            key = norm(ln)
            if key and key not in seen and q in key:
                seen.add(key)
                choices.append(app_commands.Choice(name=clip(ln, 100), value=clip(ln, 100)))
                if len(choices) >= 25:
                    break
        return choices

    # Prefix versions (view/search only — editing uses forms, slash only)
    @commands.command(name="setup")
    async def prefix_setup(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        row = self.db.get(member.id)
        if row is None:
            await ctx.send(embed=no_setup_embed(member, member == ctx.author))
            return
        await ctx.send(embed=build_setup_embed(member, row))

    @commands.command(name="whohas")
    @commands.guild_only()
    async def prefix_whohas(self, ctx: commands.Context, *, gear: str):
        if len(norm(gear)) < 2:
            await ctx.send("Search needs at least 2 letters/numbers.")
            return
        await ctx.send(embed=build_whohas_embed(ctx.guild, gear, self.db.search(norm(gear))))


async def setup(bot: commands.Bot):
    await bot.add_cog(Setups(bot))
