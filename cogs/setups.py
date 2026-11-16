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
# Convention: every list-type field is a paragraph input, one item per line
# (rendered as bullets in the embed). Follow this for any new field.
GEAR_FIELDS = [
    ("source", "Source", "🎵", "One per line, e.g.\nQobuz on PC\nWiiM Pro\nRega Planar 3", True, 500),
    ("dac", "DAC & DAC/AMP", "🔌", "One per line, e.g.\nTopping D90SE\nFiiO K9 Pro", True, 500),
    ("amp", "Amp", "⚡", "One per line, e.g.\nSchiit Jotunheim 2\nCayin HA-6A", True, 500),
    ("headphones", "Headphones & IEMs", "🎧", "One per line, e.g.\nSennheiser HD 800 S\nMoondrop Blessing 3", True, 500),
    ("speakers", "Speakers", "🔊", "One per line, e.g.\nKEF LS50 Meta\nSVS SB-1000 Pro", True, 500),
]
EXTRA_FIELDS = [
    ("other", "Cables, Accessories & Other", "🧰", "One per line, e.g.\nCable, stands, power, tubes...", True, 500),
    ("genres", "Favorite Genres", "🎼", "One per line, e.g.\nJazz\nClassical\nProg Rock", True, 300),
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

LINES_PER_PAGE = 14  # item lines + field headers per page before splitting
SECTION_ORDER = ["source", "dac", "amp", "headphones", "speakers", "other", "genres", "notes"]
INLINE_KEYS = {"source", "dac", "amp", "headphones", "speakers"}


def render_items(key: str, items: list[str], bulleted: bool) -> str:
    if key == "notes":
        return clip("\n".join(f"> {ln}" for ln in items), 1024)
    if not bulleted:
        return clip(items[0], 1024)
    return clip("\n".join(f"• {i}" for i in items), 1024)


def build_setup_pages(member: discord.abc.User, row: dict) -> list[discord.Embed]:
    """Build one or more embeds; long sections split across pages with '(cont.)'."""
    color = getattr(member, "color", None)
    color = color if color and color.value else GOLD

    # Signal chain summary: first item of each stage (page 1 only)
    chain = []
    for key in ("source", "dac", "amp"):
        if items := lines_of(row.get(key)):
            chain.append(f"`{clip(items[0], 30)}`")
    endpoints = [f"`{clip(lines_of(row.get(k))[0], 30)}`" for k in ("headphones", "speakers") if lines_of(row.get(k))]
    if endpoints:
        chain.append(" / ".join(endpoints))
    summary = "**Signal chain**\n" + " ➜ ".join(chain) if len(chain) > 1 else None

    # Distribute fields across pages
    pages: list[list[tuple[str, str, bool]]] = [[]]
    used = 0
    for key in SECTION_ORDER:
        if key == "notes":
            items = [ln.strip() for ln in (row.get(key) or "").splitlines() if ln.strip()]
        else:
            items = lines_of(row.get(key))
        if not items:
            continue
        _, label, emoji, *_ = FIELD_INFO[key]
        bulleted = len(items) > 1
        first = True
        while items:
            room = LINES_PER_PAGE - used - 1  # -1 for the field header
            if pages[-1] and room < min(2, len(items)):  # avoid orphaning one line at page bottom
                pages.append([])
                used, room = 0, LINES_PER_PAGE - 1
            chunk, items = items[:room], items[room:]
            name = f"{emoji} {label}" + ("" if first else " (cont.)")
            pages[-1].append((name, render_items(key, chunk, bulleted), key in INLINE_KEYS))
            used += len(chunk) + 1
            first = False

    total = len(pages)
    embeds = []
    for i, fields in enumerate(pages):
        e = discord.Embed(title=f"🎧 {member.display_name}'s Setup", color=color)
        e.set_thumbnail(url=member.display_avatar.url)
        if i == 0:
            e.description = summary
            if not fields:
                e.description = (summary + "\n\n" if summary else "") + "*No gear listed yet.*"
        for name, value, inline in fields:
            e.add_field(name=name, value=value, inline=inline)
        footer = ["B_Media Community"]
        if total > 1:
            footer.insert(0, f"Page {i + 1}/{total}")
        if row.get("updated_at"):
            footer.append("Last updated")
            e.timestamp = datetime.fromtimestamp(row["updated_at"], tz=timezone.utc)
        e.set_footer(text=" • ".join(footer))
        embeds.append(e)
    return embeds


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
        await send_setup(
            interaction, self.db, interaction.user, self.db.get(interaction.user.id),
            shareable=True, ephemeral=True, content="✅ Setup saved! Here's how it looks:",
        )


class SetupView(discord.ui.View):
    """Page buttons (◀ 1/3 ▶) and optional 'Share to channel' for setup embeds."""

    def __init__(self, db: SetupDB, member: discord.abc.User, pages: list[discord.Embed],
                 owner_id: int, shareable: bool):
        super().__init__(timeout=300)
        self.db, self.member, self.pages, self.owner_id = db, member, pages, owner_id
        self.index = 0
        self.message: discord.Message | None = None
        if len(pages) <= 1:
            for item in (self.prev, self.counter, self.next):
                self.remove_item(item)
        if not shareable:
            self.remove_item(self.share)
        self._sync()

    def _sync(self):
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who ran this can use these buttons — try `/setup view` yourself!",
                ephemeral=True,
            )
            return False
        return True

    async def _flip(self, interaction: discord.Interaction, step: int):
        self.index = max(0, min(len(self.pages) - 1, self.index + step))
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._flip(interaction, -1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._flip(interaction, 1)

    @discord.ui.button(label="Share to channel", emoji="📢", style=discord.ButtonStyle.primary)
    async def share(self, interaction: discord.Interaction, _button: discord.ui.Button):
        row = self.db.get(self.member.id)
        if row is None:
            await interaction.response.edit_message(content="That setup no longer exists.", embed=None, view=None)
            return
        await interaction.response.edit_message(content="📢 Shared to the channel!", view=None)
        self.stop()
        if self.member.id == interaction.user.id:
            content = f"{interaction.user.mention} shared their setup"
        else:
            content = f"{interaction.user.mention} shared {self.member.mention}'s setup"
        await send_setup(interaction, self.db, self.member, row, shareable=False,
                         ephemeral=False, content=content, followup=True)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def send_setup(target: discord.Interaction | commands.Context, db: SetupDB,
                     member: discord.abc.User, row: dict, *, shareable: bool = False,
                     ephemeral: bool = False, content: str | None = None, followup: bool = False):
    """Send a (possibly multi-page) setup embed via slash response, followup, or prefix ctx."""
    pages = build_setup_pages(member, row)
    owner = target.user if isinstance(target, discord.Interaction) else target.author
    view = SetupView(db, member, pages, owner.id, shareable) if len(pages) > 1 or shareable else None
    kwargs = {"content": content, "embed": pages[0], "allowed_mentions": discord.AllowedMentions.none()}
    if view:
        kwargs["view"] = view

    if isinstance(target, commands.Context):
        msg = await target.send(**kwargs)
    elif followup:
        msg = await target.followup.send(**kwargs, ephemeral=ephemeral, wait=True)
    else:
        await target.response.send_message(**kwargs, ephemeral=ephemeral)
        msg = await target.original_response()
    if view:
        view.message = msg


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
    @setup_group.command(name="view", description="Show your or someone's audio setup to the channel")
    @app_commands.describe(member="Whose setup to view (defaults to you)")
    async def setup_view(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        row = self.db.get(member.id)
        if row is None:
            await interaction.response.send_message(
                embed=no_setup_embed(member, member == interaction.user), ephemeral=True
            )
            return
        await send_setup(interaction, self.db, member, row)

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
        await send_setup(interaction, self.db, member, row, shareable=True, ephemeral=True)

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
        await send_setup(ctx, self.db, member, row)

    @commands.command(name="whohas")
    @commands.guild_only()
    async def prefix_whohas(self, ctx: commands.Context, *, gear: str):
        if len(norm(gear)) < 2:
            await ctx.send("Search needs at least 2 letters/numbers.")
            return
        await ctx.send(embed=build_whohas_embed(ctx.guild, gear, self.db.search(norm(gear))))


async def setup(bot: commands.Bot):
    await bot.add_cog(Setups(bot))
