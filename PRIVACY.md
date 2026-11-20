# Privacy Policy — B_Media Community Bot

_Last updated: 18 November 2026_

This policy explains what data B_Media Community Bot ("the bot", "we") collects, why, and how you can control it. By using the bot you agree to this policy.

## 1. Data we store

The bot keeps data permanently only when you save a setup, play a game, or when a moderator takes action on you.

| Data | When | Why |
|---|---|---|
| Your Discord user ID | You use `/setup edit` | To link the profile to you |
| Setup fields you type in (source/DAC/amp, headphones/IEMs/speakers, accessories, genres, notes) | You use `/setup edit` | To show your setup with `/setup view` and let others find it with `/whohas` |
| Time of last update | You save a setup | To show when the profile was last changed |
| IEMdle guesses and results (which IEMs you guessed each day, solved or not) | You use `/iemdle guess` | To show your board, streak and stats |
| IEM Cup votes (which product you voted for in each match) | You press a vote button | To count votes and award prediction points |
| Game points per server | You score in IEMdle or IEM Cup | To show `/leaderboard` |
| Moderation cases: your user ID and username, the moderator's ID and username, the action (warn/kick/ban/mute…), reason, duration, time, and whether the DM reached you | A moderator uses a moderation command on you | So server staff can review a member's history with `/cases` |

This data is stored in a database on the bot's hosting server.

For each server, the bot also stores moderation settings chosen by server admins: the server ID, the mod log channel ID and the IDs of moderator roles, and the game channel settings (channel ID, daily IEMdle on/off, ping role ID). It also stores IEM Cup events (name, channel, thread, entrants) and the IDs of IEMdle threads, which are not personal data.

## 2. Data we process but do not store

- **Messages:** The bot does not read general chat. Discord only shows it the content of messages that @mention it, which it checks for commands (e.g. `@B_Media Community Bot help`). Message content is not saved or logged.
- **Server and member info:** Commands like `/serverinfo`, `/userinfo` and `/whohas` read info Discord already provides (names, avatars, roles, join dates) to build a reply. This is not saved.
- **Rate limits and cooldowns:** Your user ID and usage count are kept in memory for up to one hour to stop spam. They are cleared when the bot restarts.
- **Cached results:** IEMRanking search results are kept in memory for about 5 minutes to reduce API use. These contain product data, not personal data.
- **Moderation notices:** When a moderator acts on someone, the bot DMs that person the reason and posts the case in the server's private mod log channel. Discord also records the action in the server's Audit Log. Those Discord copies are controlled by the server's staff.
- **Error logs:** If a command fails, technical error details may be written to the host's console logs for debugging.

## 3. Third parties

- **Discord** — the bot runs on Discord. Discord's own [Privacy Policy](https://discord.com/privacy) applies to your use of Discord.
- **IEMRanking** — when you use `/search`, `/top`, `/compare`, `/recommend` or `/upgrade`, your search terms and filters are sent to the IEMRanking API to get results (for `/upgrade` without a product, that includes an IEM/headphone name from your setup). Your Discord identity is not sent.
- **Hosting provider** — the bot and its database run on a third-party hosting service.

We do not sell, rent or share your data with anyone else, and we do not use it for advertising.

## 4. Who can see your data

Setup profiles are public inside servers where the bot is present: any member can view your setup with `/setup view` or find you with `/whohas`. Don't put anything in your setup you wouldn't want other members to see.

IEMdle guesses are private to you. When you finish a puzzle in a server with a game channel, the bot posts your result in that day's IEMdle thread: your name, the number of tries, your streak and a coloured grid (no product names). Elsewhere, nothing is posted unless you press **Share result**. Leaderboard points are visible to members of that server. IEM Cup vote counts are public, but who voted for what is not shown. Moderation cases are visible only to that server's moderators.

## 5. How long we keep it

- **Setup:** until you delete it, a server moderator removes it (e.g. for breaking the rules, or after you leave), or the bot is shut down and its database removed.
- **Game data:** until you ask us to delete it, or the bot is shut down.
- **Moderation cases:** until that server's staff delete them with `/case delete`, or the bot is shut down. Each server decides how long to keep its own records.

## 6. Your choices

- **Delete your data:** run `/setup clear` at any time. Your profile is removed from the database straight away.
- **Change your data:** run `/setup edit` to update any field.
- **Ask us:** contact us (see below) to request a copy of your stored data, or to delete your game data. For moderation cases, contact that server's staff; they decide whether to remove them.

## 7. Children

The bot is meant for Discord users who meet Discord's minimum age requirement (13 or older, or higher where local law requires).

## 8. Changes

We may update this policy. Changes take effect when posted here, and the "Last updated" date will change. Continuing to use the bot means you accept the updated policy.

## 9. Contact

Questions or data requests: ask in the B_Media Discord server or open an issue on the [GitHub repository](https://github.com/mime-r/B_Media-Community-Bot/issues).
