"""IEMRanking.com lookups: product profiles, ranking lists and head-to-head compares.

API docs: https://www.iemranking.com/page/api-docs
Needs IEMRANKING_API_KEY in the environment. Keep it server-side only.
"""

import logging
import math
import os
import re
import time
from collections import deque

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("B_Media Community Bot")

API_BASE = "https://www.iemranking.com/api/v1/"
API_KEY = os.getenv("IEMRANKING_API_KEY")
USER_AGENT = "B_Media-Community-Bot/1.0 (+discord.py)"
CACHE_TTL = 300          # API says answers may be reused for 5 minutes
FILTERS_TTL = 24 * 3600  # /filters changes rarely; fetch once a day
PAGE_SIZE = 10
MAX_OFFSET = 500         # API only serves the first 500 positions of a list
CREDIT = "Data from IEMRanking.com"

TYPE_LABEL = {"iem": "IEM", "headphone": "Headphone"}
TYPE_PLURAL = {"iem": "IEMs", "headphone": "headphones"}
TYPE_TITLE = {"iem": "IEMs", "headphone": "Headphones"}
TYPE_EMOJI = {"iem": "👂", "headphone": "🎧"}

SORTS = {
    "reviewer-score": "Reviewer score",
    "user-score": "User score",
    "favorites": "Most favorited",
    "gaming-score": "Gaming score",
    "price-to-performance": "Price to performance",
    "recently-reviewed": "Recently reviewed",
    "msrp": "Price, highest first",
    "best-tonality": "Best tonality",
    "best-technicalities": "Best technicalities",
    "best-bass": "Best bass",
    "best-mids": "Best mids",
    "best-treble": "Best treble",
    "best-dynamics": "Best dynamics",
    "best-soundstage": "Best soundstage",
    "best-details": "Best details",
    "best-imaging": "Best imaging",
}
PRICES = {
    "50": "Below $50",
    "50-100": "$50 – $100",
    "100-200": "$100 – $200",
    "200-400": "$200 – $400",
    "400-600": "$400 – $600",
    "600-1000": "$600 – $1,000",
    "1000-2000": "$1,000 – $2,000",
    "2000-4000": "$2,000 – $4,000",
    "4000": "Above $4,000",
}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# ----- Rate limits -----
# Per user, shared by every IEMRanking action that can call the API
# (/search, !search, /top + page flips, /compare, "Get my picks"). Members with Manage Server are exempt.
USER_LIMITS = ((4, 60), (30, 3600))      # (max uses, per seconds): 4/minute and 30/hour
AUTOCOMPLETE_LIMITS = ((15, 60),)         # uncached autocomplete lookups per user
# Bot-wide cap on real (uncached) API calls. Set it a bit under your key's per-minute limit.
API_MAX_PER_MINUTE = int(os.getenv("IEMRANKING_MAX_PER_MINUTE", "20"))
API_RESERVE = 5        # when the API says this few requests are left, autocomplete stops (commands keep the rest)
LOW_BUDGET_SECONDS = 60


class APIError(Exception):
    """Error with a message that is safe to show to users."""


class SlidingLimit:
    """Sliding-window limiter with one or more (max, seconds) windows per key."""

    def __init__(self, *windows: tuple[int, int]):
        self.windows = windows
        self.longest = max(s for _, s in windows)
        self.hits: dict[int, deque[float]] = {}

    def retry_after(self, key: int) -> float:
        """Seconds until `key` may act again (0 = allowed now)."""
        q = self.hits.get(key)
        if not q:
            return 0.0
        now = time.monotonic()
        while q and now - q[0] >= self.longest:
            q.popleft()
        wait = 0.0
        for limit, seconds in self.windows:
            recent = [t for t in q if now - t < seconds]
            if len(recent) >= limit:
                wait = max(wait, recent[-limit] + seconds - now)
        return wait

    def hit(self, key: int):
        self.hits.setdefault(key, deque()).append(time.monotonic())
        if len(self.hits) > 1000:  # drop idle users
            now = time.monotonic()
            self.hits = {k: q for k, q in self.hits.items() if q and now - q[-1] < self.longest}

    def try_hit(self, key: int) -> float:
        """Record a use if allowed; return 0 on success, else seconds to wait."""
        wait = self.retry_after(key)
        if wait <= 0:
            self.hit(key)
        return wait


user_limit = SlidingLimit(*USER_LIMITS)
autocomplete_limit = SlidingLimit(*AUTOCOMPLETE_LIMITS)


def is_exempt(user: discord.abc.User) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def slow_down_message(wait: float) -> str:
    return f"⏳ Easy there! You can use IEMRanking commands again <t:{int(time.time() + wait) + 1}:R>."


async def gate(interaction: discord.Interaction) -> bool:
    """Count one use for this user; if over the limit, tell them privately and return False."""
    if is_exempt(interaction.user):
        return True
    wait = user_limit.try_hit(interaction.user.id)
    if wait <= 0:
        return True
    msg = slow_down_message(wait)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


# ---------- API client ----------

class IEMRankingAPI:
    def __init__(self, key: str | None):
        self.key = key
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.blocked_until = 0.0  # set from Retry-After on 429
        self.low_until = 0.0      # set when X-RateLimit-Remaining gets close to 0
        self.outgoing = SlidingLimit((API_MAX_PER_MINUTE, 60))

    @staticmethod
    def _key(endpoint: str, params: dict) -> tuple[tuple, dict]:
        params = {k: str(v) for k, v in params.items() if v not in (None, "")}
        return (endpoint, tuple(sorted(params.items()))), params

    def peek(self, endpoint: str, **params) -> dict | None:
        """Cached answer or None. Never calls the API."""
        hit = self.cache.get(self._key(endpoint, params)[0])
        return hit[1] if hit and hit[0] > time.monotonic() else None

    def can_background(self) -> bool:
        """Spare capacity for nice-to-haves (autocomplete): not low on quota, under half the bot-wide cap."""
        now = time.monotonic()
        if self.low_until > now or self.blocked_until > now:
            return False
        return self.outgoing.retry_after(0) <= 0 and len(self.outgoing.hits.get(0, ())) < API_MAX_PER_MINUTE // 2

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"X-API-Key": self.key or "", "User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self.session

    async def get(self, endpoint: str, ttl: int = CACHE_TTL, **params) -> dict:
        if not self.key:
            raise APIError("IEMRanking isn't set up yet (missing `IEMRANKING_API_KEY`).")
        cache_key, params = self._key(endpoint, params)
        now = time.monotonic()
        if (hit := self.cache.get(cache_key)) and hit[0] > now:
            return hit[1]
        if self.blocked_until > now:
            raise APIError(f"⏳ IEMRanking is busy. Try again <t:{int(time.time() + self.blocked_until - now) + 1}:R>.")
        if (wait := self.outgoing.try_hit(0)) > 0:
            raise APIError(f"⏳ Lots of lookups right now. Try again <t:{int(time.time() + wait) + 1}:R>.")

        try:
            async with self._session().get(API_BASE + endpoint, params=params) as resp:
                if resp.status == 429:
                    retry = int(resp.headers.get("Retry-After", "60") or 60)
                    self.blocked_until = now + retry
                    log.warning("IEMRanking 429: blocked for %ss", retry)
                    raise APIError(f"⏳ IEMRanking is busy. Try again <t:{int(time.time()) + retry}:R>.")
                remaining = resp.headers.get("X-RateLimit-Remaining", "")
                if remaining.isdigit() and int(remaining) <= API_RESERVE:
                    self.low_until = now + LOW_BUDGET_SECONDS
                    log.info("IEMRanking quota low (%s left): autocomplete paused", remaining)
                if resp.content_type != "application/json":
                    # HTML = request stopped before reaching the API (e.g. Cloudflare)
                    log.warning("IEMRanking returned non-JSON (HTTP %s) for %s", resp.status, endpoint)
                    raise APIError(f"IEMRanking is unreachable right now (HTTP {resp.status}). Try again later.")
                data = await resp.json()
        except aiohttp.ClientError as exc:
            log.warning("IEMRanking request failed: %s", exc)
            raise APIError("Couldn't reach IEMRanking. Try again in a bit.") from exc
        except TimeoutError as exc:
            raise APIError("IEMRanking took too long to answer. Try again in a bit.") from exc

        if not data.get("success"):
            if resp.status == 401:
                log.error("IEMRanking rejected the API key")
                raise APIError("IEMRanking rejected the bot's API key. Tell a server admin.")
            raise APIError(data.get("message") or f"IEMRanking error (HTTP {resp.status}).")

        self.cache[cache_key] = (now + ttl, data)
        if len(self.cache) > 500:  # prune expired entries
            self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
        return data

    async def search(self, query: str, kind: str = "all", limit: int = 10) -> dict:
        return await self.get("search", q=query, type=kind, limit=limit)

    async def ranking(self, **params) -> dict:
        return await self.get("ranking", **params)

    async def filters(self) -> dict:
        return await self.get("filters", ttl=FILTERS_TTL)


# ---------- Formatting helpers ----------

def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def handle(text: str) -> str:
    """Brand handle: lower case with hyphens ('Truthear Audio' → 'truthear-audio')."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def bar(score: float | None, width: int = 10) -> str:
    if score is None:
        return "▱" * width
    filled = max(0, min(width, round(score / 10 * width)))
    return "▰" * filled + "▱" * (width - filled)


def fmt_score(score: float | None) -> str:
    return "—" if score is None else f"{score:.1f}"


def fmt_price(usd: float | None) -> str:
    if usd is None:
        return "—"
    return f"${usd:,.0f}" if float(usd).is_integer() else f"${usd:,.2f}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def top_percent(item: dict) -> int | None:
    rank, among = item.get("rank"), item.get("ranked_among")
    if not rank or not among:
        return None
    return max(1, math.ceil(rank / among * 100))


def tier_color(item: dict) -> discord.Color:
    pct = top_percent(item)
    if pct is None:
        return discord.Color.dark_grey()
    if pct <= 5:
        return discord.Color.from_rgb(212, 175, 55)   # gold
    if pct <= 15:
        return discord.Color.from_rgb(46, 204, 113)   # green
    if pct <= 35:
        return discord.Color.from_rgb(26, 188, 156)   # teal
    if pct <= 60:
        return discord.Color.from_rgb(52, 152, 219)   # blue
    return discord.Color.from_rgb(149, 165, 166)      # grey


def tier_badge(item: dict) -> str:
    pct = top_percent(item)
    if pct is None:
        return "🆕 Not ranked yet (needs 2+ reviews)"
    kind = TYPE_PLURAL.get(item.get("type"), "products")
    place = f"**#{item['rank']:,}** of {item['ranked_among']:,} {kind}"
    if pct <= 5:
        return f"🏆 {place} · **Top {pct}%**"
    if pct <= 15:
        return f"🥇 {place} · **Top {pct}%**"
    if pct <= 50:
        return f"🏅 {place} · Top {pct}%"
    return f"📊 {place}"


def score_block(score: float | None, count: int | None, noun: str) -> str:
    if score is None:
        return f"`  —  ` {bar(None)}\n*No {noun}s yet*"
    return f"**`{score:.1f}`** / 10\n{bar(score)}\n*{plural(count or 0, noun)}*"


def product_link(item: dict, n: int = 60) -> str:
    return f"[{clip(item['name'], n)}]({item['url']})"


# ---------- Embeds ----------

def build_profile_embed(item: dict, note: str | None = None) -> discord.Embed:
    kind = item.get("type")
    e = discord.Embed(
        title=f"{TYPE_EMOJI.get(kind, '🎧')} {item['name']}",
        url=item["url"],
        color=tier_color(item),
        description=(f"*{note}*\n\n" if note else "") + tier_badge(item),
    )
    e.set_author(name=f"{item.get('brand') or 'Unknown brand'} · {TYPE_LABEL.get(kind, 'Product')}")
    e.add_field(name="⭐ Reviewer Score", value=score_block(item.get("score"), item.get("review_count"), "review"))
    e.add_field(name="👥 User Score", value=score_block(item.get("user_score"), item.get("user_review_count"), "rating"))
    e.add_field(name="💰 Launch Price", value=f"**{fmt_price(item.get('msrp_usd'))}**\n*MSRP (USD)*")
    if item.get("image_url"):
        e.set_image(url=item["image_url"])
    e.set_footer(text=f"{CREDIT} · Click the title to read the reviews")
    return e


def sort_metric(sort: str, item: dict) -> str:
    """The number a ranking line leads with, depending on the sort."""
    if sort == "user-score":
        return f"👥 **{fmt_score(item.get('user_score'))}** ({item.get('user_review_count') or 0})"
    if sort == "favorites":
        return f"❤️ **{item.get('favorites') or 0:,}**"
    if sort == "msrp":
        return f"💰 **{fmt_price(item.get('msrp_usd'))}**"
    if item.get("sort_value") is not None:
        return f"📈 **{fmt_score(item['sort_value'])}** · ⭐ {fmt_score(item.get('score'))}"
    return f"⭐ **{fmt_score(item.get('score'))}** ({item.get('review_count') or 0})"


def build_ranking_embed(data: dict) -> discord.Embed:
    sort, kind = data["sort"], data.get("type", "iem")
    f = data.get("filters") or {}
    bits = []
    if f.get("price"):
        bits.append("💰 " + ", ".join(PRICES.get(p, p) for p in f["price"]))
    for key, emoji in (("tuning", "🎚️"), ("driver", "⚙️"), ("brand", "🏷️"), ("reviewers", "📝")):
        if f.get(key):
            bits.append(f"{emoji} " + ", ".join(f[key]))

    e = discord.Embed(
        title=f"🏆 {data.get('sort_label') or SORTS.get(sort, sort)} · {TYPE_TITLE.get(kind, kind)}",
        url=data.get("site_url"),
        color=discord.Color.from_rgb(200, 245, 60),  # IEMRanking lime
    )
    results = data.get("results") or []
    if not results:
        e.description = (" · ".join(bits) + "\n\n" if bits else "") + "Nothing matches those filters."
        e.set_footer(text=CREDIT)
        return e

    lines = []
    for item in results:
        pos = item["position"]
        prefix = MEDALS.get(pos, f"`{pos:>2}.`")
        extra = "" if sort == "msrp" else f" · {fmt_price(item.get('msrp_usd'))}"
        lines.append(f"{prefix} {product_link(item, 45)}\n {sort_metric(sort, item)}{extra}")
    e.description = (" · ".join(bits) + "\n\n" if bits else "") + "\n".join(lines)
    if results[0].get("image_url"):
        e.set_thumbnail(url=results[0]["image_url"])
    total = data.get("total") or 0
    first = data.get("offset", 0) + 1
    e.set_footer(text=f"#{first}–{first + len(results) - 1} of {total:,} · {CREDIT}")
    return e


def build_compare_embed(a: dict, b: dict) -> discord.Embed:
    def better(x, y, higher=True):
        """(mark_a, mark_b) for the winner of one metric."""
        if x is None or y is None or x == y:
            return "", ""
        a_wins = x > y if higher else x < y
        return (" ✅", "") if a_wins else ("", " ✅")

    sa, sb = better(a.get("score"), b.get("score"))
    ua, ub = better(a.get("user_score"), b.get("user_score"))
    ra, rb = better(a.get("rank"), b.get("rank"), higher=False) if a.get("type") == b.get("type") else ("", "")
    pa, pb = better(a.get("msrp_usd"), b.get("msrp_usd"), higher=False)

    def column(item, s, u, r, p):
        rank = f"#{item['rank']:,} / {item['ranked_among']:,}" if item.get("rank") else "Unranked"
        return (
            f"⭐ **{fmt_score(item.get('score'))}** /10{s}\n{bar(item.get('score'))}\n"
            f"*{plural(item.get('review_count') or 0, 'review')}*\n\n"
            f"👥 **{fmt_score(item.get('user_score'))}** /10{u}\n"
            f"*{plural(item.get('user_review_count') or 0, 'rating')}*\n\n"
            f"🏅 {rank}{r}\n💰 {fmt_price(item.get('msrp_usd'))}{p}\n\n"
            f"[Read reviews ↗]({item['url']})"
        )

    e = discord.Embed(title="⚔️ Head to head", color=discord.Color.from_rgb(200, 245, 60))
    e.add_field(name=f"{TYPE_EMOJI.get(a.get('type'), '🎧')} {clip(a['name'], 60)}", value=column(a, sa, ua, ra, pa))
    e.add_field(name=f"{TYPE_EMOJI.get(b.get('type'), '🎧')} {clip(b['name'], 60)}", value=column(b, sb, ub, rb, pb))

    if a.get("score") is not None and b.get("score") is not None and a["score"] != b["score"]:
        win, lose = (a, b) if a["score"] > b["score"] else (b, a)
        verdict = f"**{win['name']}** scores **{win['score'] - lose['score']:.1f}** higher with reviewers."
    elif a.get("score") is not None and a.get("score") == b.get("score"):
        verdict = "Dead even with reviewers."
    else:
        verdict = "Not enough reviews to call it."
    if a.get("type") != b.get("type"):
        verdict += "\n*IEMs and headphones are ranked in separate lists.*"
    e.description = verdict
    if a.get("image_url") or b.get("image_url"):
        e.set_thumbnail(url=a.get("image_url") or b.get("image_url"))
    e.set_footer(text=CREDIT)
    return e


# ---------- UI ----------

def link_button(label: str, url: str) -> discord.ui.Button:
    return discord.ui.Button(label=label, url=url, emoji="🔗")


class ProfileView(discord.ui.View):
    """Profile link + dropdown to switch between the other search matches (no extra API calls)."""

    def __init__(self, owner_id: int, results: list[dict], index: int = 0, note: str | None = None):
        super().__init__(timeout=300)
        self.owner_id, self.results, self.note = owner_id, results, note
        self.message: discord.Message | None = None
        self.render(index)

    def render(self, index: int):
        self.index = index
        self.clear_items()
        self.add_item(link_button("Read the reviews", self.results[index]["url"]))
        if len(self.results) > 1:
            select = discord.ui.Select(
                placeholder="Not it? Pick another match…",
                options=[
                    discord.SelectOption(
                        label=clip(r["name"], 100),
                        value=str(i),
                        emoji=TYPE_EMOJI.get(r.get("type"), "🎧"),
                        description=clip(f"⭐ {fmt_score(r.get('score'))} · {fmt_price(r.get('msrp_usd'))} · "
                                         f"{TYPE_LABEL.get(r.get('type'), '')}", 100),
                        default=i == index,
                    )
                    for i, r in enumerate(self.results[:25])
                ],
            )
            select.callback = self.on_select
            self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who searched can switch. Try `/search` yourself!",
                                                    ephemeral=True)
            return
        index = int(interaction.data["values"][0])
        self.render(index)
        await interaction.response.edit_message(embed=build_profile_embed(self.results[index]), view=self)

    async def on_timeout(self):
        # Keep the link button, drop the dropdown
        if self.message:
            view = discord.ui.View()
            view.add_item(link_button("Read the reviews", self.results[self.index]["url"]))
            try:
                await self.message.edit(view=view)
            except discord.HTTPException:
                pass


class RankingView(discord.ui.View):
    """Prev/Next paging over a ranking list + link to the same list on the site."""

    def __init__(self, api: IEMRankingAPI, params: dict, data: dict):
        super().__init__(timeout=600)
        self.api, self.params, self.data = api, params, data
        self.message: discord.Message | None = None
        self.render()

    def render(self):
        self.clear_items()
        offset, total = self.data.get("offset", 0), min(self.data.get("total") or 0, MAX_OFFSET)
        prev_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary, disabled=offset <= 0)
        next_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary,
                                     disabled=offset + PAGE_SIZE >= total)
        prev_btn.callback = self._pager(-PAGE_SIZE)
        next_btn.callback = self._pager(PAGE_SIZE)
        self.add_item(prev_btn)
        self.add_item(next_btn)
        if self.data.get("site_url"):
            self.add_item(link_button("See full list", self.data["site_url"]))

    def _pager(self, step: int):
        async def callback(interaction: discord.Interaction):
            offset = max(0, self.data.get("offset", 0) + step) or None  # page 1 = same cache key as the command
            # Flipping back to an already-seen page is free; new pages count as a use
            if self.api.peek("ranking", **self.params, offset=offset) is None and not await gate(interaction):
                return
            try:
                data = await self.api.ranking(**self.params, offset=offset)
            except APIError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            self.data = data
            self.render()
            await interaction.response.edit_message(embed=build_ranking_embed(data), view=self)
        return callback

    async def on_timeout(self):
        if self.message and self.data.get("site_url"):
            view = discord.ui.View()
            view.add_item(link_button("See full list", self.data["site_url"]))
            try:
                await self.message.edit(view=view)
            except discord.HTTPException:
                pass


# ---------- Recommendations ----------

# (sort, answer label, emoji, description, short metric label)
PRIORITIES = [
    ("reviewer-score", "Best all-rounder", "⭐", "Highest overall reviewer score", "Overall"),
    ("price-to-performance", "Best value", "💸", "Most performance for the money", "Value"),
    ("best-bass", "Bass", "🔊", "Impactful, high-quality low end", "Bass"),
    ("best-mids", "Vocals & mids", "🎤", "Natural, clear vocals and instruments", "Mids"),
    ("best-treble", "Treble & sparkle", "✨", "Airy, refined highs", "Treble"),
    ("best-tonality", "Natural tonality", "🎼", "Balanced, correct-sounding overall tuning", "Tonality"),
    ("best-details", "Detail & resolution", "🔬", "Hear every little thing", "Detail"),
    ("best-soundstage", "Soundstage & imaging", "🌌", "Wide, spacious, precise placement", "Soundstage"),
    ("gaming-score", "Gaming", "🎮", "Footsteps, positioning, competitive play", "Gaming"),
]
PRIORITY_INFO = {p[0]: p for p in PRIORITIES}
PICKS = 5
# Sorts where we re-rank candidates by our own fit score (others keep the site's order)
REFIT_SORTS = {"reviewer-score", "gaming-score"} | {p[0] for p in PRIORITIES if p[0].startswith("best-")}


def tuning_options(filters: dict) -> list[tuple[str, str]]:
    """(value, label) pairs of tuning handles from /filters."""
    opts = filters.get("tuning") or (filters.get("filters") or {}).get("tuning") or []
    return [(str(o["value"]), str(o.get("label") or o["value"])) for o in opts if isinstance(o, dict) and o.get("value")]


def fit_score(item: dict) -> float:
    """Blend the focus score with the overall score; discount products with few reviews."""
    overall = item.get("score") or 0.0
    focus = item.get("sort_value")
    quality = 0.65 * focus + 0.35 * overall if focus is not None else overall
    confidence = min(1.0, 0.85 + 0.03 * (item.get("review_count") or 0))
    return quality * confidence


async def get_picks(api: IEMRankingAPI, kind: str, prices: list[str], tunings: list[str],
                    sort: str) -> list[tuple[dict, bool]]:
    """Up to PICKS (item, outside_tuning) pairs, most suitable first. At most 2 API calls."""
    base = {"sort": sort, "type": kind, "price": ",".join(prices) or None, "limit": 25}
    data = await api.ranking(**base, tuning=",".join(tunings) or None)
    strict = data.get("results") or []
    relaxed = []
    if tunings and len(strict) < PICKS:  # too few with that tuning → top up, flagged
        seen = {it["url"] for it in strict}
        relaxed = [it for it in (await api.ranking(**base)).get("results") or [] if it["url"] not in seen]
    if sort in REFIT_SORTS:
        strict = sorted(strict, key=fit_score, reverse=True)
        relaxed = sorted(relaxed, key=fit_score, reverse=True)
    return ([(it, False) for it in strict] + [(it, True) for it in relaxed])[:PICKS]


def answers_line(kind: str, prices: list[str], tunings: list[str], sort: str, tuning_labels: dict) -> str:
    _, label, emoji, *_ = PRIORITY_INFO[sort]
    parts = [
        f"{TYPE_EMOJI[kind]} {TYPE_TITLE[kind]}",
        "💰 " + (", ".join(PRICES[p] for p in prices) if prices else "Any budget"),
        "🎚️ " + (", ".join(tuning_labels.get(t, t) for t in tunings) if tunings else "Any sound"),
        f"{emoji} {label}",
    ]
    return " · ".join(parts)


def build_recommend_panel() -> discord.Embed:
    e = discord.Embed(
        title="🎯 Find your next IEM",
        description=(
            "Answer the questions below, then hit **Get my picks**.\n"
            "Skip any question for *no preference*.\n\n"
            "**1.** IEMs or over-ear headphones?\n"
            "**2.** What's your budget? *(pick one or more)*\n"
            "**3.** What sound do you like? *(pick one or more)*\n"
            "**4.** What matters most to you?"
        ),
        color=discord.Color.from_rgb(200, 245, 60),
    )
    e.set_footer(text=CREDIT)
    return e


def build_picks_embed(picks: list[tuple[dict, bool]], summary: str, sort: str) -> discord.Embed:
    metric = PRIORITY_INFO[sort][4]
    e = discord.Embed(title="🎯 Your top picks", color=discord.Color.from_rgb(200, 245, 60))
    if not picks:
        e.description = f"{summary}\n\nNothing matches all of that. Try a wider budget or fewer sound picks."
        e.set_footer(text=CREDIT)
        return e
    e.description = f"{summary}\n\n*Most suitable first.*"
    for i, (item, outside) in enumerate(picks, 1):
        lead = MEDALS.get(i, f"`#{i}`")
        lines = []
        if item.get("sort_value") is not None:
            lines.append(f"🎯 {metric} **{fmt_score(item['sort_value'])}** · ⭐ {fmt_score(item.get('score'))} overall")
        else:
            lines.append(f"⭐ **{fmt_score(item.get('score'))}** / 10 overall {bar(item.get('score'), 8)}")
        lines.append(f"📝 {plural(item.get('review_count') or 0, 'review')} · 💰 {fmt_price(item.get('msrp_usd'))}"
                     f" · [Read reviews ↗]({item['url']})")
        if outside:
            lines.append("*↪ Outside your sound picks (not enough exact matches)*")
        e.add_field(name=f"{lead} {clip(item['name'], 80)}", value="\n".join(lines), inline=False)
    if picks[0][0].get("image_url"):
        e.set_thumbnail(url=picks[0][0]["image_url"])
    e.set_footer(text=f"{CREDIT} · Picks are a starting point, read the reviews!")
    return e


class RecommendView(discord.ui.View):
    """Private questionnaire: 4 dropdowns + a button. Lives on an ephemeral message."""

    def __init__(self, api: IEMRankingAPI, tunings: list[tuple[str, str]]):
        super().__init__(timeout=600)
        self.api, self.tunings = api, tunings
        self.kind, self.prices, self.tuning, self.sort = "iem", [], [], "reviewer-score"

        self._select("1️⃣ IEMs or headphones?", "kind", [
            discord.SelectOption(label="IEMs", value="iem", emoji="👂", default=True),
            discord.SelectOption(label="Over-ear headphones", value="headphone", emoji="🎧"),
        ])
        self._select("2️⃣ Budget? (any if none picked)", "prices", [
            discord.SelectOption(label=label, value=value) for value, label in PRICES.items()
        ], multi=True)
        if tunings:
            self._select("3️⃣ Sound you like? (any if none picked)", "tuning", [
                discord.SelectOption(label=clip(label, 100), value=value) for value, label in tunings[:25]
            ], multi=True)
        self._select("4️⃣ What matters most?", "sort", [
            discord.SelectOption(label=label, value=sort, emoji=emoji, description=desc, default=sort == self.sort)
            for sort, label, emoji, desc, _ in PRIORITIES
        ])
        go = discord.ui.Button(label="Get my picks", emoji="🎯", style=discord.ButtonStyle.success)
        go.callback = self.on_go
        self.add_item(go)

    def _select(self, placeholder: str, attr: str, options: list[discord.SelectOption], multi: bool = False):
        select = discord.ui.Select(placeholder=placeholder, options=options,
                                   min_values=0 if multi else 1, max_values=len(options) if multi else 1)

        async def callback(interaction: discord.Interaction):
            values = select.values
            setattr(self, attr, values if multi else values[0])
            for opt in select.options:  # keep choices visible if the message is re-rendered
                opt.default = opt.value in values
            await interaction.response.defer()

        select.callback = callback
        self.add_item(select)

    async def on_go(self, interaction: discord.Interaction):
        if not await gate(interaction):
            return
        await interaction.response.defer()
        prices = [] if len(self.prices) == len(PRICES) else self.prices
        try:
            picks = await get_picks(self.api, self.kind, prices, self.tuning, self.sort)
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        summary = answers_line(self.kind, prices, self.tuning, self.sort, dict(self.tunings))
        embed = build_picks_embed(picks, summary, self.sort)
        self.stop()
        await interaction.edit_original_response(embed=embed, view=PicksView(self.api, self.tunings, embed, picks))


class PicksView(discord.ui.View):
    """Under the private results: links to each pick, share publicly, or start over."""

    def __init__(self, api: IEMRankingAPI, tunings: list[tuple[str, str]], embed: discord.Embed,
                 picks: list[tuple[dict, bool]]):
        super().__init__(timeout=600)
        self.api, self.tunings, self.embed = api, tunings, embed
        for i, (item, _) in enumerate(picks, 1):
            self.add_item(discord.ui.Button(label=clip(f"{i}. {item['name']}", 80), url=item["url"], row=0))
        if picks:
            share = discord.ui.Button(label="Share to channel", emoji="📢", style=discord.ButtonStyle.primary, row=1)
            share.callback = self.on_share
            self.add_item(share)
        again = discord.ui.Button(label="Start over", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
        again.callback = self.on_again
        self.add_item(again)

    async def on_share(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="📢 Shared to the channel!", view=None)
        await interaction.followup.send(content=f"🎯 {interaction.user.mention}'s picks", embed=self.embed,
                                        allowed_mentions=discord.AllowedMentions.none())

    async def on_again(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=None, embed=build_recommend_panel(),
                                                view=RecommendView(self.api, self.tunings))


# ---------- Cog ----------

KindChoice = app_commands.Choice[str]
KIND_CHOICES = [KindChoice(name="IEMs", value="iem"), KindChoice(name="Headphones (over-ear)", value="headphone")]


class IEMRanking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api = IEMRankingAPI(API_KEY)
        if not API_KEY:
            log.warning("IEMRANKING_API_KEY not set — /search, /top, /compare, /recommend will reply with a setup notice")

    async def cog_unload(self):
        await self.api.close()

    async def find(self, query: str, kind: str = "all") -> tuple[list[dict], str | None]:
        """Search; returns (results, note). Raises APIError with a user-facing message."""
        if len(query.strip()) < 2:
            raise APIError("Search needs at least 2 characters.")
        data = await self.api.search(clip(query.strip(), 100), kind)
        results = data.get("results") or []
        note = f"No exact match for “{clip(query, 50)}”. Did you mean…" if data.get("partial_match") else None
        return results, note

    # ----- autocomplete (cached; silent on errors so typing never breaks) -----

    async def product_autocomplete(self, interaction: discord.Interaction, current: str):
        if len(norm(current)) < 3:
            return []
        query = clip(current.strip(), 100)
        data = self.api.peek("search", q=query, type="all", limit=10)
        if data is None:
            # Uncached: only when the bot has spare quota and this user isn't hammering it
            if not self.api.can_background():
                return []
            if not is_exempt(interaction.user) and autocomplete_limit.try_hit(interaction.user.id) > 0:
                return []
            try:
                data = await self.api.search(query, "all", 10)
            except APIError:
                return []
        return [
            app_commands.Choice(name=clip(f"{r['name']} · ⭐ {fmt_score(r.get('score'))}", 100),
                                value=clip(r["name"], 100))
            for r in data.get("results") or []
        ]

    async def tuning_autocomplete(self, _interaction: discord.Interaction, current: str):
        try:
            data = await self.api.filters()
        except APIError:
            return []
        q = norm(current)
        return [
            app_commands.Choice(name=clip(label, 100), value=value)
            for value, label in tuning_options(data)
            if q in norm(label) or q in norm(value)
        ][:25]

    # ----- /recommend -----

    @app_commands.command(name="recommend", description="Answer a few questions, get IEM/headphone picks that suit you")
    async def recommend(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tunings = tuning_options(await self.api.filters())
        except APIError as exc:
            if not self.api.key:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            tunings = []  # still works, just without the sound question
        await interaction.followup.send(embed=build_recommend_panel(), view=RecommendView(self.api, tunings),
                                        ephemeral=True)

    # ----- /search -----

    @app_commands.command(name="search", description="Look up an IEM or headphone on IEMRanking.com")
    @app_commands.describe(name="Product or brand name, e.g. Chu 2, Blessing 3, HD600")
    async def search_cmd(self, interaction: discord.Interaction, name: str):
        if not await gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            results, note = await self.find(name)
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not results:
            await interaction.followup.send(f"🔍 Nothing found for “{clip(name, 80)}” on IEMRanking.")
            return
        view = ProfileView(interaction.user.id, results, note=note)
        view.message = await interaction.followup.send(embed=build_profile_embed(results[0], note), view=view)

    search_cmd.autocomplete("name")(product_autocomplete)

    # ----- /top -----

    @app_commands.command(name="top", description="Top IEM/headphone lists from IEMRanking.com (by score, bass, price…)")
    @app_commands.describe(
        sort="What to rank by",
        kind="IEMs or over-ear headphones",
        price="Price range (launch price, USD)",
        tuning="Tuning style, e.g. neutral, warm",
        brand="Brand name(s), comma-separated, e.g. Moondrop, Truthear",
    )
    @app_commands.rename(kind="type")
    @app_commands.choices(
        sort=[app_commands.Choice(name=label, value=value) for value, label in SORTS.items()],
        kind=KIND_CHOICES,
        price=[app_commands.Choice(name=label, value=value) for value, label in PRICES.items()],
    )
    async def top(self, interaction: discord.Interaction, sort: str = "reviewer-score", kind: str = "iem",
                     price: str | None = None, tuning: str | None = None, brand: str | None = None):
        params = {"sort": sort, "type": kind, "price": price, "tuning": tuning, "limit": PAGE_SIZE,
                  "brand": ",".join(h for b in brand.split(",") if (h := handle(b))) if brand else None}
        if not await gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            data = await self.api.ranking(**params)
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        view = RankingView(self.api, params, data)
        view.message = await interaction.followup.send(embed=build_ranking_embed(data), view=view)

    top.autocomplete("tuning")(tuning_autocomplete)

    # ----- /compare -----

    @app_commands.command(name="compare", description="Compare two IEMs/headphones side by side")
    @app_commands.describe(first="First product", second="Second product")
    async def compare(self, interaction: discord.Interaction, first: str, second: str):
        if not await gate(interaction):
            return
        await interaction.response.defer(thinking=True)
        try:
            (ra, _), (rb, _) = await self.find(first), await self.find(second)
        except APIError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        missing = [q for q, r in ((first, ra), (second, rb)) if not r]
        if missing:
            await interaction.followup.send("🔍 Nothing found for " + " and ".join(f"“{clip(q, 60)}”" for q in missing))
            return
        a, b = ra[0], rb[0]
        if a["url"] == b["url"]:
            await interaction.followup.send(f"Both searches found **{a['name']}**. Try more specific names.")
            return
        view = discord.ui.View()
        view.add_item(link_button(clip(a["name"], 80), a["url"]))
        view.add_item(link_button(clip(b["name"], 80), b["url"]))
        await interaction.followup.send(embed=build_compare_embed(a, b), view=view)

    compare.autocomplete("first")(product_autocomplete)
    compare.autocomplete("second")(product_autocomplete)

    # ----- prefix: !search <name> -----

    @commands.command(name="search")
    async def prefix_search(self, ctx: commands.Context, *, name: str):
        if not is_exempt(ctx.author) and (wait := user_limit.try_hit(ctx.author.id)) > 0:
            await ctx.send(f"{ctx.author.mention} {slow_down_message(wait)}", delete_after=10)
            return
        async with ctx.typing():
            try:
                results, note = await self.find(name)
            except APIError as exc:
                await ctx.send(str(exc))
                return
        if not results:
            await ctx.send(f"🔍 Nothing found for “{clip(name, 80)}” on IEMRanking.")
            return
        view = ProfileView(ctx.author.id, results, note=note)
        view.message = await ctx.send(embed=build_profile_embed(results[0], note), view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(IEMRanking(bot))
