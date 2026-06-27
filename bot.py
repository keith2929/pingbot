import os
import logging
import asyncio
import httpx
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
RENDER_API_KEY = os.environ["RENDER_API_KEY"]
UPSTASH_URL = os.environ["UPSTASH_REDIS_REST_URL"]
UPSTASH_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]

RENDER_BASE = "https://api.render.com/v1"
REDIS_KEY = "pingbot:bots"


# ---------- Upstash Redis helpers ----------

async def redis(command: list) -> object:
    url = f"{UPSTASH_URL}/{'/'.join(str(c) for c in command)}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=10)
    return r.json().get("result")


async def load_bots() -> dict:
    result = await redis(["HGETALL", REDIS_KEY])
    if not result:
        return {}
    # HGETALL returns [field, value, field, value, ...]
    it = iter(result)
    return dict(zip(it, it))


async def save_bot(alias: str, service_id: str) -> None:
    await redis(["HSET", REDIS_KEY, alias, service_id])


async def delete_bot(alias: str) -> None:
    await redis(["HDEL", REDIS_KEY, alias])


# ---------- Render API helpers ----------

async def render_post(path: str) -> tuple[int, dict]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{RENDER_BASE}{path}",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            timeout=15,
        )
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


async def render_get(path: str) -> tuple[int, dict]:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{RENDER_BASE}{path}",
            headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            timeout=15,
        )
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


# ---------- Auth ----------

def auth(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


# ---------- Handlers ----------

async def cmd_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /wake [alias]")
        return
    alias = ctx.args[0].lower()
    bots = await load_bots()
    if alias not in bots:
        await update.message.reply_text(f"Unknown alias: {alias}")
        return
    status, body = await render_post(f"/services/{bots[alias]}/resume")
    if status in (200, 202):
        await update.message.reply_text(f"✅ {alias} is waking up.")
    else:
        await update.message.reply_text(f"❌ Failed to wake {alias}: {body.get('message', body)}")


async def cmd_sleep(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /sleep [alias]")
        return
    alias = ctx.args[0].lower()
    bots = await load_bots()
    if alias not in bots:
        await update.message.reply_text(f"Unknown alias: {alias}")
        return
    status, body = await render_post(f"/services/{bots[alias]}/suspend")
    if status in (200, 202):
        await update.message.reply_text(f"💤 {alias} is going to sleep.")
    else:
        await update.message.reply_text(f"❌ Failed to suspend {alias}: {body.get('message', body)}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return

    lines = ["<pre>"]
    lines.append(f"{'Alias':<15} {'Service ID':<30} State")
    lines.append("-" * 60)

    for alias, service_id in bots.items():
        status, body = await render_get(f"/services/{service_id}")
        if status == 200:
            # Render v1 nests service data under "service" key
            svc = body.get("service", body)
            suspended = svc.get("suspended")
            if suspended is True:
                state_str = "suspended"
            elif suspended is False:
                state_str = "running"
            else:
                state_str = svc.get("status", str(body))
        else:
            state_str = "error"
        lines.append(f"{alias:<15} {service_id:<30} {state_str}")

    lines.append("</pre>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /add [alias] [service_id]")
        return
    alias = ctx.args[0].lower()
    service_id = ctx.args[1]
    await save_bot(alias, service_id)
    await update.message.reply_text(f"✅ Registered {alias} → {service_id}")


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /remove [alias]")
        return
    alias = ctx.args[0].lower()
    bots = await load_bots()
    if alias not in bots:
        await update.message.reply_text(f"Unknown alias: {alias}")
        return
    await delete_bot(alias)
    await update.message.reply_text(f"🗑️ Removed {alias}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return
    lines = ["<pre>"]
    lines.append(f"{'Alias':<15} Service ID")
    lines.append("-" * 50)
    for alias, service_id in bots.items():
        lines.append(f"{alias:<15} {service_id}")
    lines.append("</pre>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    lines = [
        "/wake — resume a suspended service",
        "/sleep — suspend a running service",
        "/status — show all bots and their state",
        "/add — register a new bot",
        "/remove — unregister a bot",
        "/list — list all aliases and IDs",
        "/help — this message",
    ]
    await update.message.reply_text("\n".join(lines))


# ---------- Health check ----------

async def health(request):
    return web.Response(text="ok")


# ---------- Entry point ----------

async def main() -> None:
    port = int(os.environ.get("PORT", 8080))
    web_app = web.Application()
    web_app.router.add_get("/", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"Health server on port {port}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("wake", cmd_wake))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))
    await app.initialize()
    await app.bot.set_my_commands([
        BotCommand("wake", "Resume a suspended service"),
        BotCommand("sleep", "Suspend a running service"),
        BotCommand("status", "Show all bots and their state"),
        BotCommand("add", "Register a new bot"),
        BotCommand("remove", "Unregister a bot"),
        BotCommand("list", "List all aliases and IDs"),
        BotCommand("help", "Show command reference"),
    ])
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Controller bot started.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
