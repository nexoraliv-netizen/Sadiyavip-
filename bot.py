import asyncio
import logging
import os
import re
import sqlite3
import time

from aiohttp import web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GROUP_ID = int(os.environ["GROUP_ID"])  # private group chat id, e.g. -1001234567890
AFFILIATE_LINK = os.environ["AFFILIATE_LINK"]  # may contain {tg_id}
POSTBACK_SECRET = os.environ["POSTBACK_SECRET"]
PORT = int(os.getenv("PORT", "8080"))
UID_PARAM = os.getenv("UID_PARAM", "uid")  # postback param holding trader UID
CLICK_PARAM = os.getenv("CLICK_PARAM", "click_id")  # param holding our tg_id
INVITE_TTL_HOURS = int(os.getenv("INVITE_TTL_HOURS", "24"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

UID_RE = re.compile(r"^[A-Za-z0-9]{3,20}$")

# ---------- DB ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS postbacks(
        uid TEXT PRIMARY KEY, click_id TEXT, event TEXT, raw TEXT,
        ts INTEGER, claimed_by INTEGER)"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS users(
        tg_id INTEGER PRIMARY KEY, uid TEXT, ts INTEGER)"""
)
db.commit()


def affiliate_url(tg_id: int) -> str:
    return AFFILIATE_LINK.replace("{tg_id}", str(tg_id))


async def make_invite(bot, tg_id: int) -> str:
    link = await bot.create_chat_invite_link(
        chat_id=GROUP_ID,
        member_limit=1,
        expire_date=int(time.time()) + INVITE_TTL_HOURS * 3600,
        name=f"user {tg_id}",
    )
    return link.invite_link


async def grant_access(bot, tg_id: int, uid: str):
    invite = await make_invite(bot, tg_id)
    db.execute(
        "INSERT OR REPLACE INTO users(tg_id, uid, ts) VALUES(?,?,?)",
        (tg_id, uid, int(time.time())),
    )
    db.commit()
    await bot.send_message(
        tg_id,
        f"✅ ভেরিফিকেশন সম্পন্ন!\n\nপ্রাইভেট গ্রুপে জয়েন করুন:\n{invite}\n\n"
        f"⚠️ লিঙ্কটি শুধু একবার ব্যবহার করা যাবে এবং {INVITE_TTL_HOURS} ঘণ্টা পর মেয়াদ শেষ হবে।",
    )


# ---------- Bot handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid_tg = update.effective_user.id
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 অ্যাকাউন্ট খুলুন", url=affiliate_url(uid_tg))],
            [InlineKeyboardButton("✅ আমি অ্যাকাউন্ট খুলেছি", callback_data="registered")],
        ]
    )
    await update.message.reply_text(
        "স্বাগতম! 👋\n\nপ্রাইভেট গ্রুপে জয়েন করতে নিচের লিঙ্ক দিয়ে অ্যাকাউন্ট খুলুন, "
        "তারপর \"আমি অ্যাকাউন্ট খুলেছি\" বাটনে চাপুন।",
        reply_markup=kb,
    )


async def on_registered(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["awaiting_uid"] = True
    await q.message.reply_text("আপনার Quotex UID পাঠান (শুধু নম্বর/অক্ষর):")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_uid"):
        return
    tg_id = update.effective_user.id
    uid = update.message.text.strip()
    if not UID_RE.match(uid):
        await update.message.reply_text("❌ UID ফরম্যাট ঠিক নেই। আবার পাঠান।")
        return

    row = db.execute(
        "SELECT claimed_by FROM postbacks WHERE uid=?", (uid,)
    ).fetchone()
    if not row:
        await update.message.reply_text(
            "⏳ এই UID আমাদের লিস্টে পাওয়া যায়নি।\n\n"
            "• নিশ্চিত করুন আপনি আমার লিঙ্ক দিয়েই অ্যাকাউন্ট খুলেছেন\n"
            "• রেজিস্ট্রেশনের পর কয়েক মিনিট সময় লাগতে পারে — একটু পরে আবার চেষ্টা করুন"
        )
        return
    if row[0] not in (None, tg_id):
        await update.message.reply_text("❌ এই UID অন্য একজন ইতোমধ্যে ব্যবহার করেছেন।")
        return

    db.execute("UPDATE postbacks SET claimed_by=? WHERE uid=?", (tg_id, uid))
    db.commit()
    ctx.user_data["awaiting_uid"] = False
    await grant_access(ctx.bot, tg_id, uid)


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total = db.execute("SELECT COUNT(*) FROM postbacks").fetchone()[0]
    claimed = db.execute(
        "SELECT COUNT(*) FROM postbacks WHERE claimed_by IS NOT NULL"
    ).fetchone()[0]
    await update.message.reply_text(
        f"📊 মোট রেজিস্ট্রেশন (postback): {total}\n✅ গ্রুপে অ্যাক্সেস পেয়েছে: {claimed}"
    )


async def add_uid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin fallback: /add_uid 12345678 — manually whitelist a UID."""
    if update.effective_user.id != ADMIN_ID or not ctx.args:
        return
    uid = ctx.args[0]
    db.execute(
        "INSERT OR IGNORE INTO postbacks(uid, event, raw, ts) VALUES(?,?,?,?)",
        (uid, "manual", "", int(time.time())),
    )
    db.commit()
    await update.message.reply_text(f"✅ UID {uid} যোগ হয়েছে।")


# ---------- Postback web server ----------
async def postback(request: web.Request):
    if request.match_info["secret"] != POSTBACK_SECRET:
        return web.Response(status=403, text="forbidden")

    params = dict(request.query)
    if request.method == "POST":
        params.update(await request.post())
    log.info("postback: %s", params)

    uid = (params.get(UID_PARAM) or "").strip()
    if not uid:
        return web.Response(status=400, text="missing uid")
    click_id = (params.get(CLICK_PARAM) or "").strip()
    event = params.get("event") or params.get("status") or "registration"

    db.execute(
        "INSERT OR IGNORE INTO postbacks(uid, click_id, event, raw, ts) VALUES(?,?,?,?,?)",
        (uid, click_id, event, str(params), int(time.time())),
    )
    db.commit()

    # Fully automatic path: click_id carries the Telegram user id
    if click_id.isdigit():
        tg_id = int(click_id)
        res = db.execute(
            "UPDATE postbacks SET claimed_by=? WHERE uid=? AND claimed_by IS NULL",
            (tg_id, uid),
        )
        db.commit()
        if res.rowcount:
            try:
                await request.app["tg"].bot.send_message(
                    tg_id, "🎉 আপনার রেজিস্ট্রেশন কনফার্ম হয়েছে!"
                )
                await grant_access(request.app["tg"].bot, tg_id, uid)
            except Exception:
                log.exception("auto-grant failed for %s", tg_id)

    return web.Response(text="OK")


async def main():
    tg = Application.builder().token(BOT_TOKEN).build()
    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(CommandHandler("stats", stats))
    tg.add_handler(CommandHandler("add_uid", add_uid))
    tg.add_handler(CallbackQueryHandler(on_registered, pattern="^registered$"))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    web_app = web.Application()
    web_app["tg"] = tg
    web_app.add_routes(
        [
            web.get("/postback/{secret}", postback),
            web.post("/postback/{secret}", postback),
            web.get("/health", lambda r: web.Response(text="ok")),
        ]
    )
    runner = web.AppRunner(web_app)

    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling()
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Bot running. Postback: /postback/<secret> on port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await tg.updater.stop()
        await tg.stop()
        await tg.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
