import os
import logging
import psycopg2
from datetime import datetime, date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    conn.commit()
    cur.close()
    conn.close()


def rank_name(xp):
    if xp >= 5000:
        return "🔥 FLIK Commander"
    if xp >= 2500:
        return "🚀 Elite Raider"
    if xp >= 1000:
        return "⚔️ Raid Captain"
    if xp >= 500:
        return "🔥 Inferno"
    if xp >= 150:
        return "✨ Flame"
    return "⚡ Spark"


def is_admin(user_id):
    return user_id in ADMIN_IDS


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    await update.message.reply_text(
        "🔥 Welcome to Flik Raidar.\n\n"
        "Raid. Earn XP. Climb the leaderboard.\n\n"
        "Commands:\n"
        "/rank - check your XP\n"
        "/leaderboard - top raiders\n"
        "/active - current raid\n"
        "/complete - complete current raid"
    )


async def raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("Only Flik admins can create raids.")
        return

    if not context.args:
        await update.message.reply_text("Use: /raid https://x.com/post-link")
        return

    link = context.args[0]
    reward = 25

    if len(context.args) > 1:
        try:
            reward = int(context.args[1])
        except ValueError:
            reward = 25

    conn = db()
    cur = conn.cursor()

    cur.execute("UPDATE raids SET active = FALSE WHERE active = TRUE;")
    cur.execute(
        "INSERT INTO raids (link, reward, active) VALUES (%s, %s, TRUE) RETURNING id;",
        (link, reward)
    )

    raid_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"🚨 RAID LIVE #{raid_id}\n\n"
        f"{link}\n\n"
        f"Reward: +{reward} XP\n\n"
        "Complete the raid, then type /complete"
    )


async def active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT id, link, reward FROM raids WHERE active = TRUE ORDER BY id DESC LIMIT 1;")
    raid_row = cur.fetchone()

    cur.close()
    conn.close()

    if not raid_row:
        await update.message.reply_text("No active raid right now.")
        return

    raid_id, link, reward = raid_row

    await update.message.reply_text(
        f"🚨 Active Raid #{raid_id}\n\n"
        f"{link}\n\n"
        f"Reward: +{reward} XP\n\n"
        "After you raid, type /complete"
    )


async def complete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT id, reward FROM raids WHERE active = TRUE ORDER BY id DESC LIMIT 1;")
    raid_row = cur.fetchone()

    if not raid_row:
        await update.message.reply_text("No active raid to complete.")
        cur.close()
        conn.close()
        return

    raid_id, reward = raid_row

    try:
        cur.execute(
            "INSERT INTO completions (telegram_id, raid_id) VALUES (%s, %s);",
            (user.id, raid_id)
        )
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        await update.message.reply_text("You already completed this raid.")
        cur.close()
        conn.close()
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

    conn.commit()

    cur.execute("SELECT xp, raids_completed, streak FROM users WHERE telegram_id = %s;", (user.id,))
    new_xp, raids_completed, streak = cur.fetchone()

    cur.close()
    conn.close()

    await update.message.reply_text(
        "🔥 Raid completed!\n\n"
        f"+{reward} XP\n"
        f"+{streak_bonus} streak bonus\n\n"
        f"Total XP: {new_xp}\n"
        f"Rank: {rank_name(new_xp)}\n"
        f"Raids Completed: {raids_completed}\n"
        f"Streak: {streak} day(s)"
    )


async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT xp, raids_completed, streak
        FROM users
        WHERE telegram_id = %s;
    """, (user.id,))

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

    for i, row in enumerate(rows, start=1):
        username, xp, raids_completed = row
        text += f"{i}. @{username} — {xp} XP | {raids_completed} raids\n"

    await update.message.reply_text(text)


async def addxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can add XP.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Use: /addxp telegram_id amount")
        return

    telegram_id = int(context.args[0])
    amount = int(context.args[1])

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users (telegram_id, username, xp)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id)
        DO UPDATE SET xp = users.xp + EXCLUDED.xp;
    """, (telegram_id, "manual_user", amount))

    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(f"Added {amount} XP to {telegram_id}.")


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


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("raid", raid))
    app.add_handler(CommandHandler("active", active))
    app.add_handler(CommandHandler("complete", complete))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("addxp", addxp))
    app.add_handler(CommandHandler("endraid", endraid))

    print("Flik Raidar is live.")
    app.run_polling()


if __name__ == "__main__":
    main()
