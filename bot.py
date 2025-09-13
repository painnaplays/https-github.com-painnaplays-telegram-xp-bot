# pip install python-telegram-bot==21.* aiosqlite python-dotenv
import asyncio, json, os, aiosqlite
from dotenv import load_dotenv
from typing import List, Tuple
from telegram import Update, Poll, ChatAdministratorRights
from telegram.ext import (
    Application, CommandHandler, MessageReactionHandler,
    PollAnswerHandler, ContextTypes
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB = os.getenv("DB_PATH", "engage.db")

# กติกา XP
RULES = {"reaction": 10, "reaction_remove": -10, "poll_answer": 20}

# ---------- DB ----------
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
        await db.execute("""CREATE TABLE IF NOT EXISTS polled_once(
            user_id INTEGER, poll_id TEXT, PRIMARY KEY(user_id, poll_id))""")
        await db.commit()

async def upsert_user(user_id: int, username: str | None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, xp) VALUES(?,?,0)",
            (user_id, username or "")
        )
        await db.commit()

async def add_event_and_xp(user_id: int, delta: int, typ: str,
                           chat_id: int, message_id: int, meta: str) -> bool:
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

# ---------- Helpers ----------
def pack_reactions(items) -> Tuple[str, ...]:
    """แปลงรายการ reaction เป็น tuple อ่านง่าย ใช้ทำ meta/compare"""
    if not items:
        return tuple()
    out = []
    for it in items:
        t = getattr(it, "type", None) or it.get("type")
        if getattr(t, "type", None) == "custom_emoji":
            out.append(f"custom:{t.custom_emoji_id}")
        elif getattr(t, "type", None) == "emoji":
            out.append(f"emoji:{t.emoji}")
        else:
            # fallback
            try:
                # dict-like
                if t.get("type") == "custom_emoji":
                    out.append(f"custom:{t.get('custom_emoji_id')}")
                else:
                    out.append(f"emoji:{t.get('emoji')}")
            except Exception:
                out.append(str(t))
    return tuple(sorted(out))

async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    # ใช้ตรวจแอดมินเฉพาะกรณีต้องการ; ปกติ Telegram จะส่งอัปเดตให้เฉพาะบอทแอดมินอยู่แล้ว
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    await update.message.reply_text(
        "พร้อมนับ XP 👀\n"
        f"- Reaction +{RULES['reaction']} / ถอน {RULES['reaction_remove']}\n"
        f"- โหวตโพล +{RULES['poll_answer']}\n"
        "ใช้ /my ดูแต้มตัวเอง, /top ดูอันดับ"
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
    limit = 10
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT username, user_id, xp FROM users ORDER BY xp DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("ยังไม่มีใครได้ XP เลย 🥲")
        return
    lines = []
    for i, (uname, uid, xp) in enumerate(rows, start=1):
        tag = f"@{uname}" if uname else f"ID:{uid}"
        lines.append(f"{i}. {tag} — {xp} XP")
    await update.message.reply_text("🏆 Top " + str(limit) + "\n" + "\n".join(lines))

async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    mr = update.message_reaction  # MessageReactionUpdated
    if not mr or not mr.user:
        return

    user = mr.user
    chat_id = mr.chat.id
    msg_id = mr.message_id

    # สร้าง meta สำหรับอีเวนต์นี้ (ให้ dedupe ได้)
    old_set = pack_reactions(mr.old_reaction)
    new_set = pack_reactions(mr.new_reaction)

    await upsert_user(user.id, user.username)

    # เคสนับ +XP: มีรีแอคชันใหม่ (จากเดิมว่างหรือเปลี่ยน แต่เรานับ "ครั้งแรกบนข้อความนี้")
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""SELECT 1 FROM reacted_once WHERE user_id=? AND chat_id=? AND message_id=?""",
                               (user.id, chat_id, msg_id))
        already = await cur.fetchone()

    if (not old_set and new_set) and not already:
        # ครั้งแรกที่คนนี้รีแอคข้อความนี้
        if await add_event_and_xp(user.id, RULES["reaction"], "reaction",
                                  chat_id, msg_id, meta=",".join(new_set)):
            async with aiosqlite.connect(DB) as db:
                await db.execute("""INSERT OR IGNORE INTO reacted_once(user_id,chat_id,message_id)
                                    VALUES(?,?,?)""", (user.id, chat_id, msg_id))
                await db.commit()
        return

    # เคสเปลี่ยนรีแอคชัน: ไม่บวกเพิ่ม (ผ่านไปเฉย ๆ)
    if old_set and new_set and old_set != new_set:
        return

    # เคสลบรีแอคชันจนไม่เหลือ: (เลือกได้ว่าจะหักแต้มไหม)
    if old_set and not new_set:
        # หักเฉพาะถ้าเคยนับข้อความนี้มาก่อน
        if await add_event_and_xp(user.id, RULES["reaction_remove"], "reaction_remove",
                                  chat_id, msg_id, meta=",".join(old_set)):
            # ไม่ลบ reacted_once นะ เพื่อกันปั๊มแต้มเพิ่ม-ลบวนไป
            pass

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    pa = update.poll_answer
    if not pa or not pa.user:
        return
    user = pa.user
    poll_id = pa.poll_id

    await upsert_user(user.id, user.username)

    # นับครั้งเดียวต่อโพลต่อคน
    async with aiosqlite.connect(DB) as db:
        try:
            await db.execute("INSERT INTO polled_once(user_id,poll_id) VALUES(?,?)", (user.id, poll_id))
            await db.commit()
            await add_event_and_xp(user.id, RULES["poll_answer"], "poll_answer",
                                   chat_id=0, message_id=0, meta=poll_id)
        except aiosqlite.IntegrityError:
            pass  # เคยนับแล้ว

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "กติกาปัจจุบัน:\n"
        f"• Reaction ครั้งแรกบนข้อความหนึ่ง: +{RULES['reaction']} XP\n"
        f"• ลบ reaction สุดท้าย: {RULES['reaction_remove']} XP\n"
        f"• ตอบโพล (บอทเป็นคนส่ง, non-anonymous): +{RULES['poll_answer']} XP"
    )

# ---------- Main ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN in environment")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("rules", cmd_rules))

    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_handler(PollAnswerHandler(on_poll_answer))

    # สำคัญ: เปิดรับอัปเดตประเภทที่ต้องใช้
    app.run_polling(
        allowed_updates=["message_reaction", "message_reaction_count", "poll", "poll_answer"]
    )

if __name__ == "__main__":
    asyncio.run(init_db())
    main()
