# Telegram XP Bot

Track XP from Telegram reactions (+10 / -10) and weekly leaderboard.

## Commands
- /start — show intro
- /rules — show XP rules
- /my — show your XP
- /top — top 15 overall
- /week — weekly summary (Mon 00:00 Asia/Bangkok to now)

## Deploy to Render
1. Push this repo to GitHub
2. Go to Render Dashboard > New > Blueprint
3. Pick this repo (render.yaml included)
4. Set Environment Variable: BOT_TOKEN = your token from @BotFather
5. Deploy 🚀

## Deploy to Railway
1. Push repo to GitHub
2. Create new project in Railway > Deploy from GitHub
3. Set Environment Variable: BOT_TOKEN = your token
4. Railway will use Procfile

## Notes
- Bot must be admin in the Channel/Group
- Channel must disable anonymous reactions to get per-user XP
- Data stored in SQLite (engage.db), may reset if redeploy on free tier
