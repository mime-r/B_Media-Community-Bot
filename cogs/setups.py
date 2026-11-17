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
# Each group = one form (Discord max 5 inputs per form). New group → add to FORM_GROUPS.
CHAIN_FIELDS = [
    ("source", "Source", "🎵", "One per line, e.g.\nQobuz on PC\nWiiM Pro\nRega Planar 3", True, 500),
    ("dac", "DACs & DAC/AMPs", "🔌", "One per line, e.g.\nTopping D90SE\nFiiO K9 Pro", True, 500),
    ("amp", "Amp", "⚡", "One per line, e.g.\nSchiit Jotunheim 2\nCayin HA-6A", True, 500),
]
LISTEN_FIELDS = [
    ("headphones", "Headphones", "🎧", "One per line, e.g.\nSennheiser HD 800 S\nHifiman Arya", True, 500),
    ("iems", "IEMs/Earbuds", "👂", "One per line, e.g.\nMoondrop Blessing 3\nThieaudio Monarch", True, 500),
    ("flatheads", "Flatheads", "🔘", "One per line, e.g.\nSennheiser MX500\nYuin PK1\nVE Monk Plus", True, 500),
    ("speakers", "Speakers", "🔊", "One per line, e.g.\nKEF LS50 Meta\nSVS SB-1000 Pro", True, 500),
]
EXTRA_FIELDS = [
    ("other", "Cables, Accessories & Other", "🧰", "One per line, e.g.\nCable, stands, power, tubes...", True, 500),
    ("genres", "Favorite Genres", "🎼", "One per line, e.g.\nJazz\nClassical\nProg Rock", True, 300),
    ("notes", "Notes", "📝", "EQ, target curve, dream gear, room details...", True, 500),
]
# Custom signal chain shown at the top of the embed. Empty → auto-built from gear.
SIGNAL_FIELDS = [
    ("signal_chain", "Signal Chain (one stage per line)", "🔗",
     "One stage per line, in order, e.g.\nQobuz\nTopping D90SE\nSchiit Jotunheim 2\nHD 800 S", True, 300),
]
# (button label, emoji, form title, fields)
FORM_GROUPS = [
    ("Source / DAC / Amp", "🎛️", "Your Audio Chain", CHAIN_FIELDS),
    ("Headphones / IEMs / Flatheads / Speakers", "🎧", "Headphones, IEMs, Flatheads & Speakers", LISTEN_FIELDS),
    ("Extras", "🧰", "Extras", EXTRA_FIELDS),
    ("Signal Chain", "🔗", "Signal Chain", SIGNAL_FIELDS),
]
ALL_FIELDS = CHAIN_FIELDS + LISTEN_FIELDS + EXTRA_FIELDS + SIGNAL_FIELDS
FIELD_INFO = {f[0]: f for f in ALL_FIELDS}
# Fields that count as "owned gear" for /whohas
SEARCH_KEYS = ["source", "dac", "amp", "headphones", "iems", "flatheads", "speakers", "other"]
ENDPOINT_KEYS = ("headphones", "iems", "flatheads", "speakers")
# Shown only when filled in, placed after their row (never as "—" placeholders)
OPTIONAL_KEYS = {"flatheads"}

VIEW_COOLDOWN = 60  # seconds between public setup posts per user


class UserCooldown:
    """Per-user cooldown shared by /setup view, !setup and the Share button."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        self.last: dict[int, float] = {}

    def remaining(self, user_id: int) -> float:
        return max(0.0, self.last.get(user_id, 0) + self.seconds - time.monotonic())

    def trigger(self, user_id: int):
        now = time.monotonic()
        self.last[user_id] = now
        # prune expired entries so the dict doesn't grow forever
        if len(self.last) > 1000:
            self.last = {u: t for u, t in self.last.items() if now - t < self.seconds}


view_cooldown = UserCooldown(VIEW_COOLDOWN)


def cooldown_message(user_id: int) -> str | None:
    """Return a 'slow down' message if user is on cooldown, else None."""
    left = view_cooldown.remaining(user_id)
    if left <= 0:
        return None
    ready_at = int(time.time() + left)
    return f"⏳ Slow down! You can post a setup again <t:{ready_at}:R>."


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
        # Auto-migrate: add columns for fields added after the DB was created
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(setups)")}
        for key, *_ in ALL_FIELDS:
            if key not in existing:
                self.conn.execute(f"ALTER TABLE setups ADD COLUMN {key} TEXT")
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

def render_items(key: str, items: list[str]) -> str:
    if key == "notes":
        return clip("\n".join(f"> {ln}" for ln in items), 1024)
    if len(items) == 1:
        return clip(items[0], 1024)
    return clip("\n".join(f"• {i}" for i in items), 1024)


def auto_chain(row: dict) -> list[str]:
    """Default signal chain: first item of each stage, endpoints joined with ' / '."""
    chain = [items[0] for key in ("source", "dac", "amp") if (items := lines_of(row.get(key)))]
    endpoints = [lines_of(row.get(k))[0] for k in ENDPOINT_KEYS if lines_of(row.get(k))]
    if endpoints:
        chain.append(" / ".join(endpoints))
    return chain


def build_setup_embed(member: discord.abc.User, row: dict) -> discord.Embed:
    """One embed with everything. Max field sizes keep it under Discord's 6000-char limit."""
    color = getattr(member, "color", None)
    color = color if color and color.value else GOLD

    # Signal chain summary: user's custom chain if set, else auto-built from gear
    custom = lines_of(row.get("signal_chain"))
    chain = [f"`{clip(stage, 40)}`" for stage in custom] if custom else [
        " / ".join(f"`{clip(part, 30)}`" for part in stage.split(" / ")) for stage in auto_chain(row)
    ]
    summary = "**Signal chain**\n" + " ➜ ".join(chain) if len(chain) > 1 or custom else None

    e = discord.Embed(title=f"🎧 {member.display_name}'s Setup", color=color, description=summary)
    e.set_thumbnail(url=member.display_avatar.url)
    # Side-by-side rows of 3 (Source/DAC/Amp, Headphones/IEMs/Speakers).
    # A row shows if any of its fields is filled; empty ones get "—" so columns stay aligned.
    # Optional fields (e.g. Flatheads) appear only when filled, after their row.
    for group in (CHAIN_FIELDS, LISTEN_FIELDS):
        core = [f for f in group if f[0] not in OPTIONAL_KEYS]
        if any(lines_of(row.get(key)) for key, *_ in core):
            for key, label, emoji, *_ in core:
                items = lines_of(row.get(key))
                e.add_field(name=f"{emoji} {label}", value=render_items(key, items) if items else "—", inline=True)
        for key, label, emoji, *_ in group:
            if key in OPTIONAL_KEYS and (items := lines_of(row.get(key))):
                e.add_field(name=f"{emoji} {label}", value=render_items(key, items), inline=True)

    # Extras stacked full width, only when filled
    for key, label, emoji, *_ in EXTRA_FIELDS:
        if key == "notes":
            items = [ln.strip() for ln in (row.get(key) or "").splitlines() if ln.strip()]
        else:
            items = lines_of(row.get(key))
        if items:
            e.add_field(name=f"{emoji} {label}", value=render_items(key, items), inline=False)
    if not e.fields:
        e.description = (summary + "\n\n" if summary else "") + "*No gear listed yet.*"
    if row.get("updated_at"):
        e.timestamp = datetime.fromtimestamp(row["updated_at"], tz=timezone.utc)
        e.set_footer(text="B_Media Community • Last updated")
    else:
        e.set_footer(text="B_Media Community")
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
        # Unchanged auto chain → store nothing, so it keeps following gear edits
        if "signal_chain" in data:
            current = self.db.get(interaction.user.id) or {}
            if lines_of(data["signal_chain"]) == auto_chain(current):
                data["signal_chain"] = None
        self.db.upsert(interaction.user.id, data)
        await send_setup(
            interaction, self.db, interaction.user, self.db.get(interaction.user.id),
            shareable=True, ephemeral=True, content="✅ Setup saved! Here's how it looks:",
        )


class ShareView(discord.ui.View):
    """'Share to channel' button on private (ephemeral) setup embeds."""

    def __init__(self, db: SetupDB, member: discord.abc.User):
        super().__init__(timeout=600)
        self.db, self.member = db, member

    @discord.ui.button(label="Share to channel", emoji="📢", style=discord.ButtonStyle.primary)
    async def share(self, interaction: discord.Interaction, _button: discord.ui.Button):
        row = self.db.get(self.member.id)
        if row is None:
            await interaction.response.edit_message(content="That setup no longer exists.", embed=None, view=None)
            return
        if msg := cooldown_message(interaction.user.id):
            await interaction.response.send_message(msg, ephemeral=True)  # keep button for later
            return
        view_cooldown.trigger(interaction.user.id)
        await interaction.response.edit_message(content="📢 Shared to the channel!", view=None)
        self.stop()
        if self.member.id == interaction.user.id:
            content = f"{interaction.user.mention} shared their setup"
        else:
            content = f"{interaction.user.mention} shared {self.member.mention}'s setup"
        await send_setup(interaction, self.db, self.member, row, content=content, followup=True)


async def send_setup(target: discord.Interaction | commands.Context, db: SetupDB,
                     member: discord.abc.User, row: dict, *, shareable: bool = False,
                     ephemeral: bool = False, content: str | None = None, followup: bool = False):
    """Send a setup embed via slash response, followup, or prefix ctx."""
    kwargs = {
        "content": content,
        "embed": build_setup_embed(member, row),
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if shareable:
        kwargs["view"] = ShareView(db, member)

    if isinstance(target, commands.Context):
        await target.send(**kwargs)
    elif followup:
        await target.followup.send(**kwargs, ephemeral=ephemeral)
    else:
        await target.response.send_message(**kwargs, ephemeral=ephemeral)


class EditPanel(discord.ui.View):
    """Private panel from /setup edit: one button per form group."""

    def __init__(self, db: SetupDB):
        super().__init__(timeout=600)
        self.db = db
        for label, emoji, title, fields in FORM_GROUPS:
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary)
            button.callback = self._opener(title, fields)
            self.add_item(button)

    def _opener(self, title: str, fields: list):
        async def callback(interaction: discord.Interaction):
            current = self.db.get(interaction.user.id) or {}
            # Prefill signal chain with the auto-built one so users can tweak it
            if not current.get("signal_chain") and (auto := auto_chain(current)):
                current["signal_chain"] = "\n".join(auto)
            await interaction.response.send_modal(ProfileModal(self.db, title, fields, current))
        return callback


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
        if msg := cooldown_message(interaction.user.id):
            await interaction.response.send_message(msg, ephemeral=True)
            return
        view_cooldown.trigger(interaction.user.id)
        await send_setup(interaction, self.db, member, row)

    # /setup edit
    @setup_group.command(name="edit", description="Add or edit your gear")
    async def setup_edit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🛠️ Edit your setup",
            description="Pick a section to edit. Put **one item per line**.\nYou can come back and edit any section anytime.",
            color=GOLD,
        )
        await interaction.response.send_message(embed=embed, view=EditPanel(self.db), ephemeral=True)

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
        if msg := cooldown_message(ctx.author.id):
            await ctx.send(f"{ctx.author.mention} {msg}", delete_after=10)  # auto-clean to avoid clutter
            return
        view_cooldown.trigger(ctx.author.id)
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
