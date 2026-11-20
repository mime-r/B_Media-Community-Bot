"""Games for B_Media Community Bot: IEMdle (daily puzzle), IEM Cup (voting brackets), /leaderboard.

Game channel: admins pick one with /gamesconfig channel. The bot locks it (members can't post in the channel itself,
only in threads) and opens one thread per game:
- IEMdle: a new thread every UTC day at midnight. Guesses stay private (slash command + autocomplete); finished
  results (spoiler-free grids) are posted in the thread. The next day the thread gets the answer and is locked.
- IEM Cup: each event gets a thread holding all its matches; members vote with buttons and talk in the thread.

IEMdle pool: the whole IEM catalog when the key has catalog access (else the top 150 from /ranking); the answer is a
seeded /random pick among well-known IEMs. Loaded once per UTC day and saved; guesses never call the API.
Vote and Play buttons are DynamicItems, so they keep working after a restart. No message content intent needed.
"""

import asyncio
import hashlib
import json
import logging
import random
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.iemranking import (
    KIND_CHOICES, MEDALS, PRICES, SORTS, TYPE_EMOJI, TYPE_TITLE, APIError, bar, build_profile_embed, clip,
    fmt_price, fmt_score, gate, norm,
)
from cogs.moderation import fmt_duration, parse_duration
from cogs.setups import DB_PATH
from cogs.style import BAD, GAME, GOLD, OK, WARN, brand_footer, check

log = logging.getLogger("B_Media Community Bot")

# ----- IEMdle settings -----
IEMDLE_EPOCH = date(2026, 11, 1)   # puzzle #1
FALLBACK_POOL = 150                # without catalog access: guessable = top N by reviewer score
FALLBACK_ANSWERS = 100             # ...and answers come from the top N
ANSWER_MIN_REVIEWS = 6             # with the catalog: answers are well-known IEMs (this many reviews or more)
NO_REPEAT_DAYS = 60
MAX_TRIES = 6
PAGE = 25                          # max ranking page size
KEEP_POOLS_DAYS = 2                # older days keep only their answer (pools can be ~2,000 IEMs)
RETRY_DAILY = 600                  # seconds before retrying a daily post that failed

# ----- IEM Cup settings -----
CUP_SIZES = (8, 16, 32)
ROUND_NAMES = {1: "Final", 2: "Semifinal", 4: "Quarterfinal"}
MIN_ROUND, MAX_ROUND = 60, 7 * 86400

# ----- Game channel permissions -----
# Members: read and use threads, but can't post in the channel or open their own threads
MEMBER_LOCK = {"send_messages": False, "create_public_threads": False, "create_private_threads": False,
               "send_messages_in_threads": True}
# The bot keeps everything it needs even though @everyone is locked
BOT_ALLOW = {"view_channel": True, "send_messages": True, "embed_links": True, "read_message_history": True,
             "create_public_threads": True, "send_messages_in_threads": True, "manage_threads": True}
BOT_NEEDS = list(BOT_ALLOW) + ["manage_roles"]  # manage_roles on a channel = "Manage Permissions"
PERM_NAMES = {"manage_roles": "Manage Permissions"}

# Product fields worth keeping (pool JSON / match JSON stay small)
KEEP = ("name", "brand", "url", "type", "score", "review_count", "user_score", "user_review_count", "rank",
        "ranked_among", "msrp_usd", "image_url", "tuning")


class PostError(Exception):
    """A game message couldn't be posted (channel gone)."""


def slim(item: dict) -> dict:
    return {k: item.get(k) for k in KEEP if item.get(k) is not None}


def today() -> date:
    return datetime.now(timezone.utc).date()


def puzzle_no(day: date) -> int:
    return (day - IEMDLE_EPOCH).days + 1


def next_reset() -> int:
    return int(datetime.combine(today() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())


def slug(url: str | None) -> str:
    """Product slug from its URL ('…/show/moondrop-chu-ii/' → 'moondrop-chu-ii'), to match across endpoints."""
    return (url or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()


def perm_name(perm: str) -> str:
    return PERM_NAMES.get(perm, perm.replace("_", " ").title())


def bracket_order(n: int) -> list[int]:
    """Standard seeding: 1 meets n, and the top seeds can only meet late (8 → 1,8,4,5,2,7,3,6)."""
    order = [1]
    while len(order) < n:
        total = len(order) * 2 + 1
        order = [x for s in order for x in (s, total - s)]
    return order


# ---------- Storage ----------

class GamesDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS game_points (guild_id INTEGER, user_id INTEGER, game TEXT,"
                  " points INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id, game))")
        c.execute("CREATE TABLE IF NOT EXISTS iemdle_days (day TEXT PRIMARY KEY, pool TEXT, answer_url TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS iemdle_plays (day TEXT, user_id INTEGER, guesses TEXT, solved INTEGER,"
                  " finished INTEGER, guild_id INTEGER, PRIMARY KEY (day, user_id))")
        c.execute("CREATE TABLE IF NOT EXISTS wc_events (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,"
                  " channel_id INTEGER, name TEXT, size INTEGER, round_seconds INTEGER, status TEXT, round INTEGER,"
                  " created_by INTEGER, created_at INTEGER, champion TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS wc_matches (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER,"
                  " round INTEGER, slot INTEGER, a TEXT, b TEXT, message_id INTEGER, ends_at INTEGER, winner TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS wc_votes (match_id INTEGER, user_id INTEGER, choice TEXT,"
                  " PRIMARY KEY (match_id, user_id))")
        c.execute("CREATE TABLE IF NOT EXISTS game_settings (guild_id INTEGER PRIMARY KEY, channel_id INTEGER,"
                  " iemdle_daily INTEGER DEFAULT 1, ping_role_id INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS game_threads (guild_id INTEGER, game TEXT, key TEXT, channel_id INTEGER,"
                  " thread_id INTEGER, closed INTEGER DEFAULT 0, PRIMARY KEY (guild_id, game, key))")
        cols = {r[1] for r in c.execute("PRAGMA table_info(wc_events)")}
        for col in ("thread_id", "announce_id"):  # added with game threads
            if col not in cols:
                c.execute(f"ALTER TABLE wc_events ADD COLUMN {col} INTEGER")
        c.commit()

    # points
    def add_points(self, guild_id: int, user_id: int, game: str, points: int):
        self.conn.execute("INSERT INTO game_points VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id, game)"
                          " DO UPDATE SET points = points + excluded.points", (guild_id, user_id, game, points))
        self.conn.commit()

    def leaderboard(self, guild_id: int, game: str | None) -> list[tuple[int, int]]:
        where, args = ("AND game = ?", (guild_id, game)) if game else ("", (guild_id,))
        rows = self.conn.execute(f"SELECT user_id, SUM(points) p FROM game_points WHERE guild_id = ? {where}"
                                 " GROUP BY user_id HAVING p > 0 ORDER BY p DESC, user_id", args).fetchall()
        return [(r[0], r[1]) for r in rows]

    # game channel settings
    def settings(self, guild_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM game_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def set_settings(self, guild_id: int, channel_id: int, iemdle_daily: bool, ping_role_id: int | None):
        self.conn.execute("INSERT OR REPLACE INTO game_settings VALUES (?, ?, ?, ?)",
                          (guild_id, channel_id, int(iemdle_daily), ping_role_id))
        self.conn.commit()

    def clear_settings(self, guild_id: int):
        self.conn.execute("DELETE FROM game_settings WHERE guild_id = ?", (guild_id,))
        self.conn.commit()

    def daily_guilds(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM game_settings WHERE iemdle_daily = 1")]

    # game threads
    def thread(self, guild_id: int, game: str, key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM game_threads WHERE guild_id = ? AND game = ? AND key = ?",
                                (guild_id, game, key)).fetchone()
        return dict(row) if row else None

    def add_thread(self, guild_id: int, game: str, key: str, channel_id: int, thread_id: int):
        self.conn.execute("INSERT OR REPLACE INTO game_threads VALUES (?, ?, ?, ?, ?, 0)",
                          (guild_id, game, key, channel_id, thread_id))
        self.conn.commit()

    def stale_threads(self, game: str, before_key: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM game_threads WHERE game = ? AND closed = 0 AND key < ?", (game, before_key))]

    def close_thread(self, guild_id: int, game: str, key: str):
        self.conn.execute("UPDATE game_threads SET closed = 1 WHERE guild_id = ? AND game = ? AND key = ?",
                          (guild_id, game, key))
        self.conn.commit()

    # iemdle
    def get_day(self, day: str) -> tuple[list[dict], str] | None:
        row = self.conn.execute("SELECT pool, answer_url FROM iemdle_days WHERE day = ?", (day,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def save_day(self, day: str, pool: list[dict], answer_url: str):
        self.conn.execute("INSERT OR REPLACE INTO iemdle_days VALUES (?, ?, ?)", (day, json.dumps(pool), answer_url))
        self.conn.commit()

    def prune_pools(self, before: str):
        """Old days only need their answer (for no-repeat and the closing reveal)."""
        rows = self.conn.execute("SELECT day, pool, answer_url FROM iemdle_days WHERE day < ? AND length(pool) > 5000",
                                 (before,)).fetchall()
        for day, pool, url in rows:
            keep = [it for it in json.loads(pool) if it.get("url") == url]
            self.conn.execute("UPDATE iemdle_days SET pool = ? WHERE day = ?", (json.dumps(keep), day))
        self.conn.commit()

    def recent_answers(self, since: str) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT answer_url FROM iemdle_days WHERE day >= ?", (since,))}

    def get_play(self, day: str, user_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM iemdle_plays WHERE day = ? AND user_id = ?", (day, user_id)).fetchone()
        if not row:
            return {"guesses": [], "solved": False, "finished": False}
        return {"guesses": json.loads(row["guesses"]), "solved": bool(row["solved"]), "finished": bool(row["finished"])}

    def save_play(self, day: str, user_id: int, play: dict, guild_id: int | None):
        self.conn.execute("INSERT OR REPLACE INTO iemdle_plays VALUES (?, ?, ?, ?, ?, ?)",
                          (day, user_id, json.dumps(play["guesses"]), int(play["solved"]), int(play["finished"]),
                           guild_id))
        self.conn.commit()

    def day_results(self, day: str, guild_id: int) -> tuple[int, int, float | None]:
        """(finished players, solvers, average tries of solvers) for one server."""
        rows = self.conn.execute("SELECT solved, guesses FROM iemdle_plays WHERE day = ? AND guild_id = ?"
                                 " AND finished = 1", (day, guild_id)).fetchall()
        tries = [len(json.loads(r["guesses"])) for r in rows if r["solved"]]
        return len(rows), len(tries), (sum(tries) / len(tries) if tries else None)

    def player_stats(self, user_id: int) -> dict:
        rows = self.conn.execute("SELECT day, solved, guesses FROM iemdle_plays WHERE user_id = ? AND finished = 1"
                                 " ORDER BY day DESC", (user_id,)).fetchall()
        played, wins = len(rows), sum(r["solved"] for r in rows)
        dist = [0] * MAX_TRIES
        for r in rows:
            if r["solved"]:
                dist[len(json.loads(r["guesses"])) - 1] += 1
        # current streak: consecutive solved days ending today (or yesterday if today isn't done yet)
        solved_days = {date.fromisoformat(r["day"]) for r in rows if r["solved"]}
        d = today() if today() in solved_days else today() - timedelta(days=1)
        streak = 0
        while d in solved_days:
            streak += 1
            d -= timedelta(days=1)
        best = run = 0
        prev = None
        for day in sorted(solved_days):
            run = run + 1 if prev and day - prev == timedelta(days=1) else 1
            best, prev = max(best, run), day
        return {"played": played, "wins": wins, "dist": dist, "streak": streak, "best": best}

    # iem cup (tables keep their old wc_ names)
    def running_event(self, guild_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM wc_events WHERE guild_id = ? AND status = 'running'",
                                (guild_id,)).fetchone()
        return dict(row) if row else None

    def latest_event(self, guild_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM wc_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
                                (guild_id,)).fetchone()
        return dict(row) if row else None

    def event(self, event_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM wc_events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def create_event(self, guild_id: int, channel_id: int, name: str, size: int, round_seconds: int,
                     created_by: int) -> int:
        cur = self.conn.execute("INSERT INTO wc_events (guild_id, channel_id, name, size, round_seconds, status, round,"
                                " created_by, created_at) VALUES (?, ?, ?, ?, ?, 'running', 1, ?, ?)",
                                (guild_id, channel_id, name, size, round_seconds, created_by, int(time.time())))
        self.conn.commit()
        return cur.lastrowid

    def set_event(self, event_id: int, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE wc_events SET {cols} WHERE id = ?", (*fields.values(), event_id))
        self.conn.commit()

    def add_match(self, event_id: int, rnd: int, slot: int, a: dict, b: dict, ends_at: int) -> int:
        cur = self.conn.execute("INSERT INTO wc_matches (event_id, round, slot, a, b, ends_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (event_id, rnd, slot, json.dumps(a), json.dumps(b), ends_at))
        self.conn.commit()
        return cur.lastrowid

    def match(self, match_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM wc_matches WHERE id = ?", (match_id,)).fetchone()
        return self._match(row) if row else None

    def matches(self, event_id: int, rnd: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM wc_matches WHERE event_id = ?", [event_id]
        if rnd is not None:
            q, args = q + " AND round = ?", args + [rnd]
        return [self._match(r) for r in self.conn.execute(q + " ORDER BY round, slot", args)]

    def due_matches(self, now: int) -> list[dict]:
        rows = self.conn.execute("SELECT m.* FROM wc_matches m JOIN wc_events e ON e.id = m.event_id"
                                 " WHERE e.status = 'running' AND m.winner IS NULL AND m.ends_at <= ?", (now,))
        return [self._match(r) for r in rows]

    @staticmethod
    def _match(row) -> dict:
        m = dict(row)
        m["a"], m["b"] = json.loads(m["a"]), json.loads(m["b"])
        return m

    def set_match(self, match_id: int, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE wc_matches SET {cols} WHERE id = ?", (*fields.values(), match_id))
        self.conn.commit()

    def vote(self, match_id: int, user_id: int, choice: str) -> str | None:
        """Record a vote; returns the previous choice (None if first vote)."""
        row = self.conn.execute("SELECT choice FROM wc_votes WHERE match_id = ? AND user_id = ?",
                                (match_id, user_id)).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO wc_votes VALUES (?, ?, ?)", (match_id, user_id, choice))
        self.conn.commit()
        return row[0] if row else None

    def tally(self, match_id: int) -> dict[str, int]:
        counts = {"a": 0, "b": 0}
        for choice, n in self.conn.execute("SELECT choice, COUNT(*) FROM wc_votes WHERE match_id = ? GROUP BY choice",
                                           (match_id,)):
            counts[choice] = n
        return counts

    def voters(self, match_id: int, choice: str) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT user_id FROM wc_votes WHERE match_id = ? AND choice = ?",
                                                (match_id, choice))]


# ---------- IEMdle logic ----------

def compare(guess: dict, answer: dict) -> dict:
    """Per-category hint for one guess: {cat: (display, square)}. Arrows point toward the answer."""
    def numeric(g, a, green, yellow, relative=False, lower_is_up=False):
        if g is None or a is None:
            return "❔", "⬛"
        diff = abs(g - a) / a if relative and a else abs(g - a)
        if diff < 1e-9:
            return "✅", "🟩"
        up = (a < g) if lower_is_up else (a > g)
        square = "🟩" if diff <= green else "🟨" if diff <= yellow else "⬛"
        return ("⬆️" if up else "⬇️"), square

    out = {
        "brand": ("✅", "🟩") if norm(guess.get("brand")) == norm(answer.get("brand")) else ("❌", "⬛"),
        "price": numeric(guess.get("msrp_usd"), answer.get("msrp_usd"), 0.10, 0.35, relative=True),
        "score": numeric(guess.get("score"), answer.get("score"), 0.2, 0.6),
        "rank": numeric(guess.get("rank"), answer.get("rank"), 5, 20, lower_is_up=True),
    }
    if guess.get("tuning") and answer.get("tuning"):
        same = norm(str(guess["tuning"])) == norm(str(answer["tuning"]))
        out["tuning"] = ("✅", "🟩") if same else ("❌", "⬛")
    if guess["url"] == answer["url"]:
        out = {k: ("✅", "🟩") for k in out}
    return out


def guess_line(guess: dict, answer: dict) -> str:
    h = compare(guess, answer)
    rank = f"#{guess['rank']}" if guess.get("rank") else "—"
    parts = [
        f"🏷️ {clip(guess.get('brand') or '?', 18)} {h['brand'][0]}",
        f"💰 {fmt_price(guess.get('msrp_usd'))} {h['price'][0]}{h['price'][1]}",
        f"⭐ {fmt_score(guess.get('score'))} {h['score'][0]}{h['score'][1]}",
        f"📊 {rank} {h['rank'][0]}{h['rank'][1]}",
    ]
    if "tuning" in h:
        parts.append(f"🎚️ {h['tuning'][0]}")
    return " · ".join(parts)


def grid(guesses: list[dict], answer: dict) -> str:
    return "\n".join("".join(sq for _, sq in compare(g, answer).values()) for g in guesses)


def rules_text(pool_size: int) -> str:
    if pool_size > FALLBACK_POOL:  # catalog pool
        scope = (f"The answer is a well-known IEM ({ANSWER_MIN_REVIEWS}+ reviews); you can guess any of the "
                 f"{pool_size:,} ranked IEMs.")
    else:
        scope = f"The answer is one of the top {FALLBACK_ANSWERS} IEMs on IEMRanking."
    return (
        f"Guess today's mystery **IEM** in **{MAX_TRIES} tries** with `/iemdle guess`.\n"
        "Each guess shows how close you are:\n"
        "🏷️ brand · 💰 launch price · ⭐ reviewer score · 📊 IEMRanking rank\n"
        "⬆️⬇️ the answer is higher/lower · 🟩 very close · 🟨 close · ⬛ far\n"
        f"-# {scope} Everyone gets the same puzzle."
    )


def board_embed(day: date, play: dict, answer: dict, pool_size: int, stats: dict | None = None) -> discord.Embed:
    no, guesses = puzzle_no(day), play["guesses"]
    e = discord.Embed(title=f"🎯 IEMdle #{no}", color=GAME)
    if play["finished"]:
        if play["solved"]:
            n = len(guesses)
            e.description = f"🎉 **Got it in {n}/{MAX_TRIES}!** +{MAX_TRIES + 1 - n} points"
            e.color = OK
        else:
            e.description = "😵 **Out of guesses!** Better luck tomorrow."
        e.description += f"\nThe answer was **[{answer['name']}]({answer['url']})** · ⭐ {fmt_score(answer.get('score'))}" \
                         f" · 💰 {fmt_price(answer.get('msrp_usd'))}"
        if answer.get("image_url"):
            e.set_thumbnail(url=answer["image_url"])
        if stats:
            e.description += f"\n🔥 Streak **{stats['streak']}** · 🏅 Best {stats['best']} · " \
                             f"✅ {stats['wins']}/{stats['played']} solved"
    elif not guesses:
        e.description = rules_text(pool_size)
    else:
        left = MAX_TRIES - len(guesses)
        e.description = f"**{len(guesses)}/{MAX_TRIES}** guesses used · **{left}** left · keep going with `/iemdle guess`"
    for i, g in enumerate(guesses, 1):
        e.add_field(name=f"{i}. {clip(g['name'], 80)}", value=guess_line(g, answer), inline=False)
    e.add_field(name="​", value=f"⏰ Next puzzle <t:{next_reset()}:R>", inline=False)
    return brand_footer(e, "Data from IEMRanking.com")


def stats_embed(user: discord.abc.User, s: dict) -> discord.Embed:
    e = discord.Embed(title=f"📈 IEMdle stats · {user.display_name}", color=GAME)
    e.set_thumbnail(url=user.display_avatar.url)
    rate = round(s["wins"] / s["played"] * 100) if s["played"] else 0
    e.description = (f"🎮 **{s['played']}** played · ✅ **{rate}%** solved\n"
                     f"🔥 Streak **{s['streak']}** · 🏅 Best **{s['best']}**")
    peak = max(s["dist"]) or 1
    e.add_field(name="Guess distribution", value="\n".join(
        f"`{i}` {'🟩' * max(1 if n else 0, round(n / peak * 10))} {n}" for i, n in enumerate(s["dist"], 1)
    ), inline=False)
    return brand_footer(e)


def daily_embed(day: date, pool_size: int, played: int, solved: int) -> discord.Embed:
    """The starter message of a day's IEMdle thread in the game channel."""
    e = discord.Embed(title=f"🎯 IEMdle #{puzzle_no(day)} · {day:%b %d}", color=GAME)
    e.description = (
        f"{rules_text(pool_size)}\n\n"
        "**Press Play** for your board, then guess with `/iemdle guess` (here or in the thread). "
        "Your guesses are private; finished results are posted in the thread 🧵\n"
        "-# No answers in chat please. Use ||spoiler tags||."
    )
    e.add_field(name="🧩 Today", value=f"**{played}** finished · **{solved}** solved")
    e.add_field(name="⏰ Closes", value=f"<t:{next_reset()}:R>" if day == today() else "Closed")
    return brand_footer(e, "Data from IEMRanking.com")


def closed_embed(day: date, answer: dict | None, played: int, solved: int, avg: float | None) -> discord.Embed:
    e = discord.Embed(title=f"🎯 IEMdle #{puzzle_no(day)} · {day:%b %d} · closed", color=OK)
    lines = []
    if answer:
        lines.append(f"📖 The answer was **[{answer['name']}]({answer['url']})** · ⭐ {fmt_score(answer.get('score'))}"
                     f" · 💰 {fmt_price(answer.get('msrp_usd'))}")
        if answer.get("image_url"):
            e.set_thumbnail(url=answer["image_url"])
    result = f"🧩 **{played}** finished · ✅ **{solved}** solved"
    if avg is not None:
        result += f" · avg **{avg:.1f}** tries"
    lines += [result, "", "A new puzzle is up in the channel! 🎯"]
    e.description = "\n".join(lines)
    return brand_footer(e, "Data from IEMRanking.com")


class ShareGridView(discord.ui.View):
    def __init__(self, text: str):
        super().__init__(timeout=900)
        self.text = text

    @discord.ui.button(label="Share result", emoji="📢", style=discord.ButtonStyle.primary)
    async def share(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(f"{interaction.user.mention}\n{self.text}",
                                        allowed_mentions=discord.AllowedMentions.none())


class IemdleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"iemdle:(?P<action>play|stats)"):
    """Play / My stats on the daily post. Persistent across restarts."""

    def __init__(self, action: str):
        play = action == "play"
        super().__init__(discord.ui.Button(
            label="Play" if play else "My stats", emoji="🎯" if play else "📈",
            style=discord.ButtonStyle.success if play else discord.ButtonStyle.secondary,
            custom_id=f"iemdle:{action}",
        ))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Games")
        if cog is None:
            return
        if self.action == "play":
            await cog.show_board(interaction)
        else:
            await interaction.response.send_message(
                embed=stats_embed(interaction.user, cog.db.player_stats(interaction.user.id)), ephemeral=True)


def iemdle_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(IemdleButton("play"))
    view.add_item(IemdleButton("stats"))
    return view


# ---------- IEM Cup embeds / buttons ----------

def round_name(matches_in_round: int) -> str:
    return ROUND_NAMES.get(matches_in_round, f"Round of {matches_in_round * 2}")


def side_value(item: dict, votes: int, total: int, won: bool | None) -> str:
    pct = round(votes / total * 100) if total else 0
    crown = " 👑" if won else ""
    return (f"⭐ {fmt_score(item.get('score'))} · 💰 {fmt_price(item.get('msrp_usd'))}\n"
            f"🗳️ **{votes}** vote{'s' if votes != 1 else ''} ({pct}%){crown}\n"
            f"{bar(pct / 10, 10)}\n[Reviews ↗]({item['url']})")


def match_embed(event: dict, match: dict, n_in_round: int, tally: dict[str, int]) -> discord.Embed:
    a, b, total = match["a"], match["b"], tally["a"] + tally["b"]
    done = match.get("winner") is not None
    title = f"⚔️ {round_name(n_in_round)}" + (f" {match['slot'] + 1}/{n_in_round}" if n_in_round > 1 else "")
    e = discord.Embed(title=title, color=OK if done else GAME)
    e.set_author(name=f"🏆 {event['name']}")
    if done:
        w = a if match["winner"] == "a" else b
        e.description = f"✅ **{w['name']}** goes through!"
    else:
        e.description = f"Vote for your favourite! Voting ends <t:{match['ends_at']}:R>.\n-# One vote each; tap again to switch."
    e.add_field(name=f"🅰️ {clip(a['name'], 80)}", value=side_value(a, tally["a"], total, done and match["winner"] == "a"))
    e.add_field(name=f"🅱️ {clip(b['name'], 80)}", value=side_value(b, tally["b"], total, done and match["winner"] == "b"))
    img = (a if not done or match["winner"] == "a" else b).get("image_url")
    if img:
        e.set_thumbnail(url=img)
    return brand_footer(e, f"{total} vote{'s' if total != 1 else ''} · Product data from IEMRanking.com")


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"wc:(?P<match>\d+):(?P<side>[ab])"):
    def __init__(self, match_id: int, side: str, label: str = "Vote"):
        super().__init__(discord.ui.Button(
            label=clip(label, 80), emoji="🅰️" if side == "a" else "🅱️",
            style=discord.ButtonStyle.primary if side == "a" else discord.ButtonStyle.danger,
            custom_id=f"wc:{match_id}:{side}",
        ))
        self.match_id, self.side = match_id, side

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["match"]), match["side"])

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Games")
        if cog:
            await cog.on_vote(interaction, self.match_id, self.side)


def vote_view(match: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VoteButton(match["id"], "a", f"Vote {match['a']['name']}"))
    view.add_item(VoteButton(match["id"], "b", f"Vote {match['b']['name']}"))
    return view


# ---------- Cog ----------

GAME_CHOICES = [
    app_commands.Choice(name="All games", value="all"),
    app_commands.Choice(name="IEMdle", value="iemdle"),
    app_commands.Choice(name="IEM Cup predictions", value="worldcup"),  # points key kept from the old name
]


class Games(commands.Cog):
    iemdle = app_commands.Group(name="iemdle", description="Daily guess-the-IEM puzzle")
    iemcup = app_commands.Group(name="iemcup", description="IEM Cup: bracket voting events", guild_only=True)
    gamesconfig = app_commands.Group(
        name="gamesconfig",
        description="Admins: set up the game channel",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),  # hidden from non-admins
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = GamesDB(DB_PATH)
        self.pool_lock = asyncio.Lock()
        self.event_lock = asyncio.Lock()
        self.daily_lock = asyncio.Lock()
        self.pool_cache: dict[str, tuple[list[dict], dict]] = {}
        self.daily_retry: dict[int, float] = {}  # guild_id → don't retry the daily post before this time

    async def cog_load(self):
        self.bot.add_dynamic_items(VoteButton, IemdleButton)
        self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()
        self.bot.remove_dynamic_items(VoteButton, IemdleButton)
        self.db.conn.close()

    @property
    def api(self):
        cog = self.bot.get_cog("IEMRanking")
        if cog is None:
            raise APIError("IEMRanking isn't loaded.")
        return cog.api

    def is_mod(self, member: discord.Member) -> bool:
        mod = self.bot.get_cog("Moderation")
        return mod.is_mod(member) if mod else member.guild_permissions.manage_guild

    async def channel_by_id(self, channel_id: int | None):
        """Cached channel/thread, else fetched (archived threads aren't cached). None if gone."""
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    def ping(self, settings: dict | None, guild: discord.Guild) -> tuple[str | None, discord.AllowedMentions]:
        role = guild.get_role(settings["ping_role_id"]) if settings and settings.get("ping_role_id") else None
        if role is None:
            return None, discord.AllowedMentions.none()
        return role.mention, discord.AllowedMentions(roles=[role], users=False, everyone=False)

    # ===== IEMdle: puzzle =====

    async def load_pool(self) -> list[dict]:
        """Guessable IEMs, best first: the whole catalog if the key has access, else the top of the ranking."""
        try:
            data = await self.api.catalog("iem")
            pool = [{**slim(it), "type": "iem"} for it in data.get("products") or [] if it.get("score") is not None]
            if len(pool) > FALLBACK_POOL:
                return pool
        except APIError as exc:
            log.info("IEMdle: catalog unavailable (%s), using the top %d from /ranking", exc, FALLBACK_POOL)
        pool = []
        for offset in range(0, FALLBACK_POOL, PAGE):
            data = await self.api.get("ranking", ttl=6 * 3600, sort="reviewer-score", type="iem",
                                      limit=PAGE, offset=offset or None)
            results = data.get("results") or []
            pool += [slim(it) for it in results if it.get("score") is not None]
            if len(results) < PAGE:
                break
        return pool

    async def pick_answer(self, day: str, pool: list[dict]) -> tuple[dict, str]:
        """(answer, how it was picked). Seeded /random pick among well-known IEMs (catalog pools),
        else a seeded pick from the top 100."""
        recent = {slug(u) for u in self.db.recent_answers((today() - timedelta(days=NO_REPEAT_DAYS)).isoformat())}
        by_slug = {slug(it["url"]): it for it in pool}
        reason = "no catalog access"
        if len(pool) > FALLBACK_POOL:
            try:
                data = await self.api.random(count=10, seed=f"iemdle-{day}", type="iem",
                                             min_reviews=ANSWER_MIN_REVIEWS)
                results = data.get("results") or []
                for it in results:
                    s = slug(it.get("url"))
                    if s in by_slug and s not in recent:
                        # adds photo + user score; keeps the catalog's url so guesses compare equal
                        by_slug[s].update({k: v for k, v in slim(it).items() if k != "url"})
                        return by_slug[s], "random"
                matched = sum(slug(it.get("url")) in by_slug for it in results)
                reason = f"/random gave {len(results)} picks, {matched} in the catalog, all used recently or unmatched"
            except APIError as exc:
                reason = f"/random failed: {exc}"
        top = pool[:FALLBACK_ANSWERS]
        candidates = [it for it in top if slug(it["url"]) not in recent] or top
        seed = int(hashlib.sha256(f"iemdle-{day}".encode()).hexdigest(), 16)
        return random.Random(seed).choice(candidates), f"fallback ({reason})"

    def cached_puzzle(self) -> tuple[list[dict], dict] | None:
        """Today's puzzle from memory or the database, never the API."""
        day = today().isoformat()
        if day not in self.pool_cache:
            stored = self.db.get_day(day)
            if not stored:
                return None
            pool, answer_url = stored
            answer = next((it for it in pool if it["url"] == answer_url), None)
            if answer is None:
                return None
            self.pool_cache = {day: (pool, answer)}  # drop older days
        return self.pool_cache[day]

    async def today_puzzle(self) -> tuple[list[dict], dict]:
        """(pool, answer) for today, loading it once per UTC day."""
        if cached := self.cached_puzzle():
            return cached
        async with self.pool_lock:
            if cached := self.cached_puzzle():
                return cached
            day = today().isoformat()
            pool = await self.load_pool()
            if len(pool) < 10:
                raise APIError("Couldn't load enough IEMs for today's puzzle. Try again later.")
            answer, how = await self.pick_answer(day, pool)
            log.info("IEMdle %s: answer is %s (%s) · pool of %d IEMs (%s), picked via %s", day, answer["name"],
                     answer["url"], len(pool), "catalog" if len(pool) > FALLBACK_POOL else "top of /ranking", how)
            self.db.save_day(day, pool, answer["url"])
            self.db.prune_pools((today() - timedelta(days=KEEP_POOLS_DAYS)).isoformat())
            self.pool_cache = {day: (pool, answer)}
            return pool, answer

    @staticmethod
    def resolve_guess(pool: list[dict], text: str) -> dict | None:
        q = norm(text)
        if not q:
            return None
        exact = [it for it in pool if norm(it["name"]) == q or norm(f"{it.get('brand', '')}{it['name']}") == q]
        if exact:
            return exact[0]
        partial = [it for it in pool if q in norm(it["name"])]
        return partial[0] if len(partial) == 1 else None

    @staticmethod
    def share_text(play: dict, answer: dict, stats: dict | None) -> str:
        tries = f"{len(play['guesses'])}/{MAX_TRIES}" if play["solved"] else f"X/{MAX_TRIES}"
        streak = f" · 🔥{stats['streak']}" if stats and stats["streak"] else ""
        return f"🎯 **IEMdle #{puzzle_no(today())}** · {tries}{streak}\n{grid(play['guesses'], answer)}\n" \
               f"-# Play with `/iemdle play`"

    def today_thread_row(self, guild: discord.Guild | None) -> dict | None:
        return self.db.thread(guild.id, "iemdle", today().isoformat()) if guild else None

    # ===== IEMdle: commands =====

    async def show_board(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pool, answer = await self.today_puzzle()
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        play = self.db.get_play(today().isoformat(), interaction.user.id)
        stats = self.db.player_stats(interaction.user.id) if play["finished"] else None
        # Servers with a daily thread get results posted there, so no manual share button
        share = play["finished"] and not self.today_thread_row(interaction.guild)
        view = ShareGridView(self.share_text(play, answer, stats)) if share else discord.utils.MISSING
        await interaction.followup.send(embed=board_embed(today(), play, answer, len(pool), stats), view=view,
                                        ephemeral=True)

    @iemdle.command(name="play", description="Show today's IEMdle board and rules")
    async def iemdle_play(self, interaction: discord.Interaction):
        await self.show_board(interaction)

    @iemdle.command(name="guess", description="Guess today's mystery IEM")
    @app_commands.describe(iem="Start typing an IEM name")
    async def iemdle_guess(self, interaction: discord.Interaction, iem: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pool, answer = await self.today_puzzle()
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        day = today().isoformat()
        play = self.db.get_play(day, interaction.user.id)
        if play["finished"]:
            stats = self.db.player_stats(interaction.user.id)
            await interaction.followup.send("You've already finished today's puzzle!",
                                            embed=board_embed(today(), play, answer, len(pool), stats), ephemeral=True)
            return
        guess = self.resolve_guess(pool, iem)
        if guess is None:
            await interaction.followup.send(f"“{clip(iem, 60)}” isn't in today's list. Pick a suggestion from the "
                                            "autocomplete.", ephemeral=True)
            return
        if any(g["url"] == guess["url"] for g in play["guesses"]):
            await interaction.followup.send(f"You already guessed **{guess['name']}**.", ephemeral=True)
            return

        play["guesses"].append(guess)
        play["solved"] = guess["url"] == answer["url"]
        play["finished"] = play["solved"] or len(play["guesses"]) >= MAX_TRIES
        guild_id = interaction.guild.id if interaction.guild else None
        self.db.save_play(day, interaction.user.id, play, guild_id)
        stats, content, view = None, None, discord.utils.MISSING
        if play["finished"]:
            if play["solved"] and guild_id:
                self.db.add_points(guild_id, interaction.user.id, "iemdle", MAX_TRIES + 1 - len(play["guesses"]))
            stats = self.db.player_stats(interaction.user.id)
            thread = await self.post_result(interaction.guild, interaction.user, play, answer, stats, len(pool))
            if thread:
                content = f"🧵 Your result is posted in {thread.mention}."
            else:
                view = ShareGridView(self.share_text(play, answer, stats))
        await interaction.followup.send(content, embed=board_embed(today(), play, answer, len(pool), stats), view=view,
                                        ephemeral=True)

    @iemdle_guess.autocomplete("iem")
    async def iemdle_autocomplete(self, _interaction: discord.Interaction, current: str):
        cached = self.cached_puzzle()  # never fetch from autocomplete; Play or the first guess loads the pool
        if not cached:
            return []
        q = norm(current)
        hits = [it for it in cached[0] if q in norm(it["name"]) or q in norm(f"{it.get('brand', '')} {it['name']}")]
        hits.sort(key=lambda it: (not norm(it["name"]).startswith(q), it["name"]))
        return [app_commands.Choice(name=clip(it["name"], 100), value=clip(it["name"], 100)) for it in hits[:25]]

    @iemdle.command(name="stats", description="Your IEMdle stats")
    async def iemdle_stats(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=stats_embed(interaction.user, self.db.player_stats(interaction.user.id)), ephemeral=True)

    # ===== IEMdle: daily threads =====

    async def post_result(self, guild: discord.Guild | None, user: discord.abc.User, play: dict, answer: dict,
                          stats: dict, pool_size: int) -> discord.Thread | None:
        """Post a finished (spoiler-free) result in today's thread and refresh the counts. None if no thread."""
        row = self.today_thread_row(guild)
        thread = await self.channel_by_id(row["thread_id"]) if row else None
        if thread is None:
            return None
        n = len(play["guesses"])
        streak = f" · 🔥 {stats['streak']}" if stats.get("streak") else ""
        head = f"🎉 {user.mention} solved it in **{n}/{MAX_TRIES}**{streak}" if play["solved"] else \
            f"😵 {user.mention} ran out of guesses (X/{MAX_TRIES})"
        try:
            await thread.send(f"{head}\n{grid(play['guesses'], answer)}", allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return None
        played, solved, _ = self.db.day_results(today().isoformat(), guild.id)
        parent = self.bot.get_channel(row["channel_id"])
        if parent:
            try:
                await parent.get_partial_message(row["thread_id"]).edit(
                    embed=daily_embed(today(), pool_size, played, solved))
            except discord.HTTPException:
                pass
        return thread

    async def post_daily(self, settings: dict) -> discord.Thread | None:
        """Open today's IEMdle thread in a server's game channel (once per day). Raises on failure."""
        async with self.daily_lock:
            day = today().isoformat()
            gid = settings["guild_id"]
            if self.db.thread(gid, "iemdle", day):
                return None
            guild = self.bot.get_guild(gid)
            channel = guild.get_channel(settings["channel_id"]) if guild else None
            if channel is None:
                raise PostError("the game channel no longer exists")
            pool, _ = await self.today_puzzle()
            played, solved, _ = self.db.day_results(day, gid)
            content, mentions = self.ping(settings, guild)
            msg = await channel.send(content=content, embed=daily_embed(today(), len(pool), played, solved),
                                     view=iemdle_view(), allowed_mentions=mentions)
            thread = await msg.create_thread(name=f"🎯 IEMdle #{puzzle_no(today())} · {today():%b %d}",
                                             auto_archive_duration=1440)
            self.db.add_thread(gid, "iemdle", day, channel.id, thread.id)
            await thread.send("🧵 Talk about today's puzzle here. Results show up as people finish.\n"
                              "-# Keep the answer out of chat, or hide it in ||spoiler tags||.")
            return thread

    async def close_day(self, row: dict):
        """Reveal the answer in an old IEMdle thread, update its starter post, lock it."""
        self.db.close_thread(row["guild_id"], "iemdle", row["key"])  # first: never retry in a loop
        stored = self.db.get_day(row["key"])
        answer = next((it for it in stored[0] if it["url"] == stored[1]), None) if stored else None
        played, solved, avg = self.db.day_results(row["key"], row["guild_id"])
        embed = closed_embed(date.fromisoformat(row["key"]), answer, played, solved, avg)
        parent = self.bot.get_channel(row["channel_id"])
        if parent:
            try:
                await parent.get_partial_message(row["thread_id"]).edit(embed=embed, view=None)
            except discord.HTTPException:
                pass
        thread = await self.channel_by_id(row["thread_id"])
        if isinstance(thread, discord.Thread):
            try:
                await thread.send(embed=embed)
                await thread.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass

    async def run_daily(self):
        day = today().isoformat()
        for row in self.db.stale_threads("iemdle", day):
            try:
                await self.close_day(row)
            except Exception:
                log.exception("IEMdle: closing thread %s failed", row["thread_id"])
        now = time.time()
        for settings in self.db.daily_guilds():
            gid = settings["guild_id"]
            if self.daily_retry.get(gid, 0) > now or self.db.thread(gid, "iemdle", day):
                continue
            try:
                await self.post_daily(settings)
            except (APIError, PostError, discord.HTTPException) as exc:
                self.daily_retry[gid] = now + RETRY_DAILY
                log.warning("IEMdle: daily post for guild %s failed: %s", gid, exc)

    # ===== Game channel config =====

    def missing_perms(self, channel: discord.abc.GuildChannel) -> list[str]:
        p = channel.permissions_for(channel.guild.me)
        return [perm_name(n) for n in BOT_NEEDS if not getattr(p, n)]

    async def lock_channel(self, channel: discord.TextChannel):
        guild, reason = channel.guild, "Game channel: members play in threads"
        bot_ow = channel.overwrites_for(guild.me)
        bot_ow.update(**BOT_ALLOW)
        await channel.set_permissions(guild.me, overwrite=bot_ow, reason=reason)  # before locking @everyone
        everyone = channel.overwrites_for(guild.default_role)
        everyone.update(**MEMBER_LOCK)
        await channel.set_permissions(guild.default_role, overwrite=everyone, reason=reason)

    async def unlock_channel(self, channel: discord.TextChannel):
        everyone = channel.overwrites_for(channel.guild.default_role)
        everyone.update(**{k: None for k in MEMBER_LOCK})
        await channel.set_permissions(channel.guild.default_role, overwrite=None if everyone.is_empty() else everyone,
                                      reason="No longer the game channel")

    @gamesconfig.command(name="channel", description="Make a channel the game channel (the bot locks it, games run in threads)")
    @app_commands.describe(
        channel="Channel for IEMdle and IEM Cup threads",
        daily_iemdle="Post a new IEMdle thread every day at 00:00 UTC (default: on)",
        ping_role="Role to ping for each new IEMdle and IEM Cup (optional)",
    )
    async def gc_channel(self, interaction: discord.Interaction, channel: discord.TextChannel,
                         daily_iemdle: bool = True, ping_role: discord.Role | None = None):
        if missing := self.missing_perms(channel):
            await interaction.response.send_message(
                f"I need these permissions in {channel.mention} first: **{', '.join(missing)}**.\n"
                "-# Give my role them in the channel's settings (or server-wide), then try again.", ephemeral=True)
            return
        if ping_role and ping_role.is_default():
            await interaction.response.send_message("Pick a real role to ping, not @everyone.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        old = self.db.settings(guild.id)
        try:
            await self.lock_channel(channel)
            if old and old["channel_id"] != channel.id and (prev := guild.get_channel(old["channel_id"])):
                await self.unlock_channel(prev)
        except discord.Forbidden:
            await interaction.followup.send(f"Discord refused to change {channel.mention}'s permissions. Make sure my "
                                            "role has **Manage Permissions** there.", ephemeral=True)
            return
        self.db.set_settings(guild.id, channel.id, daily_iemdle, ping_role.id if ping_role else None)
        self.daily_retry.pop(guild.id, None)

        notes = []
        if not old or old["channel_id"] != channel.id:
            try:
                await channel.send(embed=self.hub_embed())
            except discord.HTTPException:
                pass
        if daily_iemdle:
            try:
                thread = await self.post_daily(self.db.settings(guild.id))
                existing = self.today_thread_row(guild)
                if thread:
                    notes.append(f"🎯 Today's IEMdle is up: {thread.mention}")
                elif existing:
                    notes.append(f"🎯 Today's IEMdle thread already exists: <#{existing['thread_id']}>")
            except (APIError, PostError, discord.HTTPException) as exc:
                notes.append(f"⚠️ Couldn't post today's IEMdle yet ({exc}). I'll retry automatically.")
        e = discord.Embed(title="🎮 Game channel set", color=OK, description=(
            f"{channel.mention} is now run by the bot:\n"
            "• Members **can't post** in the channel or open threads, but **can chat in game threads**\n"
            f"• IEMdle: {'a new thread every day at 00:00 UTC' if daily_iemdle else 'daily threads off'}\n"
            "• IEM Cups started with `/iemcup start` go here, one thread per event\n"
            f"• Ping role: {ping_role.mention if ping_role else 'none'}"
        ))
        if notes:
            e.add_field(name="Now", value="\n".join(notes), inline=False)
        e.add_field(name="Good to know", inline=False, value=(
            "• Staff without **Administrator** are locked out too. Add a channel override for your mod role if "
            "they should post there.\n• Members can still use slash commands in the channel; replies to private "
            "game commands are only visible to them."
        ))
        await interaction.followup.send(embed=brand_footer(e), ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    def hub_embed(self) -> discord.Embed:
        e = discord.Embed(title="🎮 Game channel", color=GAME, description=(
            "This channel is run by the bot. Every game gets its own **thread**: open it to play and chat.\n\n"
            f"🎯 **IEMdle**: a new mystery IEM every day at <t:{next_reset()}:t> (00:00 UTC). Guess privately, "
            "share your grid in the thread.\n"
            "🏆 **IEM Cup**: bracket tournaments; vote on every match.\n"
            "🏅 `/leaderboard`: points from both."
        ))
        return brand_footer(e, "Data from IEMRanking.com")

    @gamesconfig.command(name="off", description="Stop using a game channel and unlock it")
    async def gc_off(self, interaction: discord.Interaction):
        settings = self.db.settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message("No game channel is set.", ephemeral=True)
            return
        self.db.clear_settings(interaction.guild.id)
        channel = interaction.guild.get_channel(settings["channel_id"])
        msg = "🎮 Game channel turned off. No more daily IEMdle threads."
        if channel:
            try:
                await self.unlock_channel(channel)
                msg += f" {channel.mention} is unlocked for members again."
            except discord.HTTPException:
                msg += f"\n⚠️ I couldn't unlock {channel.mention}; reset its @everyone permissions manually."
        await interaction.response.send_message(msg, ephemeral=True)

    @gamesconfig.command(name="answer", description="Admins: privately see today's IEMdle answer")
    async def gc_answer(self, interaction: discord.Interaction):
        # Hidden via default_permissions, but servers can override that, so check again
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only admins (Manage Server) can see the answer.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pool, answer = await self.today_puzzle()
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        log.info("IEMdle answer viewed by %s (%s) in guild %s", interaction.user, interaction.user.id,
                 interaction.guild.id)
        rank = f"#{answer['rank']:,}" if answer.get("rank") else "—"
        e = discord.Embed(title=f"🔒 IEMdle #{puzzle_no(today())} answer", color=GAME, description=(
            f"||**[{answer['name']}]({answer['url']})**||\n"
            f"🏷️ ||{answer.get('brand') or '?'}|| · 💰 ||{fmt_price(answer.get('msrp_usd'))}|| · "
            f"⭐ ||{fmt_score(answer.get('score'))}|| · 📊 ||{rank}||\n\n"
            f"-# Hidden in spoilers in case you're screen sharing. Only you can see this. "
            f"Next puzzle <t:{next_reset()}:R>."
        ))
        if not self.db.get_play(today().isoformat(), interaction.user.id)["finished"]:
            e.add_field(name="😇 Play fair", value="You haven't finished today's puzzle. You can still play, "
                                                  "but you know the answer now!", inline=False)
        await interaction.followup.send(embed=brand_footer(e), ephemeral=True)

    @gamesconfig.command(name="view", description="Show game channel settings and check my permissions")
    async def gc_view(self, interaction: discord.Interaction):
        guild = interaction.guild
        settings = self.db.settings(guild.id)
        e = discord.Embed(title="🎮 Game channel setup", color=GAME)
        channel = guild.get_channel(settings["channel_id"]) if settings else None
        if not settings:
            e.description = "Not set. Use `/gamesconfig channel` to pick one."
            e.color = WARN
        elif channel is None:
            e.description = "⚠️ The game channel was deleted. Pick a new one with `/gamesconfig channel`."
            e.color = BAD
        else:
            missing = self.missing_perms(channel)
            everyone = channel.overwrites_for(guild.default_role)
            locked = everyone.send_messages is False and everyone.create_public_threads is False
            threads_ok = everyone.send_messages_in_threads is not False
            role = guild.get_role(settings["ping_role_id"]) if settings.get("ping_role_id") else None
            row = self.today_thread_row(guild)
            cup = self.db.running_event(guild.id)
            e.description = "\n".join([
                f"📺 Channel: {channel.mention}",
                f"{check(locked)} Members can't post in the channel" + ("" if locked else " · run `/gamesconfig channel` again"),
                f"{check(threads_ok)} Members can chat in threads",
                f"{check(not missing)} My permissions" + (f" · missing **{', '.join(missing)}**" if missing else ""),
                f"🎯 Daily IEMdle: {'on' if settings['iemdle_daily'] else 'off'}"
                + (f" · today: <#{row['thread_id']}>" if row else ""),
                f"🏆 IEM Cup: " + (f"**{cup['name']}** running" + (f" in <#{cup['thread_id']}>" if cup.get('thread_id') else "")
                                  if cup else "none running"),
                f"🔔 Ping role: {role.mention if role else 'none'}"
                + (" · ⚠️ not mentionable and I lack **Mention Everyone**"
                   if role and not role.mentionable and not channel.permissions_for(guild.me).mention_everyone else ""),
            ])
            e.color = OK if locked and threads_ok and not missing else WARN
        await interaction.response.send_message(embed=brand_footer(e), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    # ===== IEM Cup =====

    async def event_target(self, event: dict):
        """Where an event's matches go: its thread, else the channel (events from before threads)."""
        if event.get("thread_id"):
            return await self.channel_by_id(event["thread_id"])
        return await self.channel_by_id(event["channel_id"])

    async def mark_announcement(self, event: dict, name: str, value: str, color: discord.Color):
        """Add a final line (champion / cancelled) to the event's announcement post."""
        parent = self.bot.get_channel(event["channel_id"])
        if not parent or not event.get("announce_id"):
            return
        try:
            msg = await parent.fetch_message(event["announce_id"])
            e = msg.embeds[0] if msg.embeds else discord.Embed(title=f"🏆 {event['name']}")
            e.add_field(name=name, value=value, inline=False)
            e.color = color
            await msg.edit(embed=e)
        except discord.HTTPException:
            pass

    @iemcup.command(name="start", description="Mods: start an IEM Cup bracket members vote on")
    @app_commands.describe(
        name="Event name, e.g. 'Budget IEM Cup'",
        size="Number of entrants",
        round_length="How long each round's voting lasts, e.g. 1d, 12h, 30m",
        fill="Top of a ranking list, or a random draw",
        sort="Top of a list: which IEMRanking list",
        kind="IEMs or headphones",
        price="Price range filter",
        tuning="Tuning filter, e.g. neutral, warm",
        min_reviews="Only products with at least this many reviews (keeps it to well-known ones)",
        entries="Or list the entrants yourself, comma-separated (must match size)",
        channel="Where to post (default: the game channel, else here)",
    )
    @app_commands.rename(kind="type")
    @app_commands.choices(
        size=[app_commands.Choice(name=f"{n} entrants ({n.bit_length() - 1} rounds)", value=n) for n in CUP_SIZES],
        fill=[app_commands.Choice(name="Top of a ranking list", value="top"),
              app_commands.Choice(name="Random draw", value="random")],
        sort=[app_commands.Choice(name=label, value=value) for value, label in SORTS.items()][:25],
        kind=KIND_CHOICES,
        price=[app_commands.Choice(name=label, value=value) for value, label in PRICES.items()],
    )
    async def cup_start(self, interaction: discord.Interaction, name: app_commands.Range[str, 3, 60], size: int,
                        round_length: str = "1d", fill: str = "top", sort: str = "reviewer-score", kind: str = "iem",
                        price: str | None = None, tuning: str | None = None,
                        min_reviews: app_commands.Range[int, 1, 50] | None = None, entries: str | None = None,
                        channel: discord.TextChannel | None = None):
        if not self.is_mod(interaction.user):
            await interaction.response.send_message("Only moderators can start an IEM Cup.", ephemeral=True)
            return
        seconds = parse_duration(round_length)
        if not seconds or not MIN_ROUND <= seconds <= MAX_ROUND:
            await interaction.response.send_message("Round length must be between `1m` and `7d`, e.g. `1d` or `12h`.",
                                                    ephemeral=True)
            return
        settings = self.db.settings(interaction.guild.id)
        if channel is None and settings:
            channel = interaction.guild.get_channel(settings["channel_id"])
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        use_thread = isinstance(channel, discord.TextChannel) and perms.create_public_threads \
            and perms.send_messages_in_threads
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(f"I need **Send Messages** and **Embed Links** in {channel.mention}.",
                                                    ephemeral=True)
            return
        if self.db.running_event(interaction.guild.id):
            await interaction.response.send_message("An IEM Cup is already running here. Use `/iemcup status` or "
                                                    "`/iemcup cancel`.", ephemeral=True)
            return
        names = [n.strip() for n in entries.split(",") if n.strip()] if entries else []
        if entries and len(names) != size:
            await interaction.response.send_message(f"You listed {len(names)} entrants but picked size {size}.",
                                                    ephemeral=True)
            return
        if not await gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        filters = {"type": kind, "price": price, "tuning": tuning, "min_reviews": min_reviews}
        try:
            if names:
                source = "manual"
                ranking_cog = self.bot.get_cog("IEMRanking")
                field, missing = [], []
                for n in names:
                    results, _ = await ranking_cog.find(n)
                    (field.append(slim(results[0])) if results else missing.append(n))
                if missing:
                    await interaction.followup.send("🔍 Couldn't find: " + ", ".join(f"“{clip(m, 40)}”" for m in missing),
                                                    ephemeral=True)
                    return
                if len({it["url"] for it in field}) < size:
                    await interaction.followup.send("Some entries point to the same product. Use more specific names.",
                                                    ephemeral=True)
                    return
            elif fill == "random":
                source = "random"
                data = await self.api.random(count=size, seed=f"iemcup-{interaction.guild.id}-{int(time.time())}",
                                             **filters)
                field = [slim(it) for it in data.get("results") or []]
                field.sort(key=lambda it: it.get("score") or 0, reverse=True)  # seeds by reviewer score
            else:
                source = "top"
                field = []
                for offset in range(0, size, PAGE):
                    data = await self.api.ranking(sort=sort, limit=min(PAGE, size - offset), offset=offset or None,
                                                  **filters)
                    results = data.get("results") or []
                    field += [slim(it) for it in results]
                    if len(results) < min(PAGE, size - offset):
                        break
            field = field[:size]
            if len(field) < size:
                await interaction.followup.send(f"Only {len(field)} products match those filters; need {size}.",
                                                ephemeral=True)
                return
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        async with self.event_lock:
            if self.db.running_event(interaction.guild.id):
                await interaction.followup.send("An IEM Cup was just started by someone else.", ephemeral=True)
                return
            event_id = self.db.create_event(interaction.guild.id, channel.id, name, size, seconds, interaction.user.id)
            event = self.db.event(event_id)
            seeded = [field[s - 1] for s in bracket_order(size)]
            content, mentions = self.ping(settings, interaction.guild)
            try:
                announce = await channel.send(content=content, allowed_mentions=mentions, embed=self.announce_embed(
                    event, field, source, sort, kind, price, tuning, min_reviews, use_thread))
                thread = await announce.create_thread(name=clip(f"🏆 {name}", 100), auto_archive_duration=10080) \
                    if use_thread else None
                self.db.set_event(event_id, announce_id=announce.id, thread_id=thread.id if thread else None)
                event = self.db.event(event_id)
                await self.post_round(event, 1, [(seeded[i], seeded[i + 1]) for i in range(0, size, 2)])
            except (discord.HTTPException, PostError) as exc:
                self.db.set_event(event_id, status="cancelled")
                await interaction.followup.send(f"Couldn't post in {channel.mention}: {exc}", ephemeral=True)
                return
        where = thread.mention if thread else channel.mention
        await interaction.followup.send(f"🏆 **{name}** is live in {where}! Rounds last {fmt_duration(seconds)}.",
                                        ephemeral=True)

    @cup_start.autocomplete("tuning")
    async def cup_tuning_autocomplete(self, interaction: discord.Interaction, current: str):
        cog = self.bot.get_cog("IEMRanking")
        return await cog.tuning_autocomplete(interaction, current) if cog else []

    def announce_embed(self, event: dict, field: list[dict], source: str, sort: str, kind: str, price: str | None,
                       tuning: str | None, min_reviews: int | None, in_thread: bool) -> discord.Embed:
        e = discord.Embed(title=f"🏆 {event['name']}", color=GOLD)
        extras = [PRICES[price]] if price else []
        if tuning:
            extras.append(f"tuning: {tuning}")
        if min_reviews:
            extras.append(f"{min_reviews}+ reviews")
        if source == "manual":
            origin = "Hand-picked by the mods"
        elif source == "random":
            origin = f"Random draw of {TYPE_TITLE.get(kind, kind)}"
        else:
            origin = f"Top {event['size']} {TYPE_TITLE.get(kind, kind)} by {SORTS.get(sort, sort).lower()}"
        if source != "manual" and extras:
            origin += " · " + " · ".join(extras)
        rounds = event["size"].bit_length() - 1
        where = "Open the **thread** below to vote on every match" if in_thread else "Vote on each match below"
        e.description = (f"**{event['size']} entrants · {rounds} rounds · {fmt_duration(event['round_seconds'])} per round**\n"
                         f"{origin}\n\n{where}. Correct picks earn prediction points on `/leaderboard`! 🗳️")
        e.add_field(name="Seeds", value="\n".join(
            f"`{i:>2}` {TYPE_EMOJI.get(it.get('type'), '🎧')} {clip(it['name'], 40)} · ⭐ {fmt_score(it.get('score'))}"
            for i, it in enumerate(field, 1)
        )[:1024], inline=False)
        return brand_footer(e, "Product data from IEMRanking.com")

    async def post_round(self, event: dict, rnd: int, pairs: list[tuple[dict, dict]]):
        target = await self.event_target(event)
        if target is None:
            raise PostError("the event's channel or thread no longer exists")
        ends_at = int(time.time()) + event["round_seconds"]
        self.db.set_event(event["id"], round=rnd)
        if len(pairs) > 1:
            await target.send(f"## 🔔 {round_name(len(pairs))}s are open! Voting ends <t:{ends_at}:R>.")
        for slot, (a, b) in enumerate(pairs):
            match_id = self.db.add_match(event["id"], rnd, slot, a, b, ends_at)
            match = self.db.match(match_id)
            msg = await target.send(embed=match_embed(event, match, len(pairs), {"a": 0, "b": 0}), view=vote_view(match))
            self.db.set_match(match_id, message_id=msg.id)

    async def on_vote(self, interaction: discord.Interaction, match_id: int, side: str):
        match = self.db.match(match_id)
        event = self.db.event(match["event_id"]) if match else None
        if not match or not event or event["status"] != "running" or match["winner"] or match["ends_at"] <= time.time():
            await interaction.response.send_message("Voting for this match has closed.", ephemeral=True)
            return
        prev = self.db.vote(match_id, interaction.user.id, side)
        n_in_round = len(self.db.matches(event["id"], match["round"]))
        await interaction.response.edit_message(embed=match_embed(event, match, n_in_round, self.db.tally(match_id)))
        pick = match[side]["name"]
        msg = f"🗳️ Voted for **{pick}**!" if prev is None else \
            ("You already voted for this one." if prev == side else f"🔁 Switched your vote to **{pick}**.")
        await interaction.followup.send(msg, ephemeral=True)

    @tasks.loop(minutes=1)
    async def ticker(self):
        try:
            await self.close_due_matches()
        except Exception:
            log.exception("IEM Cup ticker failed")
        try:
            await self.run_daily()
        except Exception:
            log.exception("IEMdle daily ticker failed")

    @ticker.before_loop
    async def before_ticker(self):
        await self.bot.wait_until_ready()

    async def close_due_matches(self, now: int | None = None):
        now = now or int(time.time())
        touched = set()
        for match in self.db.due_matches(now):
            tally = self.db.tally(match["id"])
            if tally["a"] != tally["b"]:
                winner = "a" if tally["a"] > tally["b"] else "b"
            else:  # tie → higher reviewer score, then the higher seed (a)
                winner = "b" if (match["b"].get("score") or 0) > (match["a"].get("score") or 0) else "a"
            self.db.set_match(match["id"], winner=winner)
            match["winner"] = winner
            event = self.db.event(match["event_id"])
            for uid in self.db.voters(match["id"], winner):
                self.db.add_points(event["guild_id"], uid, "worldcup", 1)
            await self.edit_match_message(event, match, tally)
            touched.add(event["id"])
        for event_id in touched:
            await self.advance(self.db.event(event_id))

    async def edit_match_message(self, event: dict, match: dict, tally: dict):
        target = await self.event_target(event)
        if target is None or not match.get("message_id"):
            return
        n = len(self.db.matches(event["id"], match["round"]))
        try:
            await target.get_partial_message(match["message_id"]).edit(embed=match_embed(event, match, n, tally),
                                                                         view=None)
        except discord.HTTPException:
            pass

    async def advance(self, event: dict):
        """If every match in the current round is decided: post the next round, or crown the champion."""
        if event["status"] != "running":
            return
        matches = self.db.matches(event["id"], event["round"])
        if not matches or any(m["winner"] is None for m in matches):
            return
        winners = [m[m["winner"]] for m in matches]
        if len(winners) == 1:
            champ = winners[0]
            self.db.set_event(event["id"], status="done", champion=json.dumps(champ))
            target = await self.event_target(event)
            if target:
                e = build_profile_embed(champ)
                e.title = f"👑 {champ['name']}"
                e.description = f"🏆 **Champion of {event['name']}!**\n\n" + (e.description or "")
                try:
                    await target.send(content="# 🎉 We have a champion!", embed=e)
                except discord.HTTPException:
                    pass
            await self.mark_announcement(event, "👑 Champion", f"**[{champ['name']}]({champ['url']})**", OK)
            return
        pairs = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
        try:
            await self.post_round(event, event["round"] + 1, pairs)
        except (discord.HTTPException, PostError):
            log.warning("IEM Cup %s: couldn't post next round, cancelling", event["id"])
            self.db.set_event(event["id"], status="cancelled")

    @iemcup.command(name="status", description="Show the current (or last) IEM Cup bracket")
    async def cup_status(self, interaction: discord.Interaction):
        event = self.db.running_event(interaction.guild.id) or self.db.latest_event(interaction.guild.id)
        if not event:
            await interaction.response.send_message("No IEM Cup yet. Mods can start one with `/iemcup start`.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(embed=self.bracket_embed(event), ephemeral=True)

    def bracket_embed(self, event: dict) -> discord.Embed:
        status = {"running": f"🟢 Live · round {event['round']}", "done": "🏁 Finished",
                  "cancelled": "⛔ Cancelled"}[event["status"]]
        if event.get("thread_id"):
            status += f" · <#{event['thread_id']}>"
        e = discord.Embed(title=f"🏆 {event['name']}", description=status, color=GOLD)
        by_round: dict[int, list[dict]] = {}
        for m in self.db.matches(event["id"]):
            by_round.setdefault(m["round"], []).append(m)
        for ms in by_round.values():
            width = 28 if len(ms) <= 8 else 18
            lines = []
            for m in ms:
                a, b = clip(m["a"]["name"], width), clip(m["b"]["name"], width)
                if m["winner"] == "a":
                    lines.append(f"**{a}** ✅ vs ~~{b}~~")
                elif m["winner"] == "b":
                    lines.append(f"~~{a}~~ vs **{b}** ✅")
                else:
                    t = self.db.tally(m["id"])
                    lines.append(f"{a} `{t['a']}` vs `{t['b']}` {b}")
            e.add_field(name=round_name(len(ms)), value="\n".join(lines)[:1024], inline=False)
        if event.get("champion"):
            e.add_field(name="👑 Champion", value=json.loads(event["champion"])["name"], inline=False)
        return brand_footer(e)

    @iemcup.command(name="cancel", description="Mods: stop the running IEM Cup")
    async def cup_cancel(self, interaction: discord.Interaction):
        if not self.is_mod(interaction.user):
            await interaction.response.send_message("Only moderators can cancel an IEM Cup.", ephemeral=True)
            return
        event = self.db.running_event(interaction.guild.id)
        if not event:
            await interaction.response.send_message("No IEM Cup is running.", ephemeral=True)
            return
        self.db.set_event(event["id"], status="cancelled")
        await interaction.response.send_message(f"⛔ **{event['name']}** cancelled.", ephemeral=True)
        target = await self.event_target(event)
        if target:
            for m in self.db.matches(event["id"], event["round"]):
                if m["winner"] is None and m.get("message_id"):
                    try:
                        await target.get_partial_message(m["message_id"]).edit(view=None)
                    except discord.HTTPException:
                        pass
            if isinstance(target, discord.Thread):
                try:
                    await target.send("⛔ This IEM Cup was cancelled by the mods.")
                    await target.edit(archived=True, locked=True)
                except discord.HTTPException:
                    pass
        await self.mark_announcement(event, "⛔ Cancelled", "Stopped by the mods.", BAD)

    # ===== Leaderboard =====

    @app_commands.command(name="leaderboard", description="Top players in this server (IEMdle + IEM Cup)")
    @app_commands.describe(game="Which game (default: all)")
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction, game: str = "all"):
        rows = self.db.leaderboard(interaction.guild.id, None if game == "all" else game)
        label = next(c.name for c in GAME_CHOICES if c.value == game)
        e = discord.Embed(title=f"🏅 Leaderboard · {label}", color=GAME)
        if interaction.guild.icon:
            e.set_thumbnail(url=interaction.guild.icon.url)
        if not rows:
            e.description = "No points yet! Play `/iemdle play` or vote in an IEM Cup."
            await interaction.response.send_message(embed=brand_footer(e))
            return
        lines = [f"{MEDALS.get(i, f'`{i:>2}.`')} <@{uid}> · **{pts:,}** pt{'s' if pts != 1 else ''}"
                 for i, (uid, pts) in enumerate(rows[:10], 1)]
        e.description = "\n".join(lines)
        me = next(((i, p) for i, (uid, p) in enumerate(rows, 1) if uid == interaction.user.id), None)
        footer = f"You: #{me[0]} · {me[1]:,} pts" if me else "You're not on the board yet"
        await interaction.response.send_message(embed=brand_footer(e, f"{footer} · IEMdle: 7 − tries · IEM Cup: 1 per correct vote"),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
