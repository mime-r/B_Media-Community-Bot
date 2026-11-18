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
| `/warn <member> <reason>` | — | Warn + DM them; shows their warning count |
| `/kick <member> [reason]` | — | Kick a member. They're DMed the reason first |
| `/ban <user> [reason] [delete_messages]` | — | Ban by member or pasted user ID; optionally delete last hour/24h/7d of messages |
| `/softban <user> [reason] [delete_messages]` | — | Ban + instant unban: removes them and wipes recent messages (default 24h); they can rejoin |
| `/unban <user> [reason]` | — | Lift a ban (paste user ID) |
| `/mute <member> <duration> [reason]` | — | Discord timeout: `10m`, `1h30m`, `2d`… (max 28d; plain number = minutes) |
| `/unmute <member> [reason]` | — | End a timeout |
| `/cases <user>` | — | A member's moderation history (paged, totals per action) |
| `/case view <number>` | — | One case card |
| `/case reason <number> <reason>` | — | Fix a reason; the log message updates too. Case's mod or Manage Server |
| `/case delete <number>` | — | **Manage Server** only. Delete a case (numbers are never reused) |
| `/modconfig logchannel [channel]` | — | **Manage Server** only. Private channel for mod action logs (empty = off; cases are still saved) |
| `/modconfig addrole <role>` / `removerole <role>` | — | **Manage Server** only. Roles allowed to use the mod commands above |
| `/modconfig view` | — | Setup health check: log channel, mod roles, bot permissions, role position |
| `/slowmode <seconds> [channel]` | `@bot slowmode <seconds> [#channel]` | Mods only. Native Discord slowmode with any value 0–21600s (e.g. 3s, below the app's 5s preset) |
| `/setup remove <user>` | — | **Manage Server** only. Delete someone's setup; paste a user ID for members who left |
| **IEMRanking** | | |
| `/search <name>` | `@bot search <name>` | IEMRanking profile: reviewer + user score, rank, launch price, photo. Autocomplete; dropdown to switch to other matches |
| `/top [sort] [type] [price] [tuning] [brand]` | — | Ranking lists (reviewer score, best bass, price-to-performance…), paged with ◀️ ▶️ |
| `/compare <first> <second>` | — | Two products side by side, winner per metric marked ✅ |
| `/recommend` | — | Private questionnaire (type, budget, sound, priority) → top 5 picks, most suitable first. Share to channel or start over |
| `/upgrade [current] [budget]` | — | Better-scoring IEMs/headphones in the same price range + one above. No product given → pick from your setup |
| **Games** | | |
| `/iemdle play` | — | Today's guess-the-IEM puzzle: board + rules |
| `/iemdle guess <iem>` | — | Guess (6 tries). Hints: brand, price, score, rank (⬆️⬇️ 🟩🟨⬛). Share a spoiler-free grid when done |
| `/iemdle stats` | — | Streak, best streak, solve rate, guess distribution |
| `/worldcup start <name> <size> [round_length] [channel] [sort] [type] [price] [entries]` | — | Mods. 8/16-entrant bracket from an IEMRanking list or your own entries; members vote with buttons |
| `/worldcup status` | — | Bracket so far |
| `/worldcup cancel` | — | Mods. Stop the running event |
| `/leaderboard [game]` | — | Top 10 in this server + your rank |

Right-click a user → **Apps → View Setup** also works.

IEMRanking commands need `IEMRANKING_API_KEY` set (env var / `.env`). Answers are cached 5 min to save quota; on rate limit the bot tells users when to retry.

Rate limits (tune at the top of `cogs/iemranking.py`):
- **Per user:** 4 uses/minute and 30/hour across `/search`, `@bot search`, `/top` (+ new pages), `/compare`, `/upgrade`, `/worldcup start` and *Get my picks*. Members with **Manage Server** are exempt.
- **IEMdle:** loads its pool of 150 IEMs once per UTC day (6 calls), saved in the database. Guesses and autocomplete never call the API.
- **Autocomplete:** cached suggestions are free; new lookups capped at 15/min per user and paused when the bot is busy or the key's quota runs low.
- **Bot-wide:** at most `IEMRANKING_MAX_PER_MINUTE` (default 20) real API calls per minute. When the API reports ≤5 requests left, autocomplete pauses so commands get the rest.

## Moderation setup
1. `/modconfig logchannel #mod-log` — a private channel the bot can see, send and embed in.
2. `/modconfig addrole @Moderator` — repeat for each mod role.
3. Drag the bot's role **above** every role it should be able to kick/ban/mute (Server Settings → Roles).

Every action becomes a numbered **case** (per server), saved even when no log channel is set.

Who can use `/warn` `/kick` `/ban` `/softban` `/unban` `/mute` `/unmute` `/cases`: Administrators, Manage Server, members with the matching Discord permission (Kick/Ban/Timeout Members), or anyone with a configured mod role. The bot acts with **its own** permissions, so mod roles don't need Discord permissions themselves. The commands are visible to everyone but refuse non-mods.

Safety rules: no acting on yourself, the owner, the bot, anyone with an equal/higher top role, or other mods (admins excepted). Targets are DMed before kick/ban/softban and on mute; the log notes if the DM failed. Every action also lands in Discord's Audit Log with the mod's name and reason.

## Games
- **IEMdle**: everyone gets the same mystery IEM each UTC day (from the top 100 by reviewer score; no repeat within 60 days). Points: 7 − tries.
- **IEM World Cup**: mods run a bracket; each match is its own message with two vote buttons (one vote per member, tap again to switch). Rounds close automatically (checked every minute); ties go to the higher reviewer score. Buttons keep working after a restart. Correct votes earn 1 prediction point. One event per server at a time.
- Test a World Cup quickly with `round_length: 1m`.

## Data
Setups, moderation settings and cases, IEMdle plays, World Cup events/votes and game points are stored in `bmedia.db` (SQLite, created automatically next to `main.py`). Override location with `DB_PATH` env var. **Back this file up** — deleting it wipes all profiles.

## Legal
- [Terms of Service](TERMS.md)
- [Privacy Policy](PRIVACY.md)

## 1. Create the bot (Discord Developer Portal)
1. https://discord.com/developers/applications → **New Application** → name `B_Media Community Bot`.
2. **Bot** tab → **Reset Token** → copy token (keep secret).
3. Same tab → no privileged intents needed (Server Members and Message Content can stay off). Prefix commands then only work by @mentioning the bot, e.g. `@B_Media Community Bot ping`.
4. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`; permissions: Send Messages, Embed Links, Read Message History, View Channels, Manage Channels (for `/slowmode`), Kick Members, Ban Members, Moderate Members (for `/mute`). Already invited? Add these to the bot's role in Server Settings → Roles instead. Open URL → invite to server.

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
