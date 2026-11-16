# B_Media Community Bot

Simple Discord bot (discord.py) with slash + prefix commands and welcome messages.

## Commands
| Slash | Prefix | What |
|---|---|---|
| `/help` | `!help` | List commands |
| `/ping` | `!ping` | Latency |
| `/hello` | `!hello` | Greeting |
| `/serverinfo` | `!serverinfo` | Server stats |
| `/userinfo [member]` | `!userinfo [member]` | Member info |

Auto welcome embed when someone joins.

## 1. Create the bot (Discord Developer Portal)
1. https://discord.com/developers/applications → **New Application** → name `B_Media Community Bot`.
2. **Bot** tab → **Reset Token** → copy token (keep secret).
3. Same tab → enable **Server Members Intent** and **Message Content Intent**.
4. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`; permissions: Send Messages, Embed Links, Read Message History, View Channels. Open URL → invite to server.

## 2. Run locally (optional)
```
pip install -r requirements.txt
copy .env.example .env   # then paste token into .env
python main.py
```

## 3. Deploy on Pella
1. Create a new **Python** server on Pella.
2. Upload `main.py` and `requirements.txt` (don't upload `.env` with a real token publicly).
3. Set the token: add `DISCORD_TOKEN` in Pella's environment variables / `.env` file panel (plus optional `WELCOME_CHANNEL_ID`, `PREFIX`).
4. Set startup/main file to `main.py` and start. Pella installs `requirements.txt` automatically.
5. Console should show `B_Media Community Bot online as ...`. Slash commands may take a minute to appear.
