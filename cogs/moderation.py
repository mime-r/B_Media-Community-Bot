"""Moderation tools for B_Media Community Bot.

- /slowmode: custom slowmode (any seconds)
- /warn, /kick, /ban, /softban, /unban, /mute, /unmute: member actions, each saved as a numbered case
  and logged to the mod channel
- /cases, /case: case history per user, view / edit reason / delete a case
- /modconfig: set the log channel and which roles count as moderators, plus a setup health check

Who may use member actions: Administrator / Manage Server, the matching native Discord
permission (e.g. Ban Members for /ban), or any role added with /modconfig addrole.
The bot performs the action with its own permissions, so mod roles need no Discord perms.
"""

import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.setups import DB_PATH
from cogs.style import BAD, GOLD, OK, WARN, brand_footer, check

MAX_SLOWMODE = 21600  # Discord API max: 6 hours
MAX_TIMEOUT = 28 * 86400  # Discord API max: 28 days
CASES_PER_PAGE = 8

# Channel types that support slowmode
SlowmodeChannel = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.ForumChannel | discord.Thread

DELETE_CHOICES = [
    app_commands.Choice(name="Don't delete any", value=0),
    app_commands.Choice(name="Last hour", value=3600),
    app_commands.Choice(name="Last 24 hours", value=86400),
    app_commands.Choice(name="Last 7 days", value=604800),
]

# action -> (emoji, past tense, colour, native permission that also grants it)
ACTIONS = {
    "warn": ("⚠️", "warned", WARN, "moderate_members"),
    "kick": ("👢", "kicked", discord.Color.orange(), "kick_members"),
    "ban": ("🔨", "banned", BAD, "ban_members"),
    "softban": ("🧹", "softbanned", discord.Color.dark_orange(), "ban_members"),
    "unban": ("🕊️", "unbanned", OK, "ban_members"),
    "mute": ("🔇", "muted", discord.Color.dark_grey(), "moderate_members"),
    "unmute": ("🔊", "unmuted", OK, "moderate_members"),
}
# DM wording: "You were warned in X" vs "kicked from X"
DM_PREP = {"warn": "in", "mute": "in", "unmute": "in"}


def fmt_duration(seconds: int) -> str:
    if seconds == 0:
        return "off"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return " ".join(parts)


def parse_duration(text: str) -> int | None:
    """'10m', '1h30m', '2d', '45' (minutes) -> seconds. None if invalid."""
    text = text.strip().lower().replace(" ", "")
    if text.isdigit():
        return int(text) * 60
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    tokens = re.findall(r"(\d+)([smhdw])", text)
    if not tokens or "".join(n + u for n, u in tokens) != text:
        return None
    return sum(int(n) * units[u] for n, u in tokens)


def audit_reason(mod: discord.abc.User, reason: str | None) -> str:
    return f"{mod} ({mod.id}): {reason or 'No reason given'}"[:512]


def nice_perm(name: str) -> str:
    return name.replace("_", " ").title()


# ---------- Storage ----------

class ModDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE IF NOT EXISTS mod_settings (guild_id INTEGER PRIMARY KEY, log_channel_id INTEGER)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS mod_roles (guild_id INTEGER, role_id INTEGER, PRIMARY KEY (guild_id, role_id))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS mod_cases ("
            " guild_id INTEGER, case_no INTEGER, action TEXT, target_id INTEGER, mod_id INTEGER,"
            " reason TEXT, extra TEXT, log_channel_id INTEGER, log_message_id INTEGER, created_at INTEGER,"
            " PRIMARY KEY (guild_id, case_no))"
        )
        # Highest case number ever issued per server, so deleted numbers are never reused
        self.conn.execute("CREATE TABLE IF NOT EXISTS mod_case_seq (guild_id INTEGER PRIMARY KEY, last INTEGER)")
        self.conn.commit()

    # settings
    def log_channel_id(self, guild_id: int) -> int | None:
        row = self.conn.execute("SELECT log_channel_id FROM mod_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return row[0] if row else None

    def set_log_channel(self, guild_id: int, channel_id: int | None):
        self.conn.execute(
            "INSERT INTO mod_settings (guild_id, log_channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def role_ids(self, guild_id: int) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT role_id FROM mod_roles WHERE guild_id = ?", (guild_id,))}

    def add_role(self, guild_id: int, role_id: int) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO mod_roles VALUES (?, ?)", (guild_id, role_id))
        self.conn.commit()
        return cur.rowcount > 0

    def remove_role(self, guild_id: int, role_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM mod_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        self.conn.commit()
        return cur.rowcount > 0

    # cases
    def add_case(self, guild_id: int, action: str, target_id: int, mod_id: int, reason: str | None,
                 extra: dict) -> int:
        """Store a case; case numbers count up per server (never reused, even after deletes)."""
        seq = self.conn.execute("SELECT last FROM mod_case_seq WHERE guild_id = ?", (guild_id,)).fetchone()
        top = self.conn.execute("SELECT MAX(case_no) FROM mod_cases WHERE guild_id = ?", (guild_id,)).fetchone()
        case_no = max(seq[0] if seq else 0, top[0] or 0) + 1
        self.conn.execute("INSERT OR REPLACE INTO mod_case_seq VALUES (?, ?)", (guild_id, case_no))
        self.conn.execute(
            "INSERT INTO mod_cases (guild_id, case_no, action, target_id, mod_id, reason, extra, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, case_no, action, target_id, mod_id, reason, json.dumps(extra), int(time.time())),
        )
        self.conn.commit()
        return case_no

    def set_case_message(self, guild_id: int, case_no: int, channel_id: int, message_id: int):
        self.conn.execute("UPDATE mod_cases SET log_channel_id = ?, log_message_id = ? WHERE guild_id = ? AND case_no = ?",
                          (channel_id, message_id, guild_id, case_no))
        self.conn.commit()

    def get_case(self, guild_id: int, case_no: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM mod_cases WHERE guild_id = ? AND case_no = ?",
                                (guild_id, case_no)).fetchone()
        return dict(row) if row else None

    def cases_for(self, guild_id: int, target_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM mod_cases WHERE guild_id = ? AND target_id = ? ORDER BY case_no DESC",
                                 (guild_id, target_id)).fetchall()
        return [dict(r) for r in rows]

    def update_reason(self, guild_id: int, case_no: int, reason: str) -> bool:
        cur = self.conn.execute("UPDATE mod_cases SET reason = ? WHERE guild_id = ? AND case_no = ?",
                                (reason, guild_id, case_no))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_case(self, guild_id: int, case_no: int) -> bool:
        cur = self.conn.execute("DELETE FROM mod_cases WHERE guild_id = ? AND case_no = ?", (guild_id, case_no))
        self.conn.commit()
        return cur.rowcount > 0


# ---------- Embeds ----------

def case_embed(case: dict) -> discord.Embed:
    """The log-channel card for a case. Rebuilt from the DB so reason edits keep the same look."""
    emoji, past, colour, _ = ACTIONS[case["action"]]
    extra = json.loads(case.get("extra") or "{}")
    e = discord.Embed(
        title=f"{emoji} Case #{case['case_no']} · Member {past}",
        color=colour,
        timestamp=datetime.fromtimestamp(case["created_at"], tz=timezone.utc),
    )
    if extra.get("_mod_tag"):
        e.set_author(name=f"Moderator: {extra['_mod_tag']}", icon_url=extra.get("_mod_avatar"))
    if extra.get("_target_avatar"):
        e.set_thumbnail(url=extra["_target_avatar"])
    tag = f"`{extra['_target_tag']}` · " if extra.get("_target_tag") else ""
    e.add_field(name="User", value=f"<@{case['target_id']}>\n{tag}`{case['target_id']}`")
    e.add_field(name="Moderator", value=f"<@{case['mod_id']}>")
    for name, value in extra.items():
        if not name.startswith("_"):
            e.add_field(name=name, value=value)
    e.add_field(name="Reason", value=case.get("reason") or "*No reason given*", inline=False)
    if "_dm" in extra:
        e.set_footer(text="📨 User notified by DM" if extra["_dm"] else "📭 Couldn't DM user (DMs closed or not in server)")
    return e


def case_line(case: dict) -> str:
    emoji, past, *_ = ACTIONS[case["action"]]
    extra = json.loads(case.get("extra") or "{}")
    dur = f" · {extra['Duration'].split(' (')[0]}" if extra.get("Duration") else ""
    reason = case.get("reason") or "No reason given"
    reason = reason if len(reason) <= 80 else reason[:79] + "…"
    when = discord.utils.format_dt(datetime.fromtimestamp(case["created_at"], tz=timezone.utc), "d")
    return f"`#{case['case_no']}` {emoji} **{past.capitalize()}**{dur} · {when} · by <@{case['mod_id']}>\n> {reason}"


def cases_embed(user: discord.abc.User, cases: list[dict], page: int) -> discord.Embed:
    counts = Counter(c["action"] for c in cases)
    summary = " · ".join(f"{ACTIONS[a][0]} {n} {a}{'s' if n != 1 else ''}" for a, n in counts.most_common())
    e = discord.Embed(title=f"📁 Case history · {user}", color=GOLD)
    e.set_thumbnail(url=user.display_avatar.url)
    if not cases:
        e.description = f"{user.mention} has a clean record. ✨"
        return brand_footer(e)
    pages = max(1, -(-len(cases) // CASES_PER_PAGE))
    chunk = cases[page * CASES_PER_PAGE:(page + 1) * CASES_PER_PAGE]
    e.description = f"{user.mention} · **{len(cases)}** case{'s' if len(cases) != 1 else ''}\n{summary}\n\n" + \
        "\n".join(case_line(c) for c in chunk)
    return brand_footer(e, f"Page {page + 1}/{pages} · /case view <number> for details")


class CasesView(discord.ui.View):
    def __init__(self, user: discord.abc.User, cases: list[dict]):
        super().__init__(timeout=300)
        self.user, self.cases, self.page = user, cases, 0
        self.pages = max(1, -(-len(cases) // CASES_PER_PAGE))
        self._sync()

    def _sync(self):
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=cases_embed(self.user, self.cases, self.page), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=cases_embed(self.user, self.cases, self.page), view=self)


# ---------- Cog ----------

class Moderation(commands.Cog):
    modconfig = app_commands.Group(
        name="modconfig",
        description="Configure moderation: log channel and mod roles",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),  # hidden from non-admins
    )
    case_group = app_commands.Group(name="case", description="Mods: view or edit a moderation case", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = ModDB(DB_PATH)

    async def cog_unload(self):
        self.db.conn.close()

    # ----- permission helpers -----

    def is_mod(self, member: discord.Member, native_perm: str | None = None) -> bool:
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            return True
        if native_perm and getattr(perms, native_perm):
            return True
        mod_roles = self.db.role_ids(member.guild.id)
        return any(r.id in mod_roles for r in member.roles)

    def target_problem(self, mod: discord.Member, target: discord.Member, action: str) -> str | None:
        """Why mod can't act on target, or None if allowed."""
        guild = mod.guild
        if target.id == mod.id:
            return f"You can't {action} yourself."
        if target.id == guild.me.id:
            return f"I can't {action} myself."
        if target.id == guild.owner_id:
            return f"You can't {action} the server owner."
        if mod.id != guild.owner_id:
            if target.top_role >= mod.top_role:
                return f"{target.mention} has an equal or higher role than you."
            if self.is_mod(target) and not mod.guild_permissions.administrator:
                return f"{target.mention} is a moderator. Only admins can {action} moderators."
        if action != "warn" and target.top_role >= guild.me.top_role:
            return f"{target.mention}'s highest role is above or equal to mine. Move my role higher in Server Settings → Roles."
        if action in ("mute", "unmute") and target.guild_permissions.administrator:
            return "Discord doesn't allow timing out administrators."
        return None

    async def check_mod(self, interaction: discord.Interaction, action: str) -> bool:
        """Check caller is a mod and the bot has the needed permission. Replies + returns False if not."""
        native = ACTIONS[action][3]
        if not self.is_mod(interaction.user, native):
            await interaction.response.send_message(
                "You need a moderator role (or the matching Discord permission) to use this.", ephemeral=True
            )
            return False
        if action != "warn" and not getattr(interaction.guild.me.guild_permissions, native):
            await interaction.response.send_message(f"I need the **{nice_perm(native)}** permission to do that.",
                                                    ephemeral=True)
            return False
        return True

    async def require_mod(self, interaction: discord.Interaction) -> bool:
        """For case commands: any moderator."""
        if self.is_mod(interaction.user, "moderate_members"):
            return True
        await interaction.response.send_message("Only moderators can see cases.", ephemeral=True)
        return False

    # ----- notify + log -----

    async def dm_target(self, user: discord.abc.User, guild: discord.Guild, action: str, reason: str | None,
                        duration: str | None = None) -> bool:
        emoji, past, colour, _ = ACTIONS[action]
        desc = f"You were **{past}** {DM_PREP.get(action, 'from')} **{guild.name}**"
        if duration:
            desc += f" for **{duration}**"
        e = discord.Embed(description=f"{emoji} {desc}.", color=colour, timestamp=discord.utils.utcnow())
        e.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
        e.add_field(name="Reason", value=reason or "No reason given", inline=False)
        if action == "warn":
            e.set_footer(text="Please review the server rules. Further issues may lead to stronger action.")
        try:
            await user.send(embed=e)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def log_action(self, guild: discord.Guild, action: str, target: discord.abc.User, mod: discord.Member,
                         reason: str | None, extra: dict[str, str] | None = None,
                         dm_sent: bool | None = None) -> tuple[int, str]:
        """Save a case and post it to the mod log. Returns (case number, note for the moderator)."""
        data = dict(extra or {})
        data.update(_target_tag=str(target), _target_avatar=target.display_avatar.url,
                    _mod_tag=str(mod), _mod_avatar=mod.display_avatar.url)
        if dm_sent is not None:
            data["_dm"] = dm_sent
        case_no = self.db.add_case(guild.id, action, target.id, mod.id, reason, data)

        channel_id = self.db.log_channel_id(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return case_no, f" · Case #{case_no}\n-# No mod log channel set. Use `/modconfig logchannel`."
        try:
            msg = await channel.send(embed=case_embed(self.db.get_case(guild.id, case_no)),
                                     allowed_mentions=discord.AllowedMentions.none())
            self.db.set_case_message(guild.id, case_no, channel.id, msg.id)
            return case_no, f" · Case #{case_no}"
        except (discord.Forbidden, discord.HTTPException):
            return case_no, f" · Case #{case_no}\n-# Couldn't post to {channel.mention}. Check my permissions there."

    async def refresh_log_message(self, guild: discord.Guild, case: dict) -> bool:
        """Re-render a case's log message after an edit. False if it can't be found/edited."""
        if not case.get("log_message_id"):
            return False
        channel = guild.get_channel(case["log_channel_id"])
        if channel is None:
            return False
        try:
            msg = channel.get_partial_message(case["log_message_id"])
            await msg.edit(embed=case_embed(case))
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # ----- /warn -----

    @app_commands.command(name="warn", description="Mods: warn a member (DMs them and saves a case)")
    @app_commands.describe(member="Member to warn", reason="Why (shown in the log and sent to them)")
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Range[str, 1, 500]):
        if not await self.check_mod(interaction, "warn"):
            return
        if problem := self.target_problem(interaction.user, member, "warn"):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        dm = await self.dm_target(member, interaction.guild, "warn", reason)
        prior = len([c for c in self.db.cases_for(interaction.guild.id, member.id) if c["action"] == "warn"])
        extra = {"Warnings": f"{prior + 1} total"}
        _, note = await self.log_action(interaction.guild, "warn", member, interaction.user, reason, extra, dm_sent=dm)
        await interaction.followup.send(f"⚠️ Warned {member.mention} (warning #{prior + 1}).{note}", ephemeral=True)

    # ----- /kick -----

    @app_commands.command(name="kick", description="Mods: kick a member from the server")
    @app_commands.describe(member="Member to kick", reason="Why (shown in the log and sent to them)")
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        if not await self.check_mod(interaction, "kick"):
            return
        if problem := self.target_problem(interaction.user, member, "kick"):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        dm = await self.dm_target(member, interaction.guild, "kick", reason)
        await member.kick(reason=audit_reason(interaction.user, reason))
        _, note = await self.log_action(interaction.guild, "kick", member, interaction.user, reason, dm_sent=dm)
        await interaction.followup.send(f"👢 Kicked {member.mention}.{note}", ephemeral=True)

    # ----- /ban -----

    @app_commands.command(name="ban", description="Mods: ban a user (works by user ID even if they left)")
    @app_commands.describe(
        user="User to ban (or paste a user ID)",
        reason="Why (shown in the log and sent to them)",
        delete_messages="Delete their recent messages",
    )
    @app_commands.choices(delete_messages=DELETE_CHOICES)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, user: discord.Member | discord.User, reason: str | None = None,
                  delete_messages: app_commands.Choice[int] | None = None):
        await self._ban(interaction, user, reason, delete_messages.value if delete_messages else 0, soft=False)

    # ----- /softban -----

    @app_commands.command(name="softban", description="Mods: ban + instantly unban to remove someone and wipe their messages")
    @app_commands.describe(
        user="User to softban (or paste a user ID)",
        reason="Why (shown in the log and sent to them)",
        delete_messages="How far back to delete their messages (default 24 hours)",
    )
    @app_commands.choices(delete_messages=DELETE_CHOICES[1:])
    @app_commands.guild_only()
    async def softban(self, interaction: discord.Interaction, user: discord.Member | discord.User, reason: str | None = None,
                      delete_messages: app_commands.Choice[int] | None = None):
        await self._ban(interaction, user, reason, delete_messages.value if delete_messages else 86400, soft=True)

    async def _ban(self, interaction: discord.Interaction, user: discord.Member | discord.User, reason: str | None,
                   delete_seconds: int, soft: bool):
        action = "softban" if soft else "ban"
        if not await self.check_mod(interaction, action):
            return
        if isinstance(user, discord.Member) and (problem := self.target_problem(interaction.user, user, action)):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        try:  # also stops a softban from accidentally lifting an existing ban
            await guild.fetch_ban(user)
            await interaction.followup.send(f"{user.mention} is already banned.", ephemeral=True)
            return
        except discord.NotFound:
            pass
        dm = await self.dm_target(user, guild, action, reason) if isinstance(user, discord.Member) else False
        await guild.ban(user, reason=audit_reason(interaction.user, reason), delete_message_seconds=delete_seconds)
        if soft:
            await guild.unban(user, reason=audit_reason(interaction.user, f"Softban: {reason or 'No reason given'}"))
        extra = {"Messages deleted": f"Last {fmt_duration(delete_seconds)}" if delete_seconds else "None"}
        _, note = await self.log_action(guild, action, user, interaction.user, reason, extra, dm_sent=dm)
        emoji, past, *_ = ACTIONS[action]
        await interaction.followup.send(f"{emoji} {past.capitalize()} {user.mention}.{note}", ephemeral=True)

    # ----- /unban -----

    @app_commands.command(name="unban", description="Mods: unban a user by ID")
    @app_commands.describe(user="User to unban (paste their user ID)", reason="Why (shown in the log)")
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        if not await self.check_mod(interaction, "unban"):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.guild.unban(user, reason=audit_reason(interaction.user, reason))
        except discord.NotFound:
            await interaction.followup.send(f"{user.mention} isn't banned.", ephemeral=True)
            return
        _, note = await self.log_action(interaction.guild, "unban", user, interaction.user, reason)
        await interaction.followup.send(f"🕊️ Unbanned {user.mention}.{note}", ephemeral=True)

    # ----- /mute -----

    @app_commands.command(name="mute", description="Mods: time out a member (they can't chat, react or join voice)")
    @app_commands.describe(
        member="Member to mute",
        duration="How long, e.g. 10m, 1h, 1h30m, 2d (max 28d). A plain number means minutes",
        reason="Why (shown in the log and sent to them)",
    )
    @app_commands.guild_only()
    async def mute(self, interaction: discord.Interaction, member: discord.Member, duration: str,
                   reason: str | None = None):
        if not await self.check_mod(interaction, "mute"):
            return
        seconds = parse_duration(duration)
        if not seconds or seconds > MAX_TIMEOUT:
            await interaction.response.send_message(
                "Duration must be like `10m`, `1h30m` or `2d`, between 1 second and 28 days.", ephemeral=True
            )
            return
        if problem := self.target_problem(interaction.user, member, "mute"):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        nice = fmt_duration(seconds)
        await member.timeout(timedelta(seconds=seconds), reason=audit_reason(interaction.user, reason))
        dm = await self.dm_target(member, interaction.guild, "mute", reason, duration=nice)
        ends = discord.utils.format_dt(discord.utils.utcnow() + timedelta(seconds=seconds), "R")
        extra = {"Duration": f"{nice} (ends {ends})"}
        _, note = await self.log_action(interaction.guild, "mute", member, interaction.user, reason, extra, dm_sent=dm)
        await interaction.followup.send(f"🔇 Muted {member.mention} for **{nice}**.{note}", ephemeral=True)

    # ----- /unmute -----

    @app_commands.command(name="unmute", description="Mods: remove a member's timeout")
    @app_commands.describe(member="Member to unmute", reason="Why (shown in the log)")
    @app_commands.guild_only()
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
        if not await self.check_mod(interaction, "unmute"):
            return
        if not member.is_timed_out():
            await interaction.response.send_message(f"{member.mention} isn't muted.", ephemeral=True)
            return
        if problem := self.target_problem(interaction.user, member, "unmute"):
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await member.timeout(None, reason=audit_reason(interaction.user, reason))
        _, note = await self.log_action(interaction.guild, "unmute", member, interaction.user, reason)
        await interaction.followup.send(f"🔊 Unmuted {member.mention}.{note}", ephemeral=True)

    # ----- /cases, /case -----

    @app_commands.command(name="cases", description="Mods: show a user's moderation history")
    @app_commands.describe(user="Member (or paste a user ID)")
    @app_commands.guild_only()
    async def cases(self, interaction: discord.Interaction, user: discord.User):
        if not await self.require_mod(interaction):
            return
        cases = self.db.cases_for(interaction.guild.id, user.id)
        view = CasesView(user, cases) if len(cases) > CASES_PER_PAGE else discord.utils.MISSING
        await interaction.response.send_message(embed=cases_embed(user, cases, 0), view=view, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @case_group.command(name="view", description="Show one case")
    @app_commands.describe(number="Case number, e.g. 12")
    async def case_view(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]):
        if not await self.require_mod(interaction):
            return
        case = self.db.get_case(interaction.guild.id, number)
        if case is None:
            await interaction.response.send_message(f"Case #{number} doesn't exist.", ephemeral=True)
            return
        await interaction.response.send_message(embed=case_embed(case), ephemeral=True)

    @case_group.command(name="reason", description="Change a case's reason (also updates the log message)")
    @app_commands.describe(number="Case number", reason="New reason")
    async def case_reason(self, interaction: discord.Interaction, number: app_commands.Range[int, 1],
                          reason: app_commands.Range[str, 1, 500]):
        if not await self.require_mod(interaction):
            return
        guild = interaction.guild
        case = self.db.get_case(guild.id, number)
        if case is None:
            await interaction.response.send_message(f"Case #{number} doesn't exist.", ephemeral=True)
            return
        if case["mod_id"] != interaction.user.id and not interaction.permissions.manage_guild:
            await interaction.response.send_message("Only the moderator who made it (or Manage Server) can edit this case.",
                                                    ephemeral=True)
            return
        self.db.update_reason(guild.id, number, reason)
        updated = await self.refresh_log_message(guild, self.db.get_case(guild.id, number))
        note = "" if updated else "\n-# The original log message couldn't be updated (deleted or no access)."
        await interaction.response.send_message(f"✏️ Updated the reason for case #{number}.{note}", ephemeral=True)

    @case_group.command(name="delete", description="Manage Server: permanently delete a case")
    @app_commands.describe(number="Case number")
    async def case_delete(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]):
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message("You need **Manage Server** to delete cases.", ephemeral=True)
            return
        if not self.db.delete_case(interaction.guild.id, number):
            await interaction.response.send_message(f"Case #{number} doesn't exist.", ephemeral=True)
            return
        await interaction.response.send_message(f"🗑️ Deleted case #{number}. (The log channel message stays.)",
                                                ephemeral=True)

    # ----- /modconfig -----

    @modconfig.command(name="logchannel", description="Set the private channel where mod actions are logged")
    @app_commands.describe(channel="Log channel (leave empty to turn logging off)")
    async def modconfig_logchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if channel is None:
            self.db.set_log_channel(interaction.guild.id, None)
            await interaction.response.send_message("Mod logging turned **off**. Cases are still saved.", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I need **View Channel**, **Send Messages** and **Embed Links** in {channel.mention} first.",
                ephemeral=True,
            )
            return
        self.db.set_log_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"Mod actions will be logged in {channel.mention}.", ephemeral=True)

    @modconfig.command(name="addrole", description="Let a role use warn/kick/ban/softban/mute commands")
    @app_commands.describe(role="Role to treat as moderators")
    async def modconfig_addrole(self, interaction: discord.Interaction, role: discord.Role):
        if role.is_default():
            await interaction.response.send_message("You can't make @everyone a mod role.", ephemeral=True)
            return
        added = self.db.add_role(interaction.guild.id, role.id)
        msg = f"{role.mention} can now use moderation commands." if added else f"{role.mention} is already a mod role."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @modconfig.command(name="removerole", description="Stop a role from using moderation commands")
    @app_commands.describe(role="Role to remove")
    async def modconfig_removerole(self, interaction: discord.Interaction, role: discord.Role):
        removed = self.db.remove_role(interaction.guild.id, role.id)
        msg = f"{role.mention} is no longer a mod role." if removed else f"{role.mention} wasn't a mod role."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @modconfig.command(name="view", description="Show moderation settings and check the bot is set up correctly")
    async def modconfig_view(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.health_embed(interaction.guild), ephemeral=True)

    def health_embed(self, guild: discord.Guild) -> discord.Embed:
        me = guild.me
        problems = 0

        channel_id = self.db.log_channel_id(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            log_line = "⚠️ Not set · use `/modconfig logchannel`"
            problems += 1
        else:
            p = channel.permissions_for(me)
            ok = p.view_channel and p.send_messages and p.embed_links
            log_line = f"{check(ok)} {channel.mention}" + ("" if ok else " · I can't post there")
            problems += not ok

        roles = [guild.get_role(r) for r in self.db.role_ids(guild.id)]
        roles = [r for r in roles if r]
        role_line = " ".join(r.mention for r in roles) or "None yet · only admins / native permissions"

        perm_lines = []
        for perm in ("kick_members", "ban_members", "moderate_members", "manage_channels"):
            ok = getattr(me.guild_permissions, perm)
            problems += not ok
            perm_lines.append(f"{check(ok)} {nice_perm(perm)}")

        top = me.top_role
        mod_reach = [r for r in roles if r >= top]
        if top.position <= 1:
            pos_line = "❌ My role is at the bottom. Drag it up in Server Settings → Roles."
            problems += 1
        elif mod_reach:
            verb, them = ("is", "it") if len(mod_reach) == 1 else ("are", "them")
            pos_line = f"⚠️ {', '.join(r.mention for r in mod_reach)} {verb} above me, so I can't act on members with {them}."
        else:
            pos_line = f"✅ My top role: {top.mention} (I can act on members below it)"

        e = discord.Embed(
            title="🛡️ Moderation setup",
            description="All good! ✅" if problems == 0 else f"**{problems}** thing{'s' if problems != 1 else ''} to fix below.",
            color=OK if problems == 0 else WARN,
        )
        e.add_field(name="📋 Log channel", value=log_line, inline=False)
        e.add_field(name="👮 Mod roles", value=role_line, inline=False)
        e.add_field(name="🔑 My permissions", value="\n".join(perm_lines), inline=True)
        e.add_field(name="📶 Role position", value=pos_line, inline=True)
        return brand_footer(e, "Admins, Manage Server and matching Discord permissions always count as mods")

    # ----- /slowmode -----

    async def apply_slowmode(self, channel, seconds: int, actor: discord.abc.User) -> discord.Embed:
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {actor} ({actor.id})")
        if seconds == 0:
            desc = f"🐇 Slowmode **disabled** in {channel.mention}."
        else:
            desc = f"🐢 Slowmode set to **{fmt_duration(seconds)}** in {channel.mention}."
        return discord.Embed(description=desc, color=GOLD)

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
            msg = f"You need the **{', '.join(map(nice_perm, error.missing_permissions))}** permission to do that."
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f"I need the **{', '.join(map(nice_perm, error.missing_permissions))}** permission here."
        elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
            msg = "Discord refused: I'm missing a permission, or that member's role is above mine."
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
