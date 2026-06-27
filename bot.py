import os
import logging
import asyncio
import httpx
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes,
)
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
RENDER_API_KEY = os.environ["RENDER_API_KEY"]
UPSTASH_URL = os.environ["UPSTASH_REDIS_REST_URL"]
UPSTASH_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]

RENDER_BASE = "https://api.render.com/v1"
REDIS_BOTS_KEY = "pingbot:bots"
REDIS_SUPABASE_KEY = "pingbot:supabase"

SUPABASE_PING_INTERVAL = 6 * 24 * 60 * 60  # 6 days in seconds

# Conversation states
ADD_NAME, ADD_ID = range(2)
REMOVE_CONFIRM = range(1)
SB_ADD_NAME, SB_ADD_URL, SB_ADD_KEY = range(3)
SB_REMOVE_CONFIRM = range(1)


# ---------- Upstash Redis helpers ----------

async def redis(command: list) -> object:
    url = f"{UPSTASH_URL}/{'/'.join(str(c) for c in command)}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=10)
    return r.json().get("result")


async def hgetall(key: str) -> dict:
    result = await redis(["HGETALL", key])
    if not result:
        return {}
    it = iter(result)
    return dict(zip(it, it))


# ---------- Bot registry ----------

async def load_bots() -> dict:
    return await hgetall(REDIS_BOTS_KEY)

async def save_bot(alias: str, service_id: str) -> None:
    await redis(["HSET", REDIS_BOTS_KEY, alias, service_id])

async def delete_bot(alias: str) -> None:
    await redis(["HDEL", REDIS_BOTS_KEY, alias])


# ---------- Supabase registry ----------
# Stored as JSON strings: { "url": "...", "key": "..." }

import json

async def load_supabase() -> dict:
    raw = await hgetall(REDIS_SUPABASE_KEY)
    return {name: json.loads(val) for name, val in raw.items()}

async def save_supabase(name: str, url: str, key: str) -> None:
    await redis(["HSET", REDIS_SUPABASE_KEY, name, json.dumps({"url": url, "key": key})])

async def delete_supabase(name: str) -> None:
    await redis(["HDEL", REDIS_SUPABASE_KEY, name])


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


# ---------- Wake / Sleep ----------

async def bot_picker(update: Update, action: str) -> None:
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered. Use /add first.")
        return
    buttons = [
        [InlineKeyboardButton(alias, callback_data=f"{action}:{alias}")]
        for alias in bots
    ]
    label = "wake" if action == "wake" else "suspend"
    await update.message.reply_text(
        f"Which bot would you like to {label}?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if ctx.args:
        alias = ctx.args[0].lower()
        bots = await load_bots()
        if alias not in bots:
            await update.message.reply_text(f"Unknown alias: {alias}")
            return
        await do_wake(update.message.reply_text, alias, bots[alias])
    else:
        await bot_picker(update, "wake")


async def cmd_sleep(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if ctx.args:
        alias = ctx.args[0].lower()
        bots = await load_bots()
        if alias not in bots:
            await update.message.reply_text(f"Unknown alias: {alias}")
            return
        await do_sleep(update.message.reply_text, alias, bots[alias])
    else:
        await bot_picker(update, "sleep")


async def do_wake(reply, alias: str, service_id: str) -> None:
    status, body = await render_post(f"/services/{service_id}/resume")
    if status in (200, 202):
        await reply(f"✅ {alias} is waking up.")
    else:
        await reply(f"❌ Failed to wake {alias}: {body.get('message', body)}")


async def do_sleep(reply, alias: str, service_id: str) -> None:
    status, body = await render_post(f"/services/{service_id}/suspend")
    if status in (200, 202):
        await reply(f"💤 {alias} is going to sleep.")
    else:
        await reply(f"❌ Failed to suspend {alias}: {body.get('message', body)}")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ALLOWED_USER_ID:
        await query.answer()
        return
    await query.answer()
    action, alias = query.data.split(":", 1)
    bots = await load_bots()
    if alias not in bots:
        await query.edit_message_text(f"Unknown alias: {alias}")
        return
    reply = query.edit_message_text
    if action == "wake":
        await do_wake(reply, alias, bots[alias])
    elif action == "sleep":
        await do_sleep(reply, alias, bots[alias])


# ---------- Status / List ----------

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return

    lines = []
    for alias, service_id in bots.items():
        status, body = await render_get(f"/services/{service_id}")
        if status == 200:
            svc = body.get("service", body)
            suspended = svc.get("suspended", "")
            if suspended == "not_suspended":
                icon, state_str = "🟢", "running"
            elif suspended == "suspended":
                icon, state_str = "⏸️", "suspended"
            else:
                icon, state_str = "❓", suspended or "unknown"
        else:
            icon, state_str = "❌", "error"
        lines.append(f"{icon} <b>{alias}</b> — {state_str}\n<code>{service_id}</code>")

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return
    lines = []
    for alias, service_id in bots.items():
        lines.append(f"• <b>{alias}</b>\n<code>{service_id}</code>")
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


# ---------- Add bot (conversation) ----------

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not auth(update):
        return ConversationHandler.END
    await update.message.reply_text("What would you like to call this bot? (alias)")
    return ADD_NAME


async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["add_alias"] = update.message.text.strip().lower()
    await update.message.reply_text(
        f"Got it: <b>{ctx.user_data['add_alias']}</b>\n\nNow send the Render service ID (starts with <code>srv-</code>)",
        parse_mode="HTML",
    )
    return ADD_ID


async def add_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    service_id = update.message.text.strip()
    alias = ctx.user_data.pop("add_alias")
    await save_bot(alias, service_id)
    await update.message.reply_text(f"✅ Registered <b>{alias}</b> → <code>{service_id}</code>", parse_mode="HTML")
    return ConversationHandler.END


async def add_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------- Remove bot (conversation) ----------

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not auth(update):
        return ConversationHandler.END
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(alias, callback_data=f"remove:{alias}")]
        for alias in bots
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="remove:__cancel__")])
    await update.message.reply_text(
        "Which bot would you like to remove?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return REMOVE_CONFIRM


async def remove_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, alias = query.data.split(":", 1)
    if alias == "__cancel__":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END
    await delete_bot(alias)
    await query.edit_message_text(f"🗑️ Removed <b>{alias}</b>", parse_mode="HTML")
    return ConversationHandler.END


# ---------- Supabase ping ----------

async def ping_supabase_projects(bot=None) -> list[str]:
    projects = await load_supabase()
    if not projects:
        return []
    results = []
    for name, creds in projects.items():
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{creds['url']}/storage/v1/bucket",
                    headers={"apikey": creds["key"], "Authorization": f"Bearer {creds['key']}"},
                    timeout=15,
                )
            if r.status_code < 500:
                results.append(f"🟢 <b>{name}</b> — ok ({r.status_code})")
            else:
                results.append(f"🔴 <b>{name}</b> — error ({r.status_code})")
        except Exception as e:
            results.append(f"❌ <b>{name}</b> — {e}")
    if bot:
        msg = "🗄️ <b>Weekly Supabase ping</b>\n\n" + "\n".join(results)
        await bot.send_message(chat_id=ALLOWED_USER_ID, text=msg, parse_mode="HTML")
    return results


async def supabase_ping_loop(bot) -> None:
    while True:
        await asyncio.sleep(SUPABASE_PING_INTERVAL)
        logger.info("Running scheduled Supabase ping")
        await ping_supabase_projects(bot)


async def cmd_pingsupabase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    projects = await load_supabase()
    if not projects:
        await update.message.reply_text("No Supabase projects registered. Use /addsupabase first.")
        return
    await update.message.reply_text("Pinging Supabase projects...")
    results = await ping_supabase_projects()
    await update.message.reply_text("\n".join(results), parse_mode="HTML")


# ---------- Add Supabase (conversation) ----------

async def cmd_addsupabase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not auth(update):
        return ConversationHandler.END
    await update.message.reply_text("What would you like to call this Supabase project? (alias)")
    return SB_ADD_NAME


async def sb_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["sb_name"] = update.message.text.strip().lower()
    await update.message.reply_text(
        f"Got it: <b>{ctx.user_data['sb_name']}</b>\n\nNow send the Supabase project URL\n(e.g. <code>https://xxxx.supabase.co</code>)",
        parse_mode="HTML",
    )
    return SB_ADD_URL


async def sb_add_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["sb_url"] = update.message.text.strip().rstrip("/")
    await update.message.reply_text(
        "Now send the <b>anon/public API key</b> for this project\n(Supabase dashboard → Project Settings → API)",
        parse_mode="HTML",
    )
    return SB_ADD_KEY


async def sb_add_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    name = ctx.user_data.pop("sb_name")
    url = ctx.user_data.pop("sb_url")
    await save_supabase(name, url, key)
    await update.message.reply_text(f"✅ Registered Supabase project <b>{name}</b>", parse_mode="HTML")
    return ConversationHandler.END


# ---------- Remove Supabase (conversation) ----------

async def cmd_removesupabase(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not auth(update):
        return ConversationHandler.END
    projects = await load_supabase()
    if not projects:
        await update.message.reply_text("No Supabase projects registered.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"sbremove:{name}")]
        for name in projects
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="sbremove:__cancel__")])
    await update.message.reply_text(
        "Which Supabase project would you like to remove?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SB_REMOVE_CONFIRM


async def sb_remove_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, name = query.data.split(":", 1)
    if name == "__cancel__":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END
    await delete_supabase(name)
    await query.edit_message_text(f"🗑️ Removed Supabase project <b>{name}</b>", parse_mode="HTML")
    return ConversationHandler.END


# ---------- Help ----------

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    lines = [
        "🤖 <b>Render bots</b>",
        "/wake — resume a suspended service",
        "/sleep — suspend a running service",
        "/status — show all bots and their state",
        "/add — register a new bot",
        "/remove — unregister a bot",
        "/list — list all aliases and IDs",
        "",
        "🗄️ <b>Supabase</b>",
        "/addsupabase — register a Supabase project",
        "/removesupabase — unregister a Supabase project",
        "/pingsupabase — ping all projects now",
        "",
        "/help — this message",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_ID:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_id)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel), CommandHandler("abort", add_cancel)],
        conversation_timeout=120,
    )

    remove_conv = ConversationHandler(
        entry_points=[CommandHandler("remove", cmd_remove)],
        states={
            REMOVE_CONFIRM: [CallbackQueryHandler(remove_confirm, pattern="^remove:")],
        },
        fallbacks=[CommandHandler("cancel", add_cancel), CommandHandler("abort", add_cancel)],
        conversation_timeout=120,
    )

    sb_add_conv = ConversationHandler(
        entry_points=[CommandHandler("addsupabase", cmd_addsupabase)],
        states={
            SB_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_add_name)],
            SB_ADD_URL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_add_url)],
            SB_ADD_KEY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_add_key)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel), CommandHandler("abort", add_cancel)],
        conversation_timeout=120,
    )

    sb_remove_conv = ConversationHandler(
        entry_points=[CommandHandler("removesupabase", cmd_removesupabase)],
        states={
            SB_REMOVE_CONFIRM: [CallbackQueryHandler(sb_remove_confirm, pattern="^sbremove:")],
        },
        fallbacks=[CommandHandler("cancel", add_cancel), CommandHandler("abort", add_cancel)],
        conversation_timeout=120,
    )

    app.add_handler(add_conv)
    app.add_handler(remove_conv)
    app.add_handler(sb_add_conv)
    app.add_handler(sb_remove_conv)
    app.add_handler(CommandHandler("wake", cmd_wake))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("pingsupabase", cmd_pingsupabase))
    app.add_handler(CommandHandler("help", cmd_help))

    await app.initialize()
    await app.bot.set_my_commands([
        BotCommand("wake", "Resume a suspended service"),
        BotCommand("sleep", "Suspend a running service"),
        BotCommand("status", "Show all bots and their state"),
        BotCommand("add", "Register a new bot"),
        BotCommand("remove", "Unregister a bot"),
        BotCommand("list", "List all aliases and IDs"),
        BotCommand("addsupabase", "Register a Supabase project"),
        BotCommand("removesupabase", "Unregister a Supabase project"),
        BotCommand("pingsupabase", "Ping all Supabase projects now"),
        BotCommand("help", "Show command reference"),
    ])
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Controller bot started.")
    asyncio.create_task(supabase_ping_loop(app.bot))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
