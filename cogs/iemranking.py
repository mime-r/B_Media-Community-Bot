"""IEMRanking.com lookups: product profiles, ranking lists and head-to-head compares.

API docs: https://www.iemranking.com/page/api-docs
Needs IEMRANKING_API_KEY in the environment. Keep it server-side only.
"""

import logging
import math
import os
import re
import time

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


class APIError(Exception):
    """Error with a message that is safe to show to users."""


# ---------- API client ----------

class IEMRankingAPI:
    def __init__(self, key: str | None):
        self.key = key
        self.session: aiohttp.ClientSession | None = None
        self.cache: dict[tuple, tuple[float, dict]] = {}
        self.blocked_until = 0.0  # set from Retry-After on 429

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
        params = {k: str(v) for k, v in params.items() if v not in (None, "")}
        cache_key = (endpoint, tuple(sorted(params.items())))
        now = time.monotonic()
        if (hit := self.cache.get(cache_key)) and hit[0] > now:
            return hit[1]
        if self.blocked_until > now:
            raise APIError(f"⏳ IEMRanking rate limit hit. Try again <t:{int(time.time() + self.blocked_until - now)}:R>.")

        try:
            async with self._session().get(API_BASE + endpoint, params=params) as resp:
                if resp.status == 429:
                    retry = int(resp.headers.get("Retry-After", "60") or 60)
                    self.blocked_until = now + retry
                    raise APIError(f"⏳ IEMRanking rate limit hit. Try again <t:{int(time.time()) + retry}:R>.")
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
        title=f"🏆 {data.get('sort_label') or SORTS.get(sort, sort)} · {TYPE_PLURAL.get(kind, kind).capitalize()}",
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
            offset = max(0, self.data.get("offset", 0) + step)
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


# ---------- Cog ----------

KindChoice = app_commands.Choice[str]
KIND_CHOICES = [KindChoice(name="IEMs", value="iem"), KindChoice(name="Headphones (over-ear)", value="headphone")]


class IEMRanking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api = IEMRankingAPI(API_KEY)
        if not API_KEY:
            log.warning("IEMRANKING_API_KEY not set — /search, /top, /compare will reply with a setup notice")

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

    async def product_autocomplete(self, _interaction: discord.Interaction, current: str):
        if len(norm(current)) < 3:
            return []
        try:
            data = await self.api.search(clip(current.strip(), 100), "all", 10)
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
        options = data.get("tuning") or (data.get("filters") or {}).get("tuning") or []
        q = norm(current)
        return [
            app_commands.Choice(name=clip(str(o.get("label") or o.get("value")), 100), value=str(o["value"]))
            for o in options
            if isinstance(o, dict) and o.get("value") and (q in norm(str(o.get("label"))) or q in norm(str(o["value"])))
        ][:25]

    # ----- /search -----

    @app_commands.command(name="search", description="Look up an IEM or headphone on IEMRanking.com")
    @app_commands.describe(name="Product or brand name, e.g. Chu 2, Blessing 3, HD600")
    async def search_cmd(self, interaction: discord.Interaction, name: str):
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
