# Privacy Policy — B_Media Community Bot

_Last updated: 18 November 2026_

This policy explains what data B_Media Community Bot ("the bot", "we") collects, why, and how you can control it. By using the bot you agree to this policy.

## 1. Data we store

The bot only keeps data permanently when you choose to save a setup profile.

| Data | When | Why |
|---|---|---|
| Your Discord user ID | You use `/setup edit` | To link the profile to you |
| Setup fields you type in (source/DAC/amp, headphones/IEMs/speakers, accessories, genres, notes) | You use `/setup edit` | To show your setup with `/setup view` and let others find it with `/whohas` |
| Time of last update | You save a setup | To show when the profile was last changed |

This data is stored in a database on the bot's hosting server.

## 2. Data we process but do not store

- **Messages:** The bot does not read general chat. Discord only shows it the content of messages that @mention it, which it checks for commands (e.g. `@B_Media Community Bot help`). Message content is not saved or logged.
- **Server and member info:** Commands like `/serverinfo`, `/userinfo` and `/whohas` read info Discord already provides (names, avatars, roles, join dates) to build a reply. This is not saved.
- **Rate limits and cooldowns:** Your user ID and usage count are kept in memory for up to one hour to stop spam. They are cleared when the bot restarts.
- **Cached results:** IEMRanking search results are kept in memory for about 5 minutes to reduce API use. These contain product data, not personal data.
- **Error logs:** If a command fails, technical error details may be written to the host's console logs for debugging.

## 3. Third parties

- **Discord** — the bot runs on Discord. Discord's own [Privacy Policy](https://discord.com/privacy) applies to your use of Discord.
- **IEMRanking** — when you use `/search`, `/top`, `/compare` or `/recommend`, your search terms and filters are sent to the IEMRanking API to get results. Your Discord identity is not sent.
- **Hosting provider** — the bot and its database run on a third-party hosting service.

We do not sell, rent or share your data with anyone else, and we do not use it for advertising.

## 4. Who can see your data

Setup profiles are public inside servers where the bot is present: any member can view your setup with `/setup view` or find you with `/whohas`. Don't put anything in your setup you wouldn't want other members to see.

## 5. How long we keep it

Your setup is kept until you delete it, or until the bot is shut down and its database removed.

## 6. Your choices

- **Delete your data:** run `/setup clear` at any time. Your profile is removed from the database straight away.
- **Change your data:** run `/setup edit` to update any field.
- **Ask us:** contact us (see below) to request a copy of your stored data or ask us to delete it.

## 7. Children

The bot is meant for Discord users who meet Discord's minimum age requirement (13 or older, or higher where local law requires).

## 8. Changes

We may update this policy. Changes take effect when posted here, and the "Last updated" date will change. Continuing to use the bot means you accept the updated policy.

## 9. Contact

Questions or data requests: ask in the B_Media Discord server or open an issue on the [GitHub repository](https://github.com/mime-r/B_Media-Community-Bot/issues).
