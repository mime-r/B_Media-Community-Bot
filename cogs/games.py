"""Games for B_Media Community Bot: IEMdle (daily puzzle), IEM World Cup (voting brackets), /leaderboard.

Everything runs on slash commands, autocomplete and buttons (no message content needed).
IEMdle loads a pool of top IEMs from IEMRanking once per day; guesses never call the API.
World Cup vote buttons are DynamicItems, so they keep working after a restart.
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
from cogs.style import GAME, GOLD, OK, brand_footer

log = logging.getLogger("B_Media Community Bot")

# ----- IEMdle settings -----
IEMDLE_EPOCH = date(2026, 11, 1)   # puzzle #1
POOL_SIZE = 150                    # guessable IEMs (top N by reviewer score)
ANSWER_POOL = 100                  # answers come from the top N (more recognisable)
NO_REPEAT_DAYS = 60
MAX_TRIES = 6
PAGE = 25                          # ranking page size the API reliably serves

# ----- World Cup settings -----
SEED_ORDER = {
    8: [1, 8, 4, 5, 2, 7, 3, 6],
    16: [1, 16, 8, 9, 4, 13, 5, 12, 2, 15, 7, 10, 3, 14, 6, 11],
}
ROUND_NAMES = {1: "Final", 2: "Semifinal", 4: "Quarterfinal", 8: "Round of 16"}
MIN_ROUND, MAX_ROUND = 60, 7 * 86400

# Product fields worth keeping (pool JSON / match JSON stay small)
KEEP = ("name", "brand", "url", "type", "score", "review_count", "user_score", "user_review_count", "rank",
        "ranked_among", "msrp_usd", "image_url", "tuning")


class PostError(Exception):
    """A World Cup message couldn't be posted (channel gone)."""


def slim(item: dict) -> dict:
    return {k: item.get(k) for k in KEEP if item.get(k) is not None}


def today() -> date:
    return datetime.now(timezone.utc).date()


def puzzle_no(day: date) -> int:
    return (day - IEMDLE_EPOCH).days + 1


def next_reset() -> int:
    return int(datetime.combine(today() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())


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

    # iemdle
    def get_day(self, day: str) -> tuple[list[dict], str] | None:
        row = self.conn.execute("SELECT pool, answer_url FROM iemdle_days WHERE day = ?", (day,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def save_day(self, day: str, pool: list[dict], answer_url: str):
        self.conn.execute("INSERT OR REPLACE INTO iemdle_days VALUES (?, ?, ?)", (day, json.dumps(pool), answer_url))
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

    # world cup
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


IEMDLE_RULES = (
    "Guess today's mystery **IEM** in **6 tries** with `/iemdle guess`.\n"
    "Each guess shows how close you are:\n"
    "🏷️ brand · 💰 launch price · ⭐ reviewer score · 📊 IEMRanking rank\n"
    "⬆️⬇️ the answer is higher/lower · 🟩 very close · 🟨 close · ⬛ far\n"
    "-# The answer is one of the top 100 IEMs on IEMRanking. Everyone gets the same puzzle."
)


def board_embed(day: date, play: dict, answer: dict, stats: dict | None = None) -> discord.Embed:
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
        e.description = IEMDLE_RULES
    else:
        e.description = f"Guess **{len(guesses) + 1}/{MAX_TRIES}** · use `/iemdle guess`"
    for i, g in enumerate(guesses, 1):
        e.add_field(name=f"{i}. {clip(g['name'], 80)}", value=guess_line(g, answer), inline=False)
    e.add_field(name="​", value=f"⏰ Next puzzle <t:{next_reset()}:R>", inline=False)
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


# ---------- World Cup embeds / buttons ----------

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
    app_commands.Choice(name="World Cup predictions", value="worldcup"),
]


class Games(commands.Cog):
    iemdle = app_commands.Group(name="iemdle", description="Daily guess-the-IEM puzzle")
    worldcup = app_commands.Group(name="worldcup", description="IEM World Cup: bracket voting events", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = GamesDB(DB_PATH)
        self.pool_lock = asyncio.Lock()
        self.event_lock = asyncio.Lock()
        self.pool_cache: dict[str, tuple[list[dict], dict]] = {}

    async def cog_load(self):
        self.bot.add_dynamic_items(VoteButton)
        self.ticker.start()

    async def cog_unload(self):
        self.ticker.cancel()
        self.bot.remove_dynamic_items(VoteButton)
        self.db.conn.close()

    @property
    def api(self):
        cog = self.bot.get_cog("IEMRanking")
        if cog is None:
            raise APIError("IEMRanking isn't loaded.")
        return cog.api

    # ===== IEMdle =====

    async def today_puzzle(self) -> tuple[list[dict], dict]:
        """(pool, answer) for today, fetching the pool once per day (6 API calls)."""
        day = today().isoformat()
        if day in self.pool_cache:
            return self.pool_cache[day]
        async with self.pool_lock:
            if day in self.pool_cache:
                return self.pool_cache[day]
            stored = self.db.get_day(day)
            if stored:
                pool, answer_url = stored
            else:
                pool = []
                for offset in range(0, POOL_SIZE, PAGE):
                    data = await self.api.get("ranking", ttl=6 * 3600, sort="reviewer-score", type="iem",
                                              limit=PAGE, offset=offset or None)
                    pool += [slim(it) for it in data.get("results") or [] if it.get("score") is not None]
                    if len(data.get("results") or []) < PAGE:
                        break
                if len(pool) < 10:
                    raise APIError("Couldn't load enough IEMs for today's puzzle. Try again later.")
                since = (today() - timedelta(days=NO_REPEAT_DAYS)).isoformat()
                recent = self.db.recent_answers(since)
                candidates = [it for it in pool[:ANSWER_POOL] if it["url"] not in recent] or pool[:ANSWER_POOL]
                seed = int(hashlib.sha256(f"iemdle-{day}".encode()).hexdigest(), 16)
                answer_url = random.Random(seed).choice(candidates)["url"]
                self.db.save_day(day, pool, answer_url)
            answer = next(it for it in pool if it["url"] == answer_url)
            self.pool_cache = {day: (pool, answer)}  # drop older days
            return pool, answer

    @iemdle.command(name="play", description="Show today's IEMdle board and rules")
    async def iemdle_play(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _, answer = await self.today_puzzle()
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        play = self.db.get_play(today().isoformat(), interaction.user.id)
        stats = self.db.player_stats(interaction.user.id) if play["finished"] else None
        view = ShareGridView(self.share_text(play, answer, stats)) if play["finished"] else discord.utils.MISSING
        await interaction.followup.send(embed=board_embed(today(), play, answer, stats), view=view, ephemeral=True)

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
                                            embed=board_embed(today(), play, answer, stats), ephemeral=True)
            return
        guess = self.resolve_guess(pool, iem)
        if guess is None:
            await interaction.followup.send(f"“{clip(iem, 60)}” isn't in today's list (top {POOL_SIZE} IEMs). "
                                            "Pick a suggestion from the autocomplete.", ephemeral=True)
            return
        if any(g["url"] == guess["url"] for g in play["guesses"]):
            await interaction.followup.send(f"You already guessed **{guess['name']}**.", ephemeral=True)
            return

        play["guesses"].append(guess)
        play["solved"] = guess["url"] == answer["url"]
        play["finished"] = play["solved"] or len(play["guesses"]) >= MAX_TRIES
        guild_id = interaction.guild.id if interaction.guild else None
        self.db.save_play(day, interaction.user.id, play, guild_id)
        stats = None
        if play["finished"]:
            if play["solved"] and guild_id:
                self.db.add_points(guild_id, interaction.user.id, "iemdle", MAX_TRIES + 1 - len(play["guesses"]))
            stats = self.db.player_stats(interaction.user.id)
        view = ShareGridView(self.share_text(play, answer, stats)) if play["finished"] else discord.utils.MISSING
        await interaction.followup.send(embed=board_embed(today(), play, answer, stats), view=view, ephemeral=True)

    @iemdle_guess.autocomplete("iem")
    async def iemdle_autocomplete(self, _interaction: discord.Interaction, current: str):
        cached = self.pool_cache.get(today().isoformat())
        if not cached:  # never fetch from autocomplete; /iemdle play or the first guess loads the pool
            stored = self.db.get_day(today().isoformat())
            if not stored:
                return []
            pool = stored[0]
        else:
            pool = cached[0]
        q = norm(current)
        hits = [it for it in pool if q in norm(it["name"]) or q in norm(f"{it.get('brand', '')} {it['name']}")]
        hits.sort(key=lambda it: (not norm(it["name"]).startswith(q), it["name"]))
        return [app_commands.Choice(name=clip(it["name"], 100), value=clip(it["name"], 100)) for it in hits[:25]]

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

    @iemdle.command(name="stats", description="Your IEMdle stats")
    async def iemdle_stats(self, interaction: discord.Interaction):
        s = self.db.player_stats(interaction.user.id)
        e = discord.Embed(title=f"📈 IEMdle stats · {interaction.user.display_name}", color=GAME)
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        rate = round(s["wins"] / s["played"] * 100) if s["played"] else 0
        e.description = (f"🎮 **{s['played']}** played · ✅ **{rate}%** solved\n"
                         f"🔥 Streak **{s['streak']}** · 🏅 Best **{s['best']}**")
        peak = max(s["dist"]) or 1
        e.add_field(name="Guess distribution", value="\n".join(
            f"`{i}` {'🟩' * max(1 if n else 0, round(n / peak * 10))} {n}" for i, n in enumerate(s["dist"], 1)
        ), inline=False)
        await interaction.response.send_message(embed=brand_footer(e), ephemeral=True)

    # ===== World Cup =====

    def is_mod(self, member: discord.Member) -> bool:
        mod = self.bot.get_cog("Moderation")
        return mod.is_mod(member) if mod else member.guild_permissions.manage_guild

    @worldcup.command(name="start", description="Mods: start an IEM World Cup bracket members vote on")
    @app_commands.describe(
        name="Event name, e.g. 'Budget IEM World Cup'",
        size="Number of entrants",
        round_length="How long each round's voting lasts, e.g. 1d, 12h, 30m",
        channel="Where to post matches (default: here)",
        sort="Fill the bracket from this IEMRanking list (ignored if entries is set)",
        kind="IEMs or headphones",
        price="Price range filter",
        entries="Or list the entrants yourself, comma-separated (must match size)",
    )
    @app_commands.rename(kind="type")
    @app_commands.choices(
        size=[app_commands.Choice(name="8 entrants (3 rounds)", value=8),
              app_commands.Choice(name="16 entrants (4 rounds)", value=16)],
        sort=[app_commands.Choice(name=label, value=value) for value, label in SORTS.items()][:25],
        kind=KIND_CHOICES,
        price=[app_commands.Choice(name=label, value=value) for value, label in PRICES.items()],
    )
    async def wc_start(self, interaction: discord.Interaction, name: app_commands.Range[str, 3, 60], size: int,
                       round_length: str = "1d", channel: discord.TextChannel | None = None,
                       sort: str = "reviewer-score", kind: str = "iem", price: str | None = None,
                       entries: str | None = None):
        if not self.is_mod(interaction.user):
            await interaction.response.send_message("Only moderators can start a World Cup.", ephemeral=True)
            return
        seconds = parse_duration(round_length)
        if not seconds or not MIN_ROUND <= seconds <= MAX_ROUND:
            await interaction.response.send_message("Round length must be between `1m` and `7d`, e.g. `1d` or `12h`.",
                                                    ephemeral=True)
            return
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(f"I need **Send Messages** and **Embed Links** in {channel.mention}.",
                                                    ephemeral=True)
            return
        if self.db.running_event(interaction.guild.id):
            await interaction.response.send_message("A World Cup is already running here. Use `/worldcup status` or "
                                                    "`/worldcup cancel`.", ephemeral=True)
            return
        names = [n.strip() for n in entries.split(",") if n.strip()] if entries else []
        if entries and len(names) != size:
            await interaction.response.send_message(f"You listed {len(names)} entrants but picked size {size}.",
                                                    ephemeral=True)
            return
        if not await gate(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            if names:
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
            else:
                data = await self.api.ranking(sort=sort, type=kind, price=price, limit=size)
                field = [slim(it) for it in (data.get("results") or [])[:size]]
                if len(field) < size:
                    await interaction.followup.send(f"Only {len(field)} products match those filters; need {size}.",
                                                    ephemeral=True)
                    return
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        async with self.event_lock:
            if self.db.running_event(interaction.guild.id):
                await interaction.followup.send("A World Cup was just started by someone else.", ephemeral=True)
                return
            event_id = self.db.create_event(interaction.guild.id, channel.id, name, size, seconds, interaction.user.id)
            event = self.db.event(event_id)
            seeded = [field[s - 1] for s in SEED_ORDER[size]]
            try:
                await channel.send(embed=self.announce_embed(event, field, sort, kind, price, bool(names)))
                await self.post_round(event, 1, [(seeded[i], seeded[i + 1]) for i in range(0, size, 2)])
            except (discord.HTTPException, PostError) as exc:
                self.db.set_event(event_id, status="cancelled")
                await interaction.followup.send(f"Couldn't post in {channel.mention}: {exc}", ephemeral=True)
                return
        await interaction.followup.send(f"🏆 **{name}** is live in {channel.mention}! Rounds last {fmt_duration(seconds)}.",
                                        ephemeral=True)

    def announce_embed(self, event: dict, field: list[dict], sort: str, kind: str, price: str | None,
                       manual: bool) -> discord.Embed:
        e = discord.Embed(title=f"🏆 {event['name']}", color=GOLD)
        source = "Hand-picked by the mods" if manual else \
            f"Top {event['size']} {TYPE_TITLE.get(kind, kind)} by {SORTS.get(sort, sort).lower()}" + \
            (f" · {PRICES[price]}" if price else "")
        rounds = event["size"].bit_length() - 1
        e.description = (f"**{event['size']} entrants · {rounds} rounds · {fmt_duration(event['round_seconds'])} per round**\n"
                         f"{source}\n\nVote on each match below. Correct picks earn prediction points "
                         f"on `/leaderboard`! 🗳️")
        e.add_field(name="Seeds", value="\n".join(
            f"`{i:>2}` {TYPE_EMOJI.get(it.get('type'), '🎧')} {clip(it['name'], 45)} · ⭐ {fmt_score(it.get('score'))}"
            for i, it in enumerate(field, 1)
        ), inline=False)
        return brand_footer(e, "Product data from IEMRanking.com")

    async def post_round(self, event: dict, rnd: int, pairs: list[tuple[dict, dict]]):
        channel = self.bot.get_channel(event["channel_id"])
        if channel is None:
            raise PostError("the event's channel no longer exists")
        ends_at = int(time.time()) + event["round_seconds"]
        self.db.set_event(event["id"], round=rnd)
        if len(pairs) > 1:
            await channel.send(f"## 🔔 {round_name(len(pairs))}s are open! Voting ends <t:{ends_at}:R>.")
        for slot, (a, b) in enumerate(pairs):
            match_id = self.db.add_match(event["id"], rnd, slot, a, b, ends_at)
            match = self.db.match(match_id)
            msg = await channel.send(embed=match_embed(event, match, len(pairs), {"a": 0, "b": 0}), view=vote_view(match))
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
            log.exception("World Cup ticker failed")

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
        channel = self.bot.get_channel(event["channel_id"])
        if channel is None or not match.get("message_id"):
            return
        n = len(self.db.matches(event["id"], match["round"]))
        try:
            await channel.get_partial_message(match["message_id"]).edit(embed=match_embed(event, match, n, tally),
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
        channel = self.bot.get_channel(event["channel_id"])
        if len(winners) == 1:
            champ = winners[0]
            self.db.set_event(event["id"], status="done", champion=json.dumps(champ))
            if channel:
                e = build_profile_embed(champ)
                e.title = f"👑 {champ['name']}"
                e.description = f"🏆 **Champion of {event['name']}!**\n\n" + (e.description or "")
                try:
                    await channel.send(content="# 🎉 We have a champion!", embed=e)
                except discord.HTTPException:
                    pass
            return
        pairs = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
        try:
            await self.post_round(event, event["round"] + 1, pairs)
        except (discord.HTTPException, PostError):
            log.warning("World Cup %s: couldn't post next round, cancelling", event["id"])
            self.db.set_event(event["id"], status="cancelled")

    @worldcup.command(name="status", description="Show the current (or last) World Cup bracket")
    async def wc_status(self, interaction: discord.Interaction):
        event = self.db.running_event(interaction.guild.id) or self.db.latest_event(interaction.guild.id)
        if not event:
            await interaction.response.send_message("No World Cup yet. Mods can start one with `/worldcup start`.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(embed=self.bracket_embed(event), ephemeral=True)

    def bracket_embed(self, event: dict) -> discord.Embed:
        status = {"running": f"🟢 Live · round {event['round']}", "done": "🏁 Finished",
                  "cancelled": "⛔ Cancelled"}[event["status"]]
        e = discord.Embed(title=f"🏆 {event['name']}", description=status, color=GOLD)
        by_round: dict[int, list[dict]] = {}
        for m in self.db.matches(event["id"]):
            by_round.setdefault(m["round"], []).append(m)
        for rnd, ms in by_round.items():
            lines = []
            for m in ms:
                a, b = clip(m["a"]["name"], 28), clip(m["b"]["name"], 28)
                if m["winner"] == "a":
                    lines.append(f"**{a}** ✅ vs ~~{b}~~")
                elif m["winner"] == "b":
                    lines.append(f"~~{a}~~ vs **{b}** ✅")
                else:
                    t = self.db.tally(m["id"])
                    lines.append(f"{a} `{t['a']}` vs `{t['b']}` {b} · ends <t:{m['ends_at']}:R>")
            e.add_field(name=round_name(len(ms)), value="\n".join(lines)[:1024], inline=False)
        if event.get("champion"):
            e.add_field(name="👑 Champion", value=json.loads(event["champion"])["name"], inline=False)
        return brand_footer(e)

    @worldcup.command(name="cancel", description="Mods: stop the running World Cup")
    async def wc_cancel(self, interaction: discord.Interaction):
        if not self.is_mod(interaction.user):
            await interaction.response.send_message("Only moderators can cancel a World Cup.", ephemeral=True)
            return
        event = self.db.running_event(interaction.guild.id)
        if not event:
            await interaction.response.send_message("No World Cup is running.", ephemeral=True)
            return
        self.db.set_event(event["id"], status="cancelled")
        await interaction.response.send_message(f"⛔ **{event['name']}** cancelled.", ephemeral=True)
        channel = self.bot.get_channel(event["channel_id"])
        for m in self.db.matches(event["id"], event["round"]):
            if m["winner"] is None and channel and m.get("message_id"):
                try:
                    await channel.get_partial_message(m["message_id"]).edit(view=None)
                except discord.HTTPException:
                    pass

    # ===== Leaderboard =====

    @app_commands.command(name="leaderboard", description="Top players in this server (IEMdle + World Cup)")
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
            e.description = "No points yet! Play `/iemdle play` or vote in a `/worldcup`."
            await interaction.response.send_message(embed=brand_footer(e))
            return
        lines = [f"{MEDALS.get(i, f'`{i:>2}.`')} <@{uid}> · **{pts:,}** pt{'s' if pts != 1 else ''}"
                 for i, (uid, pts) in enumerate(rows[:10], 1)]
        e.description = "\n".join(lines)
        me = next(((i, p) for i, (uid, p) in enumerate(rows, 1) if uid == interaction.user.id), None)
        footer = f"You: #{me[0]} · {me[1]:,} pts" if me else "You're not on the board yet"
        await interaction.response.send_message(embed=brand_footer(e, f"{footer} · IEMdle: 7 − tries · World Cup: 1 per correct vote"),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
