import os
import logging
import psycopg2
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing")


def db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        username TEXT,
        xp INTEGER DEFAULT 0,
        raids_completed INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        last_completed DATE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS raids (
        id SERIAL PRIMARY KEY,
        link TEXT NOT NULL,
        reward INTEGER DEFAULT 25,
        active BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '1 hour'),
        target_completions INTEGER DEFAULT 10,
        target_likes INTEGER DEFAULT 0,
        target_comments INTEGER DEFAULT 0,
        target_reposts INTEGER DEFAULT 0,
        target_bookmarks INTEGER DEFAULT 0
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS completions (
        id SERIAL PRIMARY KEY,
        telegram_id BIGINT,
        raid_id INTEGER,
        completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(telegram_id, raid_id)
    );
    """)

    cur.execute("ALTER TABLE raids ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '1 hour');")
    cur.execute("ALTER TABLE raids ADD COLUMN IF NOT EXISTS target_completions INTEGER DEFAULT 10;")
    cur.execute("ALTER TABLE raids ADD COLUMN IF NOT EXISTS target_likes INTEGER DEFAULT 0;")
    cur.execute("ALTER TABLE raids ADD COLUMN IF NOT EXISTS target_comments INTEGER DEFAULT 0;")
    cur.execute("ALTER TABLE raids ADD COLUMN IF NOT EXISTS target_reposts INTEGER DEFAULT 0;")
    cur.execute("ALTER TABLE raids ADD COLUMN IF NOT EXISTS target_bookmarks INTEGER DEFAULT 0;")

    conn.commit()
    cur.close()
    conn.close()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def rank_name(xp):
    if xp >= 10000:
        return "🌕 Moonbringer"
    if xp >= 5000:
        return "👑 FLIK Commander"
    if xp >= 2500:
        return "🚀 Elite Raider"
    if xp >= 1000:
        return "⚔️ Raid Captain"
    if xp >= 500:
        return "🔥 Inferno"
    if xp >= 150:
        return "✨ Flame"
    return "⚡ Spark"


def raid_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Open Raid", callback_data="open_raid")],
        [InlineKeyboardButton("✅ Complete Raid", callback_data="complete_raid")],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")]
    ])


def ensure_user(user):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (telegram_id, username)
        VALUES (%s, %s)
        ON CONFLICT (telegram_id)
        DO UPDATE SET username = EXCLUDED.username;
    """, (user.id, user.username or user.first_name))
    conn.commit()
    cur.close()
    conn.close()


def close_expired_raids(cur):
    cur.execute("""
        UPDATE raids
        SET active = FALSE
        WHERE active = TRUE AND expires_at <= NOW();
    """)


def get_active_raid(cur):
    close_expired_raids(cur)
    cur.execute("""
        SELECT id, link, reward, target_completions, target_likes, target_comments,
               target_reposts, target_bookmarks, expires_at
        FROM raids
        WHERE active = TRUE
        ORDER BY id DESC
        LIMIT 1;
    """)
    return cur.fetchone()


def close_raid_if_done(cur, raid_id):
    cur.execute("""
        SELECT target_completions
        FROM raids
        WHERE id = %s AND active = TRUE;
    """, (raid_id,))
    row = cur.fetchone()

    if not row:
        return False

    target = row[0]

    cur.execute("SELECT COUNT(*) FROM completions WHERE raid_id = %s;", (raid_id,))
    count = cur.fetchone()[0]

    if count >= target:
        cur.execute("UPDATE raids SET active = FALSE WHERE id = %s;", (raid_id,))
        return True

    return False


def format_raid_message(raid_id, link, reward, target_completions, likes, comments, reposts, bookmarks, expires_at):
    return (
        f"🚨 RAID LIVE #{raid_id}\n\n"
        f"{link}\n\n"
        f"Reward: +{reward} XP\n"
        f"Completion Target: {target_completions} raiders\n\n"
        f"🎯 Raid Goals:\n"
        f"❤️ Likes: {likes}\n"
        f"💬 Comments: {comments}\n"
        f"🔁 Reposts: {reposts}\n"
        f"🔖 Bookmarks: {bookmarks}\n\n"
        f"⏳ Expires: {expires_at}\n\n"
        f"Tap below to raid, then complete."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "🔥 Welcome to Flik Raidar.\n\n"
        "Raid. Earn XP. Climb the leaderboard.\n\n"
        "Commands:\n"
        "/rank - check XP\n"
        "/leaderboard - top raiders\n"
        "/active - current raid\n"
        "/complete - complete raid\n\n"
        "Admin:\n"
        "/raid URL XP completions likes comments reposts bookmarks\n"
        "/settargets raid_id completions likes comments reposts bookmarks\n"
        "/endraid"
    )


async def raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only Flik admins can create raids.")
        return

    if not context.args:
        await update.message.reply_text(
            "Use:\n/raid URL XP completions likes comments reposts bookmarks\n\n"
            "Example:\n/raid https://x.com/post 25 10 50 20 30 10"
        )
        return

    link = context.args[0]
    reward = int(context.args[1]) if len(context.args) > 1 else 25
    target_completions = int(context.args[2]) if len(context.args) > 2 else 10
    likes = int(context.args[3]) if len(context.args) > 3 else 0
    comments = int(context.args[4]) if len(context.args) > 4 else 0
    reposts = int(context.args[5]) if len(context.args) > 5 else 0
    bookmarks = int(context.args[6]) if len(context.args) > 6 else 0

    conn = db()
    cur = conn.cursor()

    cur.execute("UPDATE raids SET active = FALSE WHERE active = TRUE;")
    cur.execute("""
        INSERT INTO raids (
            link, reward, active, expires_at, target_completions,
            target_likes, target_comments, target_reposts, target_bookmarks
        )
        VALUES (%s, %s, TRUE, NOW() + INTERVAL '1 hour', %s, %s, %s, %s, %s)
        RETURNING id, expires_at;
    """, (link, reward, target_completions, likes, comments, reposts, bookmarks))

    raid_id, expires_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        format_raid_message(raid_id, link, reward, target_completions, likes, comments, reposts, bookmarks, expires_at),
        reply_markup=raid_buttons()
    )


async def active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.cursor()
    raid_row = get_active_raid(cur)
    conn.commit()
    cur.close()
    conn.close()

    if not raid_row:
        await update.message.reply_text("No active raid right now.")
        return

    await update.message.reply_text(
        format_raid_message(*raid_row),
        reply_markup=raid_buttons()
    )


async def complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    conn = db()
    cur = conn.cursor()

    raid_row = get_active_raid(cur)

    if not raid_row:
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text("No active raid to complete.")
        return

    raid_id = raid_row[0]
    reward = raid_row[2]

    try:
        cur.execute(
            "INSERT INTO completions (telegram_id, raid_id) VALUES (%s, %s);",
            (user.id, raid_id)
        )
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        await update.message.reply_text("You already completed this raid.")
        return

    today = date.today()

    cur.execute("SELECT xp, streak, last_completed FROM users WHERE telegram_id = %s;", (user.id,))
    xp, streak, last_completed = cur.fetchone()

    if last_completed == today:
        new_streak = streak
    elif last_completed and (today - last_completed).days == 1:
        new_streak = streak + 1
    else:
        new_streak = 1

    streak_bonus = min(new_streak * 2, 50)
    total_reward = reward + streak_bonus

    cur.execute("""
        UPDATE users
        SET xp = xp + %s,
            raids_completed = raids_completed + 1,
            streak = %s,
            last_completed = %s
        WHERE telegram_id = %s;
    """, (total_reward, new_streak, today, user.id))

    raid_closed = close_raid_if_done(cur, raid_id)

    conn.commit()

    cur.execute("SELECT xp, raids_completed, streak FROM users WHERE telegram_id = %s;", (user.id,))
    new_xp, raids_completed, streak = cur.fetchone()

    cur.close()
    conn.close()

    message = (
        "🔥 Raid completed!\n\n"
        f"+{reward} XP\n"
        f"+{streak_bonus} streak bonus\n\n"
        f"Total XP: {new_xp}\n"
        f"Rank: {rank_name(new_xp)}\n"
        f"Raids Completed: {raids_completed}\n"
        f"Streak: {streak} day(s)"
    )

    if raid_closed:
        message += "\n\n🎯 Raid target hit. Raid is now closed."

    await update.message.reply_text(message)


async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT xp, raids_completed, streak FROM users WHERE telegram_id = %s;", (user.id,))
    xp, raids_completed, streak = cur.fetchone()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"🔥 Your Flik Raidar Profile\n\n"
        f"XP: {xp}\n"
        f"Rank: {rank_name(xp)}\n"
        f"Raids Completed: {raids_completed}\n"
        f"Streak: {streak} day(s)"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, xp, raids_completed
        FROM users
        ORDER BY xp DESC
        LIMIT 10;
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        await update.message.reply_text("No raiders yet.")
        return

    text = "🏆 FLIK RAIDAR LEADERBOARD\n\n"
    for i, (username, xp, raids_completed) in enumerate(rows, start=1):
        text += f"{i}. @{username} — {xp} XP | {raids_completed} raids\n"

    await update.message.reply_text(text)


async def settargets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can adjust raid targets.")
        return

    if len(context.args) < 6:
        await update.message.reply_text(
            "Use:\n/settargets raid_id completions likes comments reposts bookmarks\n\n"
            "Example:\n/settargets 1 20 100 40 60 25"
        )
        return

    raid_id = int(context.args[0])
    completions = int(context.args[1])
    likes = int(context.args[2])
    comments = int(context.args[3])
    reposts = int(context.args[4])
    bookmarks = int(context.args[5])

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE raids
        SET target_completions = %s,
            target_likes = %s,
            target_comments = %s,
            target_reposts = %s,
            target_bookmarks = %s
        WHERE id = %s;
    """, (completions, likes, comments, reposts, bookmarks, raid_id))

    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"🎯 Raid #{raid_id} targets updated:\n\n"
        f"Completions: {completions}\n"
        f"Likes: {likes}\n"
        f"Comments: {comments}\n"
        f"Reposts: {reposts}\n"
        f"Bookmarks: {bookmarks}"
    )


async def endraid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can end raids.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE raids SET active = FALSE WHERE active = TRUE;")
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text("Raid ended.")


async def auto_raid_from_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = update.message.text.strip()

    if "x.com/" not in text and "twitter.com/" not in text:
        return

    link = text.split()[0]
    reward = 25
    target_completions = 10
    likes = 50
    comments = 20
    reposts = 30
    bookmarks = 10

    conn = db()
    cur = conn.cursor()

    cur.execute("UPDATE raids SET active = FALSE WHERE active = TRUE;")
    cur.execute("""
        INSERT INTO raids (
            link, reward, active, expires_at, target_completions,
            target_likes, target_comments, target_reposts, target_bookmarks
        )
        VALUES (%s, %s, TRUE, NOW() + INTERVAL '1 hour', %s, %s, %s, %s, %s)
        RETURNING id, expires_at;
    """, (link, reward, target_completions, likes, comments, reposts, bookmarks))

    raid_id, expires_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        format_raid_message(raid_id, link, reward, target_completions, likes, comments, reposts, bookmarks, expires_at),
        reply_markup=raid_buttons()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "open_raid":
        conn = db()
        cur = conn.cursor()
        row = get_active_raid(cur)
        conn.commit()
        cur.close()
        conn.close()

        if not row:
            await query.message.reply_text("No active raid right now.")
            return

        await query.message.reply_text(f"🚀 Open raid:\n{row[1]}")
        return

    if query.data == "complete_raid":
        class FakeMessage:
            async def reply_text(self, text, **kwargs):
                await query.message.reply_text(text, **kwargs)

        class FakeUpdate:
            effective_user = query.from_user
            message = FakeMessage()

        await complete(FakeUpdate(), context)
        return

    if query.data == "leaderboard":
        class FakeMessage:
            async def reply_text(self, text, **kwargs):
                await query.message.reply_text(text, **kwargs)

        class FakeUpdate:
            effective_user = query.from_user
            message = FakeMessage()

        await leaderboard(FakeUpdate(), context)
        return


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("raid", raid))
    app.add_handler(CommandHandler("active", active))
    app.add_handler(CommandHandler("complete", complete))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("settargets", settargets))
    app.add_handler(CommandHandler("endraid", endraid))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_raid_from_url))

    print("Flik Raidar is live.")
    app.run_polling()


if __name__ == "__main__":
    main()
