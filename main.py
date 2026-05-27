import os
import re
import asyncio
import logging
import psycopg2
from datetime import date
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

JOIN_DELAY_SECONDS = 20
SCRAPE_INTERVAL_SECONDS = 30
AUTO_BUMP_SECONDS = 10
DELETE_AFTER_SUCCESS_SECONDS = 30

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is missing")


def db():
    return psycopg2.connect(DATABASE_URL)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def calc_max_winners(likes, comments, reposts, bookmarks):
    targets = [likes, comments, reposts, bookmarks]
    non_zero = [x for x in targets if x > 0]
    return max(non_zero) if non_zero else 25


def parse_targets(text):
    parts = text.strip().split()
    reward = 25
    likes = comments = reposts = bookmarks = 0

    for part in parts:
        p = part.lower().strip()
        if p.isdigit():
            reward = int(p)
        elif p.startswith("xp") and p[2:].isdigit():
            reward = int(p[2:])
        elif p.startswith("l") and p[1:].isdigit():
            likes = int(p[1:])
        elif p.startswith("c") and p[1:].isdigit():
            comments = int(p[1:])
        elif p.startswith("r") and p[1:].isdigit():
            reposts = int(p[1:])
        elif p.startswith("b") and p[1:].isdigit():
            bookmarks = int(p[1:])
    return reward, likes, comments, reposts, bookmarks


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
        close_reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '1 hour'),
        max_winners INTEGER DEFAULT 25,
        target_likes INTEGER DEFAULT 0,
        target_comments INTEGER DEFAULT 0,
        target_reposts INTEGER DEFAULT 0,
        target_bookmarks INTEGER DEFAULT 0,
        current_likes INTEGER DEFAULT 0,
        current_comments INTEGER DEFAULT 0,
        current_reposts INTEGER DEFAULT 0,
        current_bookmarks INTEGER DEFAULT 0,
        telegram_chat_id BIGINT,
        telegram_message_id BIGINT,
        last_bumped_at TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS completions (
        id SERIAL PRIMARY KEY,
        telegram_id BIGINT,
        raid_id INTEGER,
        joined BOOLEAN DEFAULT FALSE,
        joined_at TIMESTAMP,
        eligible BOOLEAN DEFAULT TRUE,
        awarded BOOLEAN DEFAULT FALSE,
        completed_at TIMESTAMP,
        UNIQUE(telegram_id, raid_id)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_raids (
        telegram_id BIGINT PRIMARY KEY,
        chat_id BIGINT,
        link TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS raid_messages (
        id SERIAL PRIMARY KEY,
        raid_id INTEGER,
        chat_id BIGINT,
        message_id BIGINT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    for column in [
        "close_reason TEXT",
        "expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + INTERVAL '1 hour')",
        "max_winners INTEGER DEFAULT 25",
        "target_likes INTEGER DEFAULT 0",
        "target_comments INTEGER DEFAULT 0",
        "target_reposts INTEGER DEFAULT 0",
        "target_bookmarks INTEGER DEFAULT 0",
        "current_likes INTEGER DEFAULT 0",
        "current_comments INTEGER DEFAULT 0",
        "current_reposts INTEGER DEFAULT 0",
        "current_bookmarks INTEGER DEFAULT 0",
        "telegram_chat_id BIGINT",
        "telegram_message_id BIGINT",
        "last_bumped_at TIMESTAMP",
    ]:
        cur.execute(f"ALTER TABLE raids ADD COLUMN IF NOT EXISTS {column};")

    for column in [
        "joined BOOLEAN DEFAULT FALSE",
        "joined_at TIMESTAMP",
        "eligible BOOLEAN DEFAULT TRUE",
        "awarded BOOLEAN DEFAULT FALSE",
    ]:
        cur.execute(f"ALTER TABLE completions ADD COLUMN IF NOT EXISTS {column};")

    cur.execute("ALTER TABLE completions ALTER COLUMN completed_at DROP DEFAULT;")
    conn.commit()
    cur.close()
    conn.close()


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
        [InlineKeyboardButton("🚀 Join Raid on X", callback_data="join_raid")],
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


def record_raid_message(raid_id, chat_id, message_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO raid_messages (raid_id, chat_id, message_id) VALUES (%s, %s, %s);",
        (raid_id, chat_id, message_id)
    )
    conn.commit()
    cur.close()
    conn.close()


async def delete_raid_messages(app, raid_id, delay=0):
    if delay > 0:
        await asyncio.sleep(delay)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id, message_id FROM raid_messages WHERE raid_id = %s;", (raid_id,))
    rows = cur.fetchall()

    for chat_id, message_id in rows:
        try:
            await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logging.error(f"Delete failed: {e}")

    cur.execute("DELETE FROM raid_messages WHERE raid_id = %s;", (raid_id,))
    conn.commit()
    cur.close()
    conn.close()


def close_expired_raids(cur):
    cur.execute("""
        UPDATE raids
        SET active = FALSE, close_reason = 'expired'
        WHERE active = TRUE AND expires_at <= NOW();
    """)


def get_active_raid(cur):
    close_expired_raids(cur)
    cur.execute("""
        SELECT id, link, reward, max_winners, target_likes, target_comments,
               target_reposts, target_bookmarks, expires_at, current_likes,
               current_comments, current_reposts, current_bookmarks
        FROM raids
        WHERE active = TRUE
        ORDER BY id DESC
        LIMIT 1;
    """)
    return cur.fetchone()


def parse_number(text):
    text = text.replace(",", "").strip().lower()
    match = re.search(r"([\d.]+)\s*([km]?)", text)
    if not match:
        return 0
    num = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        num *= 1000
    elif suffix == "m":
        num *= 1000000
    return int(num)


def format_raid_message(
    raid_id, link, reward, max_winners, target_likes, target_comments,
    target_reposts, target_bookmarks, expires_at, current_likes=0,
    current_comments=0, current_reposts=0, current_bookmarks=0
):
    return (
        f"🚨 RAID LIVE #{raid_id}\n\n"
        f"{link}\n\n"
        f"Reward: +{reward} XP\n"
        f"XP Winner Slots: {max_winners}\n"
        f"XP pays ONLY if targets are hit.\n\n"
        f"🎯 Live Raid Goals:\n"
        f"❤️ Likes: {current_likes}/{target_likes}\n"
        f"💬 Comments: {current_comments}/{target_comments}\n"
        f"🔁 Reposts: {current_reposts}/{target_reposts}\n"
        f"🔖 Bookmarks: {current_bookmarks}/{target_bookmarks}\n\n"
        f"⏳ Expires: {expires_at}\n\n"
        f"Tap 🚀 Join Raid first, Click X link , then return and tap ✅ Complete."
    )


async def scrape_x_metrics(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page(user_agent="Mozilla/5.0 Chrome/120 Safari/537.36")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(7000)
            text = await page.locator("body").inner_text()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            likes = comments = reposts = bookmarks = 0

            for line in lines:
                lower = line.lower()
                if "like" in lower:
                    likes = max(likes, parse_number(line))
                if "repost" in lower or "retweet" in lower:
                    reposts = max(reposts, parse_number(line))
                if "reply" in lower or "replies" in lower or "comment" in lower:
                    comments = max(comments, parse_number(line))
                if "bookmark" in lower:
                    bookmarks = max(bookmarks, parse_number(line))

            await browser.close()
            return {"likes": likes, "comments": comments, "reposts": reposts, "bookmarks": bookmarks}
        except Exception as e:
            logging.error(f"Scrape failed: {e}")
            await browser.close()
            return None


def award_raid_xp(cur, raid_id, reward):
    cur.execute("""
        SELECT telegram_id
        FROM completions
        WHERE raid_id = %s
          AND joined = TRUE
          AND eligible = TRUE
          AND awarded = FALSE
          AND completed_at IS NOT NULL
        ORDER BY completed_at ASC
    """, (raid_id,))
    winners = [row[0] for row in cur.fetchall()]

    today = date.today()
    awarded_count = 0

    for telegram_id in winners:
        cur.execute("SELECT xp, streak, last_completed FROM users WHERE telegram_id = %s;", (telegram_id,))
        row = cur.fetchone()
        if not row:
            continue

        xp, streak, last_completed = row

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
        """, (total_reward, new_streak, today, telegram_id))

        cur.execute("""
            UPDATE completions
            SET awarded = TRUE
            WHERE telegram_id = %s AND raid_id = %s;
        """, (telegram_id, raid_id))

        awarded_count += 1

    return awarded_count


async def metric_watcher(app):
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            close_expired_raids(cur)

            cur.execute("""
                SELECT id, link, reward, max_winners, target_likes, target_comments,
                       target_reposts, target_bookmarks, expires_at,
                       telegram_chat_id, telegram_message_id
                FROM raids
                WHERE active = TRUE
                ORDER BY id DESC
                LIMIT 1;
            """)
            raid = cur.fetchone()

            if raid:
                raid_id, link, reward, max_winners, target_likes, target_comments, target_reposts, target_bookmarks, expires_at, chat_id, message_id = raid
                metrics = await scrape_x_metrics(link)

                if metrics:
                    cur.execute("""
                        UPDATE raids
                        SET current_likes = %s,
                            current_comments = %s,
                            current_reposts = %s,
                            current_bookmarks = %s
                        WHERE id = %s;
                    """, (metrics["likes"], metrics["comments"], metrics["reposts"], metrics["bookmarks"], raid_id))

                    targets_hit = (
                        (target_likes == 0 or metrics["likes"] >= target_likes) and
                        (target_comments == 0 or metrics["comments"] >= target_comments) and
                        (target_reposts == 0 or metrics["reposts"] >= target_reposts) and
                        (target_bookmarks == 0 or metrics["bookmarks"] >= target_bookmarks)
                    )

                    awarded_count = 0
                    if targets_hit:
                        awarded_count = award_raid_xp(cur, raid_id, reward)
                        cur.execute("UPDATE raids SET active = FALSE, close_reason = 'targets_hit' WHERE id = %s;", (raid_id,))

                    conn.commit()

                    if chat_id and message_id:
                        text = format_raid_message(
                            raid_id, link, reward, max_winners,
                            target_likes, target_comments, target_reposts, target_bookmarks,
                            expires_at, metrics["likes"], metrics["comments"], metrics["reposts"], metrics["bookmarks"]
                        )

                        if targets_hit:
                            text += f"\n\n🎯 TARGETS HIT. RAID CLOSED.\n🏆 XP awarded to {awarded_count} winner(s)."

                        try:
                            await app.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=text,
                                reply_markup=raid_buttons() if not targets_hit else None
                            )
                        except Exception as e:
                            logging.error(f"Telegram edit failed: {e}")

                        if targets_hit:
                            app.create_task(delete_raid_messages(app, raid_id, delay=DELETE_AFTER_SUCCESS_SECONDS))

            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logging.error(f"Metric watcher error: {e}")

        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


async def create_raid_from_values(update, link, reward, likes, comments, reposts, bookmarks):
    max_winners = calc_max_winners(likes, comments, reposts, bookmarks)

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE raids SET active = FALSE, close_reason = 'replaced' WHERE active = TRUE;")
    cur.execute("""
        INSERT INTO raids (
            link, reward, active, expires_at, max_winners,
            target_likes, target_comments, target_reposts, target_bookmarks,
            last_bumped_at
        )
        VALUES (%s, %s, TRUE, NOW() + INTERVAL '1 hour', %s, %s, %s, %s, %s, NOW())
        RETURNING id, expires_at;
    """, (link, reward, max_winners, likes, comments, reposts, bookmarks))

    raid_id, expires_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    sent = await update.message.reply_text(
        format_raid_message(raid_id, link, reward, max_winners, likes, comments, reposts, bookmarks, expires_at),
        reply_markup=raid_buttons()
    )
    record_raid_message(raid_id, sent.chat_id, sent.message_id)

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE raids SET telegram_chat_id = %s, telegram_message_id = %s WHERE id = %s;",
                (sent.chat_id, sent.message_id, raid_id))
    cur.execute("DELETE FROM pending_raids WHERE telegram_id = %s;", (update.effective_user.id,))
    conn.commit()
    cur.close()
    conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "🔥 Welcome to Flik Raidar.\n\n"
        "Admin flow:\n"
        "1. Paste an X URL\n"
        "2. Reply with targets like: 25 l100 c40 r60 b10\n\n"
        "User flow:\n"
        "Join Raid → Click X link → Complete Raid\n\n"
        "Commands:\n"
        "/rank\n/leaderboard\n/active\n/complete\n/myid"
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your Telegram ID:\n{update.effective_user.id}")


async def raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only Flik admins can create raids.")
        return
    if not context.args:
        await update.message.reply_text("Use:\n/raid URL 25 l100 c40 r60 b10")
        return

    link = context.args[0]
    reward, likes, comments, reposts, bookmarks = parse_targets(" ".join(context.args[1:]))
    await create_raid_from_values(update, link, reward, likes, comments, reposts, bookmarks)


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

    sent = await update.message.reply_text(format_raid_message(*raid_row), reply_markup=raid_buttons())
    record_raid_message(raid_row[0], sent.chat_id, sent.message_id)


async def join_raid_for_user(user_id):
    conn = db()
    cur = conn.cursor()
    raid_row = get_active_raid(cur)

    if not raid_row:
        conn.commit()
        cur.close()
        conn.close()
        return None, None

    raid_id, link = raid_row[0], raid_row[1]

    cur.execute("""
        INSERT INTO completions (telegram_id, raid_id, joined, joined_at, eligible, awarded, completed_at)
        VALUES (%s, %s, TRUE, NOW(), TRUE, FALSE, NULL)
        ON CONFLICT (telegram_id, raid_id)
        DO UPDATE SET joined = TRUE, joined_at = NOW();
    """, (user_id, raid_id))

    conn.commit()
    cur.close()
    conn.close()
    return raid_id, link


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

    raid_id, max_winners = raid_row[0], raid_row[3]

    cur.execute("""
        SELECT joined, joined_at, completed_at
        FROM completions
        WHERE telegram_id = %s AND raid_id = %s;
    """, (user.id, raid_id))
    joined_row = cur.fetchone()

    if not joined_row or not joined_row[0]:
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text("⚠️ You must tap 🚀 Join Raid before completing.")
        return

    joined_at, completed_at = joined_row[1], joined_row[2]

    if completed_at is not None:
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text("You already completed this raid.")
        return

    cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - %s));", (joined_at,))
    seconds_waited = int(cur.fetchone()[0])

    if seconds_waited < JOIN_DELAY_SECONDS:
        remaining = JOIN_DELAY_SECONDS - seconds_waited
        conn.commit()
        cur.close()
        conn.close()
        await update.message.reply_text(f"⏳ Spend at least {JOIN_DELAY_SECONDS} seconds on the raid first. Try again in {remaining} sec.")
        return

    cur.execute("""
        SELECT COUNT(*)
        FROM completions
        WHERE raid_id = %s AND eligible = TRUE AND completed_at IS NOT NULL;
    """, (raid_id,))
    eligible_count = cur.fetchone()[0]
    eligible = eligible_count < max_winners

    cur.execute("""
        UPDATE completions
        SET eligible = %s,
            awarded = FALSE,
            completed_at = NOW()
        WHERE telegram_id = %s AND raid_id = %s;
    """, (eligible, user.id, raid_id))

    conn.commit()
    cur.close()
    conn.close()

    if eligible:
        await update.message.reply_text(
            f"✅ You locked XP slot #{eligible_count + 1}/{max_winners}.\n\n"
            "XP is pending and will ONLY be awarded if the raid targets are hit before the raid ends."
        )
    else:
        await update.message.reply_text("⚠️ XP winner slots are full. You can still support the raid, but this completion is not eligible for XP.")


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
        f"🔥 Your Flik Raidar Profile\n\nXP: {xp}\nRank: {rank_name(xp)}\nRaids Completed: {raids_completed}\nStreak: {streak} day(s)"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT username, xp, raids_completed FROM users ORDER BY xp DESC LIMIT 10;")
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
        await update.message.reply_text("Only admins can adjust targets.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Use:\n/settargets raid_id 50 l100 c40 r60 b10")
        return

    try:
        raid_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid raid ID.")
        return

    reward, likes, comments, reposts, bookmarks = parse_targets(" ".join(context.args[1:]))
    max_winners = calc_max_winners(likes, comments, reposts, bookmarks)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE raids
        SET reward=%s, target_likes=%s, target_comments=%s, target_reposts=%s, target_bookmarks=%s, max_winners=%s
        WHERE id=%s;
    """, (reward, likes, comments, reposts, bookmarks, max_winners, raid_id))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        f"🎯 Raid #{raid_id} updated:\n\nXP Reward: {reward}\nXP Winner Slots: {max_winners}\n❤️ Likes: {likes}\n💬 Comments: {comments}\n🔁 Reposts: {reposts}\n🔖 Bookmarks: {bookmarks}"
    )


def clean_username(arg):
    return arg.replace("@", "").strip().lower()


async def find_user_by_username(cur, username):
    cur.execute("""
        SELECT telegram_id, username
        FROM users
        WHERE LOWER(username) = %s
        LIMIT 1;
    """, (clean_username(username),))
    return cur.fetchone()


async def addxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can adjust XP.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Use: /addxp @username amount")
        return

    username = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    conn = db()
    cur = conn.cursor()
    user_row = await find_user_by_username(cur, username)
    if not user_row:
        cur.close()
        conn.close()
        await update.message.reply_text("User not found. They need to use the bot once first.")
        return

    telegram_id, found_username = user_row
    cur.execute("UPDATE users SET xp = xp + %s WHERE telegram_id = %s;", (amount, telegram_id))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"✅ Added {amount} XP to @{found_username}.")


async def removexp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can adjust XP.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Use: /removexp @username amount")
        return

    username = context.args[0]
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    conn = db()
    cur = conn.cursor()
    user_row = await find_user_by_username(cur, username)
    if not user_row:
        cur.close()
        conn.close()
        await update.message.reply_text("User not found. They need to use the bot once first.")
        return

    telegram_id, found_username = user_row
    cur.execute("UPDATE users SET xp = GREATEST(xp - %s, 0) WHERE telegram_id = %s;", (amount, telegram_id))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"✅ Removed {amount} XP from @{found_username}.")


async def dq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can disqualify users.")
        return
    if len(context.args) < 1:
        await update.message.reply_text("Use: /dq @username")
        return

    conn = db()
    cur = conn.cursor()
    user_row = await find_user_by_username(cur, context.args[0])
    if not user_row:
        cur.close()
        conn.close()
        await update.message.reply_text("User not found. They need to use the bot once first.")
        return

    telegram_id, found_username = user_row
    cur.execute("""
        UPDATE users
        SET xp = 0, raids_completed = 0, streak = 0, last_completed = NULL
        WHERE telegram_id = %s;
    """, (telegram_id,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"🚫 @{found_username} has been disqualified. All XP and raid stats removed.")


async def resetleaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can reset the leaderboard.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET xp = 0, raids_completed = 0, streak = 0, last_completed = NULL;")
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text("🏆 Leaderboard reset complete. All XP and raid stats cleared.")


async def endraid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can end raids.")
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM raids WHERE active = TRUE ORDER BY id DESC LIMIT 1;")
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        await update.message.reply_text("No active raid.")
        return

    raid_id = row[0]
    cur.execute("UPDATE raids SET active = FALSE, close_reason = 'admin_ended' WHERE id = %s;", (raid_id,))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text("Raid ended by admin. No pending XP will be awarded.")
    await delete_raid_messages(context.application, raid_id, delay=0)


async def auto_raid_from_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = update.message.text.strip()
    if "x.com/" not in text and "twitter.com/" not in text:
        return

    link = text.split()[0]

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending_raids (telegram_id, chat_id, link)
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id)
        DO UPDATE SET chat_id = EXCLUDED.chat_id, link = EXCLUDED.link, created_at = CURRENT_TIMESTAMP;
    """, (update.effective_user.id, update.effective_chat.id, link))
    conn.commit()
    cur.close()
    conn.close()

    await update.message.reply_text(
        "🎯 X URL saved.\n\nReply with targets like:\n25 l100 c40 r60 b10\n\nMeaning: 25 XP, 100 likes, 40 comments, 60 reposts, 10 bookmarks."
    )


async def launch_pending_raid_from_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = update.message.text.strip().lower()
    if "x.com/" in text or "twitter.com/" in text:
        return
    if not re.search(r"(^|\s)(xp\d+|\d+|l\d+|c\d+|r\d+|b\d+)", text):
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT link FROM pending_raids WHERE telegram_id = %s;", (update.effective_user.id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return

    reward, likes, comments, reposts, bookmarks = parse_targets(text)
    await create_raid_from_values(update, row[0], reward, likes, comments, reposts, bookmarks)


async def bump_active_raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if update.effective_user and update.effective_user.is_bot:
        return

    text = update.message.text.strip()
    if text.startswith("/") or "x.com/" in text or "twitter.com/" in text:
        return

    conn = db()
    cur = conn.cursor()
    close_expired_raids(cur)
    cur.execute("""
        SELECT id, link, reward, max_winners, target_likes, target_comments,
               target_reposts, target_bookmarks, expires_at, current_likes,
               current_comments, current_reposts, current_bookmarks
        FROM raids
        WHERE active = TRUE
        ORDER BY id DESC
        LIMIT 1;
    """)
    raid_row = cur.fetchone()

    if not raid_row:
        conn.commit()
        cur.close()
        conn.close()
        return

    cur.execute("""
        SELECT CASE
            WHEN last_bumped_at IS NULL THEN TRUE
            WHEN last_bumped_at <= NOW() - INTERVAL '10 seconds' THEN TRUE
            ELSE FALSE
        END
        FROM raids WHERE id = %s;
    """, (raid_row[0],))
    can_bump = cur.fetchone()[0]

    if not can_bump:
        conn.commit()
        cur.close()
        conn.close()
        return

    sent = await update.message.reply_text(format_raid_message(*raid_row), reply_markup=raid_buttons())
    record_raid_message(raid_row[0], sent.chat_id, sent.message_id)

    cur.execute("""
        UPDATE raids
        SET telegram_chat_id=%s, telegram_message_id=%s, last_bumped_at=NOW()
        WHERE id=%s;
    """, (sent.chat_id, sent.message_id, raid_row[0]))

    conn.commit()
    cur.close()
    conn.close()


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "join_raid":
        ensure_user(query.from_user)
        raid_id, link = await join_raid_for_user(query.from_user.id)

        if not raid_id:
            await query.message.reply_text("No active raid right now.")
            return

        await query.message.reply_text(
            f"🚀 Raid joined.\n\nOpen X here:\n{link}\n\nReturn after {JOIN_DELAY_SECONDS} seconds and tap ✅ Complete Raid.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Open X Raid", url=link)]])
        )
        return

    class FakeMessage:
        async def reply_text(self, text, **kwargs):
            await query.message.reply_text(text, **kwargs)

    class FakeUpdate:
        effective_user = query.from_user
        message = FakeMessage()

    if query.data == "complete_raid":
        await complete(FakeUpdate(), context)

    if query.data == "leaderboard":
        await leaderboard(FakeUpdate(), context)


async def post_init(app):
    app.create_task(metric_watcher(app))


def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("raid", raid))
    app.add_handler(CommandHandler("complete", complete))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("settargets", settargets))
    app.add_handler(CommandHandler("addxp", addxp))
    app.add_handler(CommandHandler("removexp", removexp))
    app.add_handler(CommandHandler("dq", dq))
    app.add_handler(CommandHandler("resetleaderboard", resetleaderboard))
    app.add_handler(CommandHandler("endraid", endraid))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_raid_from_url), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, launch_pending_raid_from_targets), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bump_active_raid), group=2)

    print("Flik Raidar is live.")
    app.run_polling()


if __name__ == "__main__":
    main()
