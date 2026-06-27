# Pingbot

A Telegram controller bot that suspends and resumes other bots on Render, saving free tier hours.

## Features

- Wake or suspend any registered Render service via inline keyboard
- Step-by-step `/add` and `/remove` flows — no need to type service IDs inline
- Live status check for all registered bots
- Persistent bot registry via Upstash Redis
- Restricted to a single authorized Telegram user

## Commands

| Command | Description |
|---|---|
| `/wake` | Resume a suspended service |
| `/sleep` | Suspend a running service |
| `/status` | Show all bots and their current state |
| `/add` | Register a new bot (step-by-step) |
| `/remove` | Unregister a bot (button picker) |
| `/list` | List all aliases and service IDs |
| `/help` | Show command reference |
| `/cancel` or `/abort` | Exit current flow |

## Stack

- Python 3
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 21.6
- [httpx](https://www.python-httpx.org/) for async Render API calls
- [aiohttp](https://docs.aiohttp.org/) for health check endpoint
- [Upstash Redis](https://upstash.com/) for persistent bot registry

## Deployment (Render)

1. Fork this repo and push to GitHub
2. Create a new **Web Service** on [Render](https://render.com)
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
3. Set the following environment variables:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USER_ID` | Your Telegram numeric user ID (get from [@userinfobot](https://t.me/userinfobot)) |
| `RENDER_API_KEY` | Render API key from Account Settings → API Keys |
| `UPSTASH_REDIS_REST_URL` | Upstash Redis REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash Redis REST token |

4. Set up a free [UptimeRobot](https://uptimerobot.com) monitor on your Render URL (every 5 min) to prevent the free tier from spinning down

## Adding bots

After the bot is running, use `/add` in Telegram:

```
/add
> What would you like to call this bot?
password
> Now send the Render service ID (starts with srv-)
srv-xxxxxxxxxxxxxxxx
> ✅ Registered password → srv-xxxxxxxxxxxxxxxx
```

Find a service's ID in the Render dashboard URL:
`https://dashboard.render.com/web/srv-xxxxxxxxxxxxxxxx`
