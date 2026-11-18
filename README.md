# B_Media Community Bot

Simple Discord bot (discord.py) with slash commands, plus @mention commands.

The bot uses **no privileged intents**, so it can't read normal messages. Text commands therefore need an @mention in servers: `@B_Media Community Bot ping`. In DMs, plain `!ping` works too (prefix set by `PREFIX`, default `!`).

## Commands
| Slash | @mention | What |
|---|---|---|
| **General** | | |
| `/help` | `@bot help` | List commands |
| `/ping` | `@bot ping` | Latency |
| `/hello` | `@bot hello` | Greeting |
| `/serverinfo` | `@bot serverinfo` | Server stats |
| `/userinfo [member]` | `@bot userinfo [member]` | Member info |
| **Setup profiles** | | |
| `/setup edit` | — | Panel with 3 forms: Source/DAC/Amp · Headphones/IEMs/Speakers · Extras (accessories, genres, notes) |
| `/setup view [member]` | `@bot setup [member]` | Show a setup embed |
| `/setup clear` | — | Delete your setup (with confirm) |
| `/whohas <gear>` | `@bot whohas <gear>` | Find members owning gear (autocomplete, fuzzy: `hd800s` matches `HD 800 S`) |
| **Moderation** | | |
| `/slowmode <seconds> [channel]` | `@bot slowmode <seconds> [#channel]` | Mods only. Native Discord slowmode with any value 0–21600s (e.g. 3s, below the app's 5s preset) |
| **IEMRanking** | | |
| `/search <name>` | `@bot search <name>` | IEMRanking profile: reviewer + user score, rank, launch price, photo. Autocomplete; dropdown to switch to other matches |
| `/top [sort] [type] [price] [tuning] [brand]` | — | Ranking lists (reviewer score, best bass, price-to-performance…), paged with ◀️ ▶️ |
| `/compare <first> <second>` | — | Two products side by side, winner per metric marked ✅ |
| `/recommend` | — | Private questionnaire (type, budget, sound, priority) → top 5 picks, most suitable first. Share to channel or start over |

Right-click a user → **Apps → View Setup** also works.

IEMRanking commands need `IEMRANKING_API_KEY` set (env var / `.env`). Answers are cached 5 min to save quota; on rate limit the bot tells users when to retry.

Rate limits (tune at the top of `cogs/iemranking.py`):
- **Per user:** 4 uses/minute and 30/hour across `/search`, `@bot search`, `/top` (+ new pages), `/compare` and *Get my picks*. Members with **Manage Server** are exempt.
- **Autocomplete:** cached suggestions are free; new lookups capped at 15/min per user and paused when the bot is busy or the key's quota runs low.
- **Bot-wide:** at most `IEMRANKING_MAX_PER_MINUTE` (default 20) real API calls per minute. When the API reports ≤5 requests left, autocomplete pauses so commands get the rest.

## Data
Setups are stored in `bmedia.db` (SQLite, created automatically next to `main.py`). Override location with `DB_PATH` env var. **Back this file up** — deleting it wipes all profiles.

## Legal
- [Terms of Service](TERMS.md)
- [Privacy Policy](PRIVACY.md)

## 1. Create the bot (Discord Developer Portal)
1. https://discord.com/developers/applications → **New Application** → name `B_Media Community Bot`.
2. **Bot** tab → **Reset Token** → copy token (keep secret).
3. Same tab → no privileged intents needed (Server Members and Message Content can stay off). Prefix commands then only work by @mentioning the bot, e.g. `@B_Media Community Bot ping`.
4. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`; permissions: Send Messages, Embed Links, Read Message History, View Channels, Manage Channels (for `/slowmode`). Open URL → invite to server.

## 2. Run locally (optional)
```
pip install -r requirements.txt
copy .env.example .env   # then paste token into .env
python main.py
```

## 3. Deploy on Pella
1. Create a new **Python** server on Pella.
2. Upload `main.py`, `requirements.txt` and the `cogs/` folder (don't upload `.env` with a real token publicly).
3. Set the token: add `DISCORD_TOKEN` in Pella's environment variables / `.env` file panel (plus `IEMRANKING_API_KEY` and optional `PREFIX`).
4. Set startup/main file to `main.py` and start. Pella installs `requirements.txt` automatically.
5. Console should show `B_Media Community Bot online as ...`. Slash commands may take a minute to appear.
