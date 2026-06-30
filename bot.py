import os
import json
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

SUPABASE_PING_INTERVAL = 6 * 24 * 60 * 60  # 6 days

# Conversation states
(
    ADD_TYPE,           # choose Supabase or Render
    ADD_RENDER_ACTION,  # choose Add new or Edit existing
    ADD_NAME,           # new bot: enter name
    ADD_ID,             # new bot: enter service ID
    ADD_URL,            # new bot: enter URL
    EDIT_PICK_BOT,      # edit: pick which bot
    EDIT_PICK_FIELD,    # edit: pick which field
    EDIT_VALUE,         # edit: enter new value
    SB_ADD_NAME,        # supabase: enter name
    SB_ADD_URL,         # supabase: enter URL
    SB_ADD_KEY,         # supabase: enter key
) = range(11)

REMOVE_CONFIRM = 0


# ---------- Upstash Redis ----------

async def redis(command: list) -> object:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{UPSTASH_URL}/pipeline",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=[command],
            timeout=10,
        )
    return r.json()[0].get("result")


async def hgetall(key: str) -> dict:
    result = await redis(["HGETALL", key])
    if not result:
        return {}
    it = iter(result)
    return dict(zip(it, it))


# ---------- Bot registry
# Each entry: {"service_id": "srv-...", "url": "https://..."}

async def load_bots() -> dict[str, dict]:
    raw = await hgetall(REDIS_BOTS_KEY)
    out = {}
    for alias, val in raw.items():
        try:
            out[alias] = json.loads(val)
        except Exception:
            out[alias] = {"service_id": val, "url": ""}
    return out


async def save_bot(alias: str, service_id: str, url: str) -> None:
    await redis(["HSET", REDIS_BOTS_KEY, alias, json.dumps({"service_id": service_id, "url": url})])


async def delete_bot(alias: str) -> None:
    await redis(["HDEL", REDIS_BOTS_KEY, alias])


# ---------- Supabase registry

async def load_supabase() -> dict:
    raw = await hgetall(REDIS_SUPABASE_KEY)
    return {name: json.loads(val) for name, val in raw.items()}


async def save_supabase(name: str, url: str, key: str) -> None:
    await redis(["HSET", REDIS_SUPABASE_KEY, name, json.dumps({"url": url, "key": key})])


async def delete_supabase(name: str) -> None:
    await redis(["HDEL", REDIS_SUPABASE_KEY, name])


# ---------- Render API

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


# ---------- Auth

def auth(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID


# ---------- /add unified flow

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not auth(update):
        return ConversationHandler.END
    ctx.user_data.clear()
    buttons = [
        [InlineKeyboardButton("🗄️ Supabase", callback_data="addtype:supabase")],
        [InlineKeyboardButton("🤖 Render bot", callback_data="addtype:render")],
        [InlineKeyboardButton("❌ Cancel", callback_data="addtype:cancel")],
    ]
    await update.message.reply_text(
        "What would you like to add?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ADD_TYPE


async def add_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if choice == "supabase":
        await query.edit_message_text("What would you like to call this Supabase project? (alias)")
        return SB_ADD_NAME

    # Render
    buttons = [
        [InlineKeyboardButton("➕ Add new bot", callback_data="renderaction:new")],
        [InlineKeyboardButton("✏️ Edit existing bot", callback_data="renderaction:edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="renderaction:cancel")],
    ]
    await query.edit_message_text(
        "Render bot — what would you like to do?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ADD_RENDER_ACTION


async def add_render_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]

    if choice == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    if choice == "new":
        await query.edit_message_text("What would you like to call this bot? (alias)")
        return ADD_NAME

    # Edit existing
    bots = await load_bots()
    if not bots:
        await query.edit_message_text("No bots registered yet.")
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(alias, callback_data=f"editbot:{alias}")]
        for alias in bots
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="editbot:__cancel__")])
    await query.edit_message_text(
        "Which bot would you like to edit?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_PICK_BOT


# --- New bot steps

async def add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["add_alias"] = update.message.text.strip().lower()
    await update.message.reply_text(
        f"Got it: <b>{ctx.user_data['add_alias']}</b>\n\nNow send the Render service ID (starts with <code>srv-</code>)",
        parse_mode="HTML",
    )
    return ADD_ID


async def add_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["add_service_id"] = update.message.text.strip()
    await update.message.reply_text(
        "Now send the bot's public URL\n(e.g. <code>https://my-bot.onrender.com</code>)",
        parse_mode="HTML",
    )
    return ADD_URL


async def add_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    url = update.message.text.strip().rstrip("/")
    alias = ctx.user_data.pop("add_alias")
    service_id = ctx.user_data.pop("add_service_id")
    await save_bot(alias, service_id, url)
    await update.message.reply_text(
        f"✅ Registered <b>{alias}</b>\n<code>{service_id}</code>\n🔗 {url}",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# --- Edit existing bot steps

async def edit_pick_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    alias = query.data.split(":", 1)[1]

    if alias == "__cancel__":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    ctx.user_data["edit_alias"] = alias
    buttons = [
        [InlineKeyboardButton("Name (alias)", callback_data="editfield:name")],
        [InlineKeyboardButton("Service ID", callback_data="editfield:service_id")],
        [InlineKeyboardButton("URL", callback_data="editfield:url")],
        [InlineKeyboardButton("❌ Cancel", callback_data="editfield:cancel")],
    ]
    await query.edit_message_text(
        f"Editing <b>{alias}</b> — which field?",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )
    return EDIT_PICK_FIELD


async def edit_pick_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = query.data.split(":")[1]

    if field == "cancel":
        await query.edit_message_text("Cancelled.")
        return ConversationHandler.END

    ctx.user_data["edit_field"] = field
    labels = {"name": "new alias", "service_id": "new service ID (srv-...)", "url": "new URL"}
    await query.edit_message_text(f"Send the {labels[field]}:")
    return EDIT_VALUE


async def edit_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    value = update.message.text.strip()
    alias = ctx.user_data.pop("edit_alias")
    field = ctx.user_data.pop("edit_field")

    bots = await load_bots()
    info = bots.get(alias, {"service_id": "", "url": ""})

    if field == "name":
        # Rename: delete old, save under new alias
        await delete_bot(alias)
        await save_bot(value.lower(), info["service_id"], info["url"])
        await update.message.reply_text(
            f"✅ Renamed <b>{alias}</b> → <b>{value.lower()}</b>", parse_mode="HTML"
        )
    elif field == "service_id":
        await save_bot(alias, value, info["url"])
        await update.message.reply_text(
            f"✅ Updated <b>{alias}</b> service ID → <code>{value}</code>", parse_mode="HTML"
        )
    elif field == "url":
        await save_bot(alias, info["service_id"], value.rstrip("/"))
        await update.message.reply_text(
            f"✅ Updated <b>{alias}</b> URL → {value}", parse_mode="HTML"
        )
    return ConversationHandler.END


# --- Supabase steps (inside unified add flow)

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
        "Now send the <b>anon/public API key</b>\n(Supabase → Project Settings → API)",
        parse_mode="HTML",
    )
    return SB_ADD_KEY


async def sb_add_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    name = ctx.user_data.pop("sb_name")
    url = ctx.user_data.pop("sb_url")
    await save_supabase(name, url, key)
    await update.message.reply_text(
        f"✅ Registered Supabase project <b>{name}</b>", parse_mode="HTML"
    )
    return ConversationHandler.END


async def add_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------- Wake / Sleep

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


async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered. Use /add first.")
        return
    if ctx.args:
        alias = ctx.args[0].lower()
        if alias not in bots:
            await update.message.reply_text(f"Unknown alias: {alias}")
            return
        await do_deploy(update.message.reply_text, alias, bots[alias]["service_id"])
        return
    buttons = [
        [InlineKeyboardButton(alias, callback_data=f"deploy:{alias}")]
        for alias in bots
    ]
    await update.message.reply_text(
        "Which bot would you like to deploy?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def do_deploy(reply, alias: str, service_id: str) -> None:
    status, body = await render_post(f"/services/{service_id}/deploys")
    if status in (200, 201):
        deploy_id = (body.get("id") or body.get("deploy", {}).get("id", ""))[:8]
        await reply(f"🚀 Deploy triggered for <b>{alias}</b> (<code>{deploy_id}</code>)", parse_mode="HTML")
    else:
        await reply(f"❌ Failed to deploy {alias}: {body.get('message', body)}")


async def cmd_wake(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    if ctx.args:
        alias = ctx.args[0].lower()
        bots = await load_bots()
        if alias not in bots:
            await update.message.reply_text(f"Unknown alias: {alias}")
            return
        await do_wake(update.message.reply_text, alias, bots[alias]["service_id"])
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
        await do_sleep(update.message.reply_text, alias, bots[alias]["service_id"])
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
    if query.data == "noop":
        await query.answer()
        return
    await query.answer()

    if query.data.startswith("logs:"):
        _, alias = query.data.split(":", 1)
        bots = await load_bots()
        if alias not in bots:
            await query.edit_message_text(f"Unknown alias: {alias}")
            return
        await fetch_deploys(alias, bots[alias]["service_id"], query)
        return

    if query.data.startswith("dl:"):
        short_key = query.data[3:]
        raw = await redis(["HGET", "pingbot:deploys", short_key])
        if not raw:
            await query.edit_message_text("❌ Deploy info expired. Run /logs again.")
            return
        info = json.loads(raw)
        await fetch_deploy_logs(info["service_id"], info["deploy_id"], info["alias"], query)
        return

    action, name = query.data.split(":", 1)

    if action == "pingsb":
        projects = await load_supabase()
        if name not in projects:
            await query.edit_message_text(f"Unknown project: {name}")
            return
        creds = projects[name]
        await query.edit_message_text(f"🏓 Pinging <b>{name}</b>...", parse_mode="HTML")
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{creds['url']}/storage/v1/bucket",
                    headers={"apikey": creds["key"], "Authorization": f"Bearer {creds['key']}"},
                    timeout=15,
                )
            icon = "🟢" if r.status_code < 500 else "🔴"
            await query.edit_message_text(
                f"{icon} <b>{name}</b> — {r.status_code}", parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ <b>{name}</b> — {e}", parse_mode="HTML")
        return

    bots = await load_bots()
    if name not in bots:
        await query.edit_message_text(f"Unknown alias: {name}")
        return
    reply = query.edit_message_text
    if action == "wake":
        await do_wake(reply, name, bots[name]["service_id"])
    elif action == "sleep":
        await do_sleep(reply, name, bots[name]["service_id"])
    elif action == "deploy":
        await do_deploy(reply, name, bots[name]["service_id"])
    elif action == "ping":
        url = bots[name].get("url", "")
        if not url:
            await reply(f"⚠️ No URL stored for {name}. Edit it with /add → Render bot → Edit existing.")
            return
        await query.edit_message_text(f"🏓 Pinging <b>{name}</b>...", parse_mode="HTML")
        asyncio.create_task(ping_until_ready(query.message, name, url))


# ---------- Ping until ready

async def ping_until_ready(message, alias: str, url: str, poll_interval: int = 30, timeout: int = 300) -> None:
    elapsed = 0
    while elapsed < timeout:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, timeout=10)
            if r.status_code < 500:
                await message.edit_text(f"✅ <b>{alias}</b> is ready to use!", parse_mode="HTML")
                return
        except Exception:
            pass
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        await message.edit_text(
            f"🏓 Pinging <b>{alias}</b>... ({elapsed}s elapsed)", parse_mode="HTML"
        )
    await message.edit_text(f"⏱️ <b>{alias}</b> did not respond after {timeout}s.", parse_mode="HTML")


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    projects = await load_supabase()
    if not bots and not projects:
        await update.message.reply_text("Nothing registered. Use /add first.")
        return
    buttons = []
    if bots:
        buttons.append([InlineKeyboardButton("── 🤖 Render bots ──", callback_data="noop")])
        for alias in bots:
            buttons.append([InlineKeyboardButton(alias, callback_data=f"ping:{alias}")])
    if projects:
        buttons.append([InlineKeyboardButton("── 🗄️ Supabase ──", callback_data="noop")])
        for name in projects:
            buttons.append([InlineKeyboardButton(name, callback_data=f"pingsb:{name}")])
    await update.message.reply_text(
        "What would you like to ping?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------- Status / List

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered.")
        return

    lines = []
    for alias, info in bots.items():
        icon, state_str = await check_bot_state(alias, info)
        lines.append(f"{icon} <b>{alias}</b> — {state_str}\n<code>{info['service_id']}</code>")

    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    projects = await load_supabase()
    lines = []

    if bots:
        lines.append("🤖 <b>Render bots</b>")
        for alias, info in bots.items():
            url_line = f"\n🔗 {info['url']}" if info.get("url") else ""
            lines.append(f"• <b>{alias}</b>\n<code>{info['service_id']}</code>{url_line}")

    if projects:
        if lines:
            lines.append("")
        lines.append("🗄️ <b>Supabase projects</b>")
        for name, creds in projects.items():
            lines.append(f"• <b>{name}</b>\n🔗 {creds['url']}")

    if not lines:
        await update.message.reply_text("Nothing registered yet. Use /add to get started.")
        return

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------- Remove (unified)

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not auth(update):
        return ConversationHandler.END
    bots = await load_bots()
    projects = await load_supabase()
    if not bots and not projects:
        await update.message.reply_text("Nothing registered.")
        return ConversationHandler.END
    buttons = []
    if bots:
        buttons.append([InlineKeyboardButton("── 🤖 Render bots ──", callback_data="remove:__noop__")])
        for alias in bots:
            buttons.append([InlineKeyboardButton(alias, callback_data=f"remove:{alias}")])
    if projects:
        buttons.append([InlineKeyboardButton("── 🗄️ Supabase ──", callback_data="remove:__noop__")])
        for name in projects:
            buttons.append([InlineKeyboardButton(name, callback_data=f"removesb:{name}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="remove:__cancel__")])
    await update.message.reply_text(
        "What would you like to remove?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return REMOVE_CONFIRM


async def remove_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, name = query.data.split(":", 1)
    if name in ("__cancel__", "__noop__"):
        if name == "__cancel__":
            await query.edit_message_text("Cancelled.")
        return ConversationHandler.END if name == "__cancel__" else REMOVE_CONFIRM
    if action == "removesb":
        await delete_supabase(name)
        await query.edit_message_text(f"🗑️ Removed Supabase project <b>{name}</b>", parse_mode="HTML")
    else:
        await delete_bot(name)
        await query.edit_message_text(f"🗑️ Removed <b>{name}</b>", parse_mode="HTML")
    return ConversationHandler.END


# ---------- Supabase ping

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


# ---------- Bot state check

async def check_bot_state(alias: str, info: dict) -> tuple[str, str]:
    """Returns (icon, state_str). Checks Render API + URL ping to detect wound-down."""
    service_id = info.get("service_id", "")
    url = info.get("url", "")

    status, body = await render_get(f"/services/{service_id}")
    if status != 200:
        return "❌", "error"

    svc = body.get("service", body)
    suspended = svc.get("suspended", "")
    if suspended == "suspended":
        return "⏸️", "suspended"

    # Not suspended — check if actually responding (vs wound down)
    if url:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(url, timeout=5, follow_redirects=True)
            if r.status_code < 500:
                return "🟢", "running"
        except Exception:
            pass
        return "😴", "wound down"

    return "🟢", "running (no URL to verify)"






# ---------- Deploys

DEPLOY_STATUS_ICON = {
    "live": "✅",
    "build_failed": "❌",
    "update_failed": "❌",
    "canceled": "🚫",
    "deactivated": "⏸️",
    "pre_deploy_failed": "❌",
}

async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    bots = await load_bots()
    if not bots:
        await update.message.reply_text("No bots registered. Use /add first.")
        return
    buttons = [
        [InlineKeyboardButton(alias, callback_data=f"logs:{alias}")]
        for alias in bots
    ]
    await update.message.reply_text(
        "Which bot's deploys would you like to see?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def fetch_deploys(alias: str, service_id: str, query) -> None:
    status, body = await render_get(f"/services/{service_id}/deploys?limit=5")
    if status != 200:
        await query.edit_message_text(f"❌ Could not fetch deploys for {alias}.")
        return

    deploys = body if isinstance(body, list) else body.get("deploys", [])
    if not deploys:
        await query.edit_message_text(f"No deploys found for <b>{alias}</b>.", parse_mode="HTML")
        return

    buttons = []
    lines = [f"🚀 <b>{alias}</b> — recent deploys\n"]
    for entry in deploys:
        d = entry.get("deploy", entry)
        deploy_id = d.get("id", "")
        deploy_status = d.get("status", "unknown")
        created = d.get("createdAt", "")[:16].replace("T", " ")
        icon = DEPLOY_STATUS_ICON.get(deploy_status, "🔄")
        lines.append(f"{icon} <code>{deploy_id[:8]}</code> {created} — {deploy_status}")
        # Store deploy metadata in Redis with a short key to stay under Telegram's 64-byte callback limit
        short_key = deploy_id[:12]
        await redis(["HSET", "pingbot:deploys", short_key, json.dumps({
            "service_id": service_id, "deploy_id": deploy_id, "alias": alias
        })])
        buttons.append([InlineKeyboardButton(
            f"{icon} {created} {deploy_status}",
            callback_data=f"dl:{short_key}"
        )])

    await query.edit_message_text(
        "\n".join(lines) + "\n\nTap a deploy to view its logs.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def get_owner_id() -> str | None:
    status, body = await render_get("/owners?limit=1")
    if status == 200 and body:
        owners = body if isinstance(body, list) else body.get("owners", [])
        if owners:
            return (owners[0].get("owner") or owners[0]).get("id")
    return None


async def fetch_deploy_logs(service_id: str, deploy_id: str, alias: str, query) -> None:
    await query.edit_message_text(f"⏳ Fetching logs for deploy <code>{deploy_id[:8]}</code>...", parse_mode="HTML")

    # Get deploy time window to scope the log query
    status, resp = await render_get(f"/services/{service_id}/deploys/{deploy_id}")
    deploy = (resp.get("deploy", resp) if status == 200 else {})
    created_at = deploy.get("createdAt", "")
    finished_at = deploy.get("finishedAt", "")

    owner_id = await get_owner_id()
    if not owner_id:
        await query.edit_message_text("❌ Could not determine workspace owner ID.")
        return

    params = f"ownerId={owner_id}&resource={service_id}&limit=100&direction=backward"
    if created_at:
        params += f"&startTime={created_at}"
    if finished_at:
        params += f"&endTime={finished_at}"

    status, body = await render_get(f"/logs?{params}")
    if status != 200:
        await query.edit_message_text(f"❌ Could not fetch logs (HTTP {status}).\n<code>{str(body)[:300]}</code>", parse_mode="HTML")
        return

    entries = body if isinstance(body, list) else body.get("logs", [])
    if not entries:
        await query.edit_message_text(
            f"No logs found for deploy <code>{deploy_id[:8]}</code>.\n\n"
            f"Status: {deploy.get('status', 'unknown')}\n"
            f"Created: {created_at[:16].replace('T', ' ')}\n"
            f"Finished: {finished_at[:16].replace('T', ' ')}",
            parse_mode="HTML",
        )
        return

    log_lines = [e.get("message", "") if isinstance(e, dict) else str(e) for e in entries]
    log_text = "\n".join(log_lines)
    header = f"📋 <b>{alias}</b> deploy <code>{deploy_id[:8]}</code>\n\n"
    max_log = 4000 - len(header)
    if len(log_text) > max_log:
        log_text = "…(truncated)\n" + log_text[-max_log:]

    await query.edit_message_text(header + f"<pre>{log_text}</pre>", parse_mode="HTML")


# ---------- Help

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not auth(update):
        return
    lines = [
        "/deploy — manually trigger a deploy",
        "/logs — view recent deploys and logs",
        "/ping — ping a Render bot or Supabase project",
        "/wake — resume a suspended Render service",
        "/sleep — suspend a running Render service",
        "/status — show all Render bots and their state",
        "/add — add or edit a bot / Supabase project",
        "/remove — remove a bot or Supabase project",
        "/list — list everything registered",
        "/help — this message",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------- Health check

async def health(request):
    return web.Response(text="ok")


# ---------- Entry point

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
            ADD_TYPE: [CallbackQueryHandler(add_type, pattern="^addtype:")],
            ADD_RENDER_ACTION: [CallbackQueryHandler(add_render_action, pattern="^renderaction:")],
            ADD_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_ID:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_id)],
            ADD_URL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_url)],
            EDIT_PICK_BOT:   [CallbackQueryHandler(edit_pick_bot, pattern="^editbot:")],
            EDIT_PICK_FIELD: [CallbackQueryHandler(edit_pick_field, pattern="^editfield:")],
            EDIT_VALUE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
            SB_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_add_name)],
            SB_ADD_URL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_add_url)],
            SB_ADD_KEY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_add_key)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel), CommandHandler("abort", add_cancel)],
        conversation_timeout=180,
    )

    remove_conv = ConversationHandler(
        entry_points=[CommandHandler("remove", cmd_remove)],
        states={
            REMOVE_CONFIRM: [CallbackQueryHandler(remove_confirm, pattern="^remove:|^removesb:")],
        },
        fallbacks=[CommandHandler("cancel", add_cancel), CommandHandler("abort", add_cancel)],
        conversation_timeout=120,
    )

    app.add_handler(add_conv)
    app.add_handler(remove_conv)
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("deploy", cmd_deploy))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("wake", cmd_wake))
    app.add_handler(CommandHandler("sleep", cmd_sleep))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))

    await app.initialize()
    await app.bot.set_my_commands([
        BotCommand("logs", "View recent deploys and logs"),
        BotCommand("deploy", "Manually trigger a deploy"),
        BotCommand("ping", "Ping a Render bot or Supabase project"),
        BotCommand("wake", "Resume a suspended Render service"),
        BotCommand("sleep", "Suspend a running Render service"),
        BotCommand("status", "Show all Render bots and their state"),
        BotCommand("add", "Add or edit a bot / Supabase project"),
        BotCommand("remove", "Remove a bot or Supabase project"),
        BotCommand("list", "List everything registered"),
        BotCommand("help", "Show command reference"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Controller bot started.")
    asyncio.create_task(supabase_ping_loop(app.bot))
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
