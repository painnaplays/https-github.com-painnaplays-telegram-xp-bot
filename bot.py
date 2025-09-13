# pip install python-telegram-bot==21.* aiosqlite python-dotenv
import asyncio, os, aiosqlite
from dotenv import load_dotenv
from typing import Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageReactionHandler, ContextTypes
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB = os.getenv("DB_PATH", "engage.db")
TH = ZoneInfo("Asia/Bangkok")

RULES = {"reaction": 10}  # กดครั้งแรกบนโพสต์: +10

# ---------------- DB ----------------
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, xp INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, type TEXT, delta INTEGER,
            chat_id INTEGER, message_id INTEGER, meta TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, type, chat_id, message_id, meta))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reacted_once(
            user_id INTEGER, chat_id INTEGER, message_id INTEGER,
            PRIMARY KEY(user_id, chat_id, message_id))""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")
        await db.commit()

async def upsert_user(user_id: int, username: str | None):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id, username, xp) VALUES(?,?,0)",
                         (user_id, (username or "")))
        await db.commit()

async def add_event_and_xp(user_id: int, delta: int, typ: str,
                           chat_id: int, message_id: int, meta: str = "") -> bool:
    async with aiosqlite.connect(DB) as db:
        try:
            await db.execute("""INSERT INTO events(user_id,type,delta,chat_id,message_id,meta)
                                VALUES(?,?,?,?,?,?)""",
                             (user_id, typ, delta, chat_id, message_id, meta))
            await db.execute("UPDATE users SET xp = xp + ? WHERE user_id = ?",
                             (delta, user_id))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # เคยนับไปแล้ว

# ---------------- Helpers ----------------
def pack_reactions(items) -> Tuple[str, ...]:
    if not items:
        return tuple()
    out = []
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
    now_th = datetime.now(TH)
    week_start_th = (now_th - timedelta(days=now_th.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = week_start_th.astimezone(timezone.utc)
    end_utc = now_th.astimezone(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start_utc.strftime(fmt), end_utc.strftime(fmt), week_start_th.strftime("%d %b %Y")

# ---------------- Commands ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    await update.message.reply_text(
        "พร้อมนับ XP ✅\n"
        f"• กดรีแอคชัน 'ครั้งแรกต่อโพสต์ต่อคน' = +{RULES['reaction']} XP\n\n"
        "คำสั่ง: /my /top /week /rules"
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"กติกา:\n• Reaction ครั้งแรกบนโพสต์: +{RULES['reaction']} XP\n"
        "• ไม่หักแต้มเมื่อเปลี่ยน/ลบรีแอคชัน\n"
        "• ใช้ได้ใน Channel/Group ที่รีแอคชันไม่เป็น anonymous (บอทต้องเป็นแอดมิน)"
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
        cur = await db.execute("""
            SELECT u.username, u.user_id, SUM(e.delta) AS xp
            FROM events e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.created_at BETWEEN ? AND ?
            GROUP BY u.user_id
            HAVING xp <> 0
            ORDER BY xp DESC
            LIMIT 15
        """, (start_utc, end_utc))
        rows = await cur.fetchall()
        cur2 = await db.execute("""
            SELECT u.username, u.user_id, e.type, SUM(e.delta) AS xp
            FROM events e
            JOIN users u ON u.user_id = e.user_id
            WHERE e.created_at BETWEEN ? AND ?
            GROUP BY u.user_id, e.type
            HAVING xp <> 0
            ORDER BY u.user_id, e.type
        """, (start_utc, end_utc))
        detail_rows = await cur2.fetchall()
    if not rows:
        await update.message.reply_text(f"สรุปสัปดาห์นี้ (ตั้งแต่ {start_th_label}): ยังไม่มีคะแนนเลย 🥲")
        return
    by_user = {}
    for uname, uid, typ, xp in detail_rows:
        by_user.setdefault(uid, []).append((typ, xp))
    lines = [f"📅 สรุปสัปดาห์นี้ (ตั้งแต่ {start_th_label})"]
    medals = ["🥇","🥈","🥉"]
    for i, (uname, uid, xp) in enumerate(rows, start=1):
        tag = f"@{uname}" if uname else f"ID:{uid}"
        badge = medals[i-1] if i <= 3 else f"{i}."
        lines.append(f"{badge} {tag} — {int(xp)} XP")
        parts = [f"{typ}:{int(txp)}" for typ, txp in by_user.get(uid, [])]
        if parts:
            lines.append("   · " + " | ".join(parts))
    await update.message.reply_text("\n".join(lines))

# ---------------- Reaction Handler ----------------
def pack(items):  # alias สั้น ๆ
    return pack_reactions(items)

async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    mr = update.message_reaction
    if not mr or not mr.user:
        return
    # NOTE: ถ้าเป็น anonymous reactions จะไม่มี user -> โค้ดนี้จะไม่บวก XP
    user = mr.user
    chat_id, msg_id = mr.chat.id, mr.message_id
    old_set = pack(mr.old_reaction)
    new_set = pack(mr.new_reaction)

    # เรานับเฉพาะ "ครั้งแรกบนโพสต์": old ว่าง -> new มี และยังไม่เคยนับโพสต์นี้มาก่อน
    if (not old_set and new_set):
        await upsert_user(user.id, user.username)
        async with aiosqlite.connect(DB) as db:
            cur = await db.execute(
                "SELECT 1 FROM reacted_once WHERE user_id=? AND chat_id=? AND message_id=?",
                (user.id, chat_id, msg_id)
            )
            already = await cur.fetchone()
        if not already:
            if await add_event_and_xp(user.id, RULES["reaction"], "reaction", chat_id, msg_id, meta=",".join(new_set)):
                async with aiosqlite.connect(DB) as db:
                    await db.execute(
                        "INSERT OR IGNORE INTO reacted_once(user_id,chat_id,message_id) VALUES(?,?,?)",
                        (user.id, chat_id, msg_id)
                    )
                    await db.commit()

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN in env")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("week", cmd_week))

    app.add_handler(MessageReactionHandler(on_reaction))

    # รับอัปเดตที่เกี่ยวข้อง (reaction)
    app.run_polling(allowed_updates=["message_reaction"])

if __name__ == "__main__":
    asyncio.run(init_db())
    main()
