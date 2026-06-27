import os
import json
import logging
import asyncio
import httpx
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
RENDER_API_KEY = os.environ["RENDER_API_KEY"]

BOTS_FILE = Path("/data/bots.json")
RENDER_BASE = "https://api.render.com/v1"


def load_bots() -> dict:
    if BOTS_FILE.exists():
        return json.loads(BOTS_FILE.read_text())
    return {}


def save_bots(bots: dict) -> None:
    BOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BOTS_FILE.write_text(json.dumps(bots, indent=2))


def auth(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


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


async def cmd_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /wake <alias>")
        return
    alias = ctx.args[0].lower()
    bots = load_bots()
    if alias not in bots:
        await update.message.reply_text(f"Unknown alias: {alias}")
        return
    service_id = bots[alias]
    status, body = await render_post(f"/services/{service_id}/resume")
    if status in (200, 202):
        await update.message.reply_text(f"✅ {alias} is waking up.")
    else:
        msg = body.get("message", str(body))
        await update.message.reply_text(f"❌ Failed to wake {alias}: {msg}")


async def cmd_sleep(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /sleep <alias>")
        return
    alias = ctx.args[0].lower()
    bots = load_bots()
    if alias not in bots:
        await update.message.reply_text(f"Unknown alias: {alias}")
        return
    service_id = bots[alias]
    status, body = await render_post(f"/services/{service_id}/suspend")
    if status in (200, 202):
        await update.message.reply_text(f"💤 {alias} is going to sleep.")
    else:
        msg = body.get("message", str(body))
        await update.message.reply_text(f"❌ Failed to suspend {alias}: {msg}")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return

    lines = ["<pre>"]
    lines.append(f"{'Alias':<15} {'Service ID':<30} State")
    lines.append("-" * 60)

    for alias, service_id in bots.items():
        status, body = await render_get(f"/services/{service_id}")
        if status == 200:
            state = body.get("suspended", None)
            if state is True:
                state_str = "suspended"
            elif state is False:
                state_str = "running"
            else:
                state_str = body.get("status", "unknown")
        else:
            state_str = "error"
        lines.append(f"{alias:<15} {service_id:<30} {state_str}")

    lines.append("</pre>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Usage: /add <alias> <service_id>")
        return
    alias = ctx.args[0].lower()
    service_id = ctx.args[1]
    bots = load_bots()
    bots[alias] = service_id
    save_bots(bots)
    await update.message.reply_text(f"✅ Registered {alias} → {service_id}")


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /remove <alias>")
        return
    alias = ctx.args[0].lower()
    bots = load_bots()
    if alias not in bots:
        await update.message.reply_text(f"Unknown alias: {alias}")
        return
    del bots[alias]
    save_bots(bots)
    await update.message.reply_text(f"🗑️ Removed {alias}")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = load_bots()
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
    text = (
        "/wake <alias>              — resume a suspended service\n"
        "/sleep <alias>             — suspend a running service\n"
        "/status                    — show all bots and their state\n"
        "/add <alias> <service_id>  — register a new bot\n"
        "/remove <alias>            — unregister a bot\n"
        "/list                      — list all aliases and IDs\n"
        "/help                      — this message"
    )
    await update.message.reply_text(f"<pre>{text}</pre>", parse_mode="HTML")


async def health(request):
    return web.Response(text="ok")


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
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Controller bot started.")
    await asyncio.Event().wait()  # block forever


if __name__ == "__main__":
    asyncio.run(main())
