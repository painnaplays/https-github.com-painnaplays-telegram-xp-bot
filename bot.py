# bot.py
# pip install python-telegram-bot==21.* aiosqlite python-dotenv
import os
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Tuple

import aiosqlite
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageReactionHandler,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB = os.getenv("DB_PATH", "engage.db")

# Optional: owner for /shutdown (numeric Telegram user ID)
OWNER_ID_ENV = os.getenv("OWNER_ID")
OWNER_ID = int(OWNER_ID_ENV) if OWNER_ID_ENV and OWNER_ID_ENV.isdigit() else None

TH = ZoneInfo("Asia/Bangkok")
RULES = {"reaction": 10, "reaction_remove": -10}

# ---------------------- DB ----------------------
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users(
                   user_id INTEGER PRIMARY KEY,
                   username TEXT,
                   xp INTEGER DEFAULT 0
               )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS events(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id INTEGER,
                   type TEXT,
                   delta INTEGER,
                   chat_id INTEGER,
                   message_id INTEGER,
                   meta TEXT,
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                   UNIQUE(user_id, type, chat_id, message_id, meta)
               )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS reacted_once(
                   user_id INTEGER,
                   chat_id INTEGER,
                   message_id INTEGER,
                   PRIMARY KEY(user_id, chat_id, message_id)
               )"""
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")
        await db.commit()

async def upsert_user(user_id: int, username: str | None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, xp) VALUES(?,?,0)",
            (user_id, username or ""),
        )
        await db.commit()

async def add_event_and_xp(
    user_id: int, delta: int, typ: str, chat_id: int, message_id: int, meta: str = ""
) -> bool:
    """Returns True if this event was newly counted (not a duplicate by UNIQUE)."""
    async with aiosqlite.connect(DB) as db:
        try:
            await db.execute(
                """INSERT INTO events(user_id,type,delta,chat_id,message_id,meta)
                   VALUES(?,?,?,?,?,?)""",
                (user_id, typ, delta, chat_id, message_id, meta),
            )
            await db.execute("UPDATE users SET xp = xp + ? WHERE user_id = ?", (delta, user_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

# ---------------------- Helpers ----------------------
def pack_reactions(items) -> Tuple[str, ...]:
    """Normalize reaction list (PTB objects or dicts) into a sorted tuple for compare/log."""
    if not items:
        return tuple()
    out: list[str] = []
    for it in items:
        t = getattr(it, "type", None) or (it.get("type") if isinstance(it, dict) else None)
        if getattr(t, "type", None) == "custom_emoji":
            out.append(f"custom:{t.custom_emoji_id}")
        elif getattr(t, "type", None) == "emoji":
            out.append(f"emoji:{t.emoji}")
        elif isinstance(t, dict) and t.get("type") == "custom_emoji":
            out.append(f"custom:{t.get('custom_emoji_id')}")
        elif isinstance(t, dict) and t.get("type") == "emoji":
            out.append(f"emoji:{t.get('emoji')}")
        else:
            out.append(str(t))
    return tuple(sorted(out))

def week_range_utc_now():
    """Return (start_utc_str, end_utc_str, start_label_th) for Mon 00:00 Asia/Bangkok → now."""
    now_th = datetime.now(TH)
    week_start_th = (now_th - timedelta(days=now_th.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = week_start_th.astimezone(timezone.utc)
    end_utc = now_th.astimezone(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_utc.strftime(fmt), end_utc.strftime(fmt), week_start_th.strftime("%d %b %Y")

# ---------------------- Commands ----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    owner_hint = "\n(Owner: /shutdown เพื่อปิดบอท)" if OWNER_ID else ""
    await update.message.reply_text(
        "พร้อมนับ XP ✅\n"
        f"• กดรีแอคชันครั้งแรกบนโพสต์: +{RULES['reaction']} XP\n"
        f"• ถอนรีแอคชันจนไม่เหลือ: {RULES['reaction_remove']} XP\n\n"
        "คำสั่ง: /rules /my /top /week" + owner_hint
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "กติกา:\n"
        f"• ครั้งแรกที่กดรีแอคชันในโพสต์: +{RULES['reaction']} XP\n"
        f"• ถอนรีแอคชันสุดท้าย: {RULES['reaction_remove']} XP\n"
        "• เปลี่ยนรีแอคชัน (👍→❤️) ไม่ได้แต้มเพิ่ม/ลด\n"
        "• ใช้ได้เฉพาะที่ reactions ไม่ anonymous และบอทเป็นแอดมิน"
    )

async def cmd_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    uid = update.effective_user.id
    await upsert_user(uid, update.effective_user.username)
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT xp FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    await update.message.reply_text(f"XP ของคุณ: {row[0] if row else 0}")

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT username, user_id, xp FROM users ORDER BY xp DESC LIMIT 15")
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("ยังไม่มีคะแนนเลย 🥲")
        return
    lines = []
    for i, (uname, uid, xp) in enumerate(rows, start=1):
        tag = f"@{uname}" if uname else f"ID:{uid}"
        lines.append(f"{i}. {tag} — {xp} XP")
    await update.message.reply_text("🏆 Top 15\n" + "\n".join(lines))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    start_utc, end_utc, start_th_label = week_range_utc_now()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """
            SELECT u.username, u.user_id, SUM(e.delta) AS xp
            FROM events e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.created_at BETWEEN ? AND ?
            GROUP BY u.user_id
            HAVING xp <> 0
            ORDER BY xp DESC
            LIMIT 15
            """,
            (start_utc, end_utc),
        )
        rows = await cur.fetchall()

        cur2 = await db.execute(
            """
            SELECT u.user_id, e.type, SUM(e.delta) AS xp
            FROM events e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.created_at BETWEEN ? AND ?
            GROUP BY u.user_id, e.type
            HAVING xp <> 0
            ORDER BY u.user_id, e.type
            """,
            (start_utc, end_utc),
        )
        detail_rows = await cur2.fetchall()

    if not rows:
        await update.message.reply_text(f"สรุปสัปดาห์นี้ (ตั้งแต่ {start_th_label}): ยังไม่มีคะแนนเลย 🥲")
        return

    by_user: dict[int, list[tuple[str, int]]] = {}
    for uid, typ, xp in detail_rows:
        by_user.setdefault(uid, []).append((typ, int(xp)))

    lines = [f"📅 สรุปสัปดาห์นี้ (ตั้งแต่ {start_th_label})"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uname, uid, xp) in enumerate(rows, start=1):
        tag = f"@{uname}" if uname else f"ID:{uid}"
        badge = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{badge} {tag} — {int(xp)} XP")
        parts = []
        for typ, t_xp in by_user.get(uid, []):
            label = {"reaction": "react", "reaction_remove": "unreact"}.get(typ, typ)
            parts.append(f"{label}:{t_xp}")
        if parts:
            lines.append("   · " + " | ".join(parts))
    await update.message.reply_text("\n".join(lines))

async def cmd_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID is None or update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ คุณไม่มีสิทธิ์ปิดบอทนี้")
        return
    await update.message.reply_text("Shutting down… 👋")
    sys.exit(0)

# ---------------------- Reaction Handler ----------------------
async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    +10 XP: first time a user reacts on a specific post
    -10 XP: user removes their last reaction on that post
    0 XP : reaction change (emoji→emoji)
    Requires: non-anonymous reactions & bot is admin.
    """
    await init_db()
    mr = update.message_reaction
    if not mr or not mr.user:
        return  # anonymous or no user info

    user = mr.user
    chat_id, msg_id = mr.chat.id, mr.message_id
    old_set = pack_reactions(mr.old_reaction)
    new_set = pack_reactions(mr.new_reaction)

    await upsert_user(user.id, user.username)

    # First reaction on this post by this user → +10 XP
    if not old_set and new_set:
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT 1 FROM reacted_once WHERE user_id=? AND chat_id=? AND message_id=?",
                (user.id, chat_id, msg_id),
            )
            already = await cur.fetchone()
        if not already:
            if await add_event_and_xp(
                user.id, RULES["reaction"], "reaction", chat_id, msg_id, meta=",".join(new_set)
            ):
                async with aiosqlite.connect(DB) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO reacted_once(user_id,chat_id,message_id) VALUES(?,?,?)",
                        (user.id, chat_id, msg_id),
                    )
                    await db.commit()
        return

    # Reaction changed → no XP change
    if old_set and new_set and old_set != new_set:
        return

    # Removed last reaction → -10 XP
    if old_set and not new_set:
        await add_event_and_xp(
            user.id, RULES["reaction_remove"], "reaction_remove", chat_id, msg_id, meta=",".join(old_set)
        )

# ---------------------- Async startup (Python 3.13 friendly) ----------------------
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN in environment")
    await init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("shutdown", cmd_shutdown))
    # Events
    app.add_handler(MessageReactionHandler(on_reaction))

    # Explicit async lifecycle (works on Python 3.13 / Render)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message_reaction"])
    await app.updater.idle()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
