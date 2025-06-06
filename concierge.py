#!/usr/bin/env python
# Telegram Welcome Bot - Greets new members with scheduled follow-up messages
# Now with SQLite persistence
import logging
import datetime
import json
from telegram import (
    Update,
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.error import TelegramError, BadRequest
import pytz
import re
import os
import DatabaseManager
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

EVENT_REGEX = re.compile(
    r"#event\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.+)"
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Your bot token from BotFather
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

eastern = pytz.timezone("US/Eastern")

# Database path - use /data for Fly.io volume mount, fallback to local for development
DB_PATH = os.getenv(
    "DB_PATH",
    "./data/bot.sqlite" if not os.path.exists("/data") else "/data/bot.sqlite",
)

# Group rules link
GROUP_RULES_LINK = os.getenv("GROUP_RULES_LINK", "https://t.me/c/2593760473/3")

# Initialize database manager
db = DatabaseManager.DatabaseManager(DB_PATH)


async def set_menu_button_and_commands(application):
    await application.bot.set_my_commands(
        [BotCommand("notifications", "Toggle event notifications")]
    )
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )


def initialize_user_private_chat(**user_data):
    user_id = user_data.get("user_id")
    db.add_new_user(**user_data)
    db.mark_users_intro_sent(user_id, [user_id])
    db.mark_users_welcomed(user_id, [user_id])
    db.mark_user_posted(user_id, user_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = {
        "chat_id": update.effective_chat.id,
        "user_id": user.id,
        "first_name": user.first_name,
        "username": user.username,
    }
    initialize_user_private_chat(**user_data)  # Mark user as having posted
    welcome_text = f"👋 Welcome, {user.first_name}!\n\nWould you like to subscribe to event notifications?"

    # Inline button
    keyboard = [
        [
            InlineKeyboardButton(
                "🔔 Subscribe to Event Notifications",
                callback_data="toggle_subscribe",
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(welcome_text, reply_markup=reply_markup)


async def toggle_user_subscription(
    user_id: int, user_first_name: str, user_username=None
) -> str:
    if not db.get_user_private_chat(user_id):
        user_data = {
            "chat_id": user_id,
            "user_id": user_id,
            "first_name": user_first_name,
            "username": user_username,
        }
        initialize_user_private_chat(**user_data)

    db.toggle_notification_subscription(user_id)

    if db.get_user_notification_status(user_id):
        return f"✅ Subscription status updated for {user_first_name}. You are now *subscribed*."
    else:
        return f"✅ Subscription status updated for {user_first_name}. You are now *unsubscribed*."


async def handle_toggle_subscription(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user = query.from_user

    status_msg = await toggle_user_subscription(user.id, user.first_name)

    await query.answer()
    await query.edit_message_text(status_msg, parse_mode="Markdown")


async def handle_notifications_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if update.edited_message:
        return  # Ignore edits
    if update.effective_user.id != update.effective_chat.id:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text="❌ You can only toggle notifications in private chat.",
        )
        return
    user = update.effective_user

    status_msg = await toggle_user_subscription(
        user.id, user.first_name, user.username
    )

    await update.message.reply_text(status_msg, parse_mode="Markdown")


async def set_greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = update.effective_user
    logger.info(f"Received message: {message}")

    if not message or not message.reply_to_message:
        await context.bot.send_message(
            chat_id=user.id,
            text="❌ You must reply to the message you want to set as the greeting reference.",
        )
        return

    chat_id = message.chat_id

    # Admin check
    member = await context.bot.get_chat_member(chat_id, user.id)
    if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
        await context.bot.send_message(
            chat_id=user.id, text="Only admins can set the greeting reference."
        )
        return

    # Store in database instead of bot_data
    db.set_setting(
        "welcome_message_id", str(message.reply_to_message.message_id)
    )

    await context.bot.send_message(
        chat_id=user.id, text="✅ Greeting reference message has been set."
    )


async def greet_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Add new members to database without immediate greeting."""
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat

    for new_member in update.message.new_chat_members:
        if new_member.id == context.bot.id:
            continue

        # Add user to database (welcomed flag defaults to 0)
        db.add_new_user(
            chat_id=chat.id,
            user_id=new_member.id,
            username=new_member.username,
            first_name=new_member.first_name,
        )

        logger.info(
            f"Added new member {new_member.username or new_member.first_name} to database"
        )


async def check_and_send_intros(context: ContextTypes.DEFAULT_TYPE):
    """Check for users who need intro messages and send them."""
    # Check for first intros (3 days)
    users_for_first = db.get_users_for_intro("first")
    for chat_id, user_id, username, first_name in users_for_first:
        await send_intro_message(
            context, chat_id, user_id, username, first_name, "first"
        )
        db.mark_intro_sent(chat_id, user_id, "first")

    # Check for second intros (5 days)
    users_for_second = db.get_users_for_intro("second")
    for chat_id, user_id, username, first_name in users_for_second:
        await send_intro_message(
            context, chat_id, user_id, username, first_name, "second"
        )
        db.mark_intro_sent(chat_id, user_id, "second")


async def send_intro_message(
    context, chat_id, user_id, username, first_name, stage
):
    """Send an intro message to a user."""
    if username:
        mention = f"@{username}"
    else:
        mention = f'<a href="tg://user?id={user_id}">{first_name}</a>'

    if stage == "first":
        message = (
            f"Hey {mention}! It's been 3 days since you joined our group.\n\n"
            f"We noticed you haven't said anything yet. Feel free to introduce yourself "
            f"and join our discussions! We'd love to hear from you."
            f'<a href="{GROUP_RULES_LINK}"> Group Rules</a>'
        )
    else:  # second
        message = (
            f"Hello again {mention}!\n\n"
            f"Just checking in as it's been almost a week since you joined. "
            f"If you have any questions about our group or need help with anything, "
            f"don't hesitate to ask! We're here to help."
            f'<a href="{GROUP_RULES_LINK}"> Group Rules</a>'
        )

    try:
        reply_id_str = db.get_setting("welcome_message_id")
        reply_id = int(reply_id_str) if reply_id_str else None

        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML",
            reply_to_message_id=reply_id,
        )
        logger.info(f"Sent {stage} intro message to {username or first_name}")
    except TelegramError as e:
        logger.error(f"Failed to send {stage} intro message: {e}")


async def handle_user_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Mark user as having posted when they send a message."""
    if (
        not update.message
        or not update.message.text
        or update.message.new_chat_members
    ):
        return

    user = update.effective_user
    chat = update.effective_chat

    # Mark user as having posted
    db.mark_user_posted(chat.id, user.id)


async def send_daily_welcome(context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message at 6PM for all unwelcomed users in non-private chats."""
    # Get all users who haven't been welcomed yet in non-private chats
    unwelcomed_users = db.get_unwelcomed_users_non_private()

    if not unwelcomed_users:
        return

    # Group users by chat_id
    users_by_chat = {}
    for chat_id, user_id, username, first_name in unwelcomed_users:
        if chat_id not in users_by_chat:
            users_by_chat[chat_id] = []
        users_by_chat[chat_id].append((user_id, username, first_name))

    # Send ONE welcome message per chat for ALL unwelcomed users in that chat
    for chat_id, users in users_by_chat.items():
        try:
            # Get chat info for the welcome message
            chat = await context.bot.get_chat(chat_id)

            # Create mentions for all users
            mentions = []
            user_ids_to_mark = []

            for user_id, username, first_name in users:
                if username:
                    mentions.append(f"@{username}")
                else:
                    mentions.append(
                        f'<a href="tg://user?id={user_id}">{first_name}</a>'
                    )
                user_ids_to_mark.append(user_id)

            # Skip this chat if no users to welcome (all were in private chats)
            if not mentions:
                continue

            # Create ONE welcome message for ALL users
            if len(mentions) == 1:
                welcome_message = (
                    f"Welcome to {chat.title}, {mentions[0]}! 👋\n\n"
                    f"We're glad to have you here!\n\n"
                    f"Please take a moment to read our group rules and guidelines: "
                    f'<a href="{GROUP_RULES_LINK}">Group Rules</a>'
                )
            else:
                mentions_text = (
                    ", ".join(mentions[:-1]) + f" and {mentions[-1]}"
                )
                welcome_message = (
                    f"Welcome to {chat.title}, {mentions_text}! 👋\n\n"
                    f"We're glad to have you all here!\n\n"
                    f"Please take a moment to read our group rules and guidelines: "
                    f'<a href="{GROUP_RULES_LINK}">Group Rules</a>'
                )

            # Send the message
            reply_id_str = db.get_setting("welcome_message_id")
            reply_id = int(reply_id_str) if reply_id_str else None

            await context.bot.send_message(
                chat_id=chat_id,
                text=welcome_message,
                reply_to_message_id=reply_id,
                parse_mode="HTML",
            )

            # Mark all users as welcomed
            db.mark_users_welcomed(chat_id, user_ids_to_mark)

            logger.info(
                f"Sent welcome message to {len(users)} users in {chat.title}"
            )

        except TelegramError as e:
            logger.error(
                f"Failed to send welcome message to chat {chat_id}: {e}"
            )


async def send_intro_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Send intro reminders on specific days (1st, 8th, 15th, 22nd) for ALL users who haven't posted."""
    today = datetime.date.today()

    # Check if today is one of the notification days
    if today.day not in [6, 8, 15, 22]:
        return

    # Get ALL users who need intro reminders (joined 3+ days ago, haven't posted, not yet sent intro)
    users_for_intro = db.get_users_for_intro_reminder()

    if not users_for_intro:
        return

    # Group users by chat_id
    users_by_chat = {}
    for chat_id, user_id, username, first_name in users_for_intro:
        if chat_id not in users_by_chat:
            users_by_chat[chat_id] = []
        users_by_chat[chat_id].append((user_id, username, first_name))

    # Send ONE intro message per chat for ALL users who need it in that chat
    for chat_id, users in users_by_chat.items():
        try:
            # Create mentions for all users
            mentions = []
            user_ids_to_mark = []

            for user_id, username, first_name in users:
                if username:
                    mentions.append(f"@{username}")
                else:
                    mentions.append(
                        f'<a href="tg://user?id={user_id}">{first_name}</a>'
                    )
                user_ids_to_mark.append(user_id)

            # Skip this chat if no users to notify (all were in private chats)
            if not mentions:
                continue

            # Create ONE intro reminder message for ALL users
            if len(mentions) == 1:
                message = (
                    f"Hey {mentions[0]}! 👋\n\n"
                    f"We noticed you haven't said anything yet since joining our group. "
                    f"Feel free to introduce yourself and join our discussions! "
                    f"We'd love to hear from you.\n\n"
                    f'<a href="{GROUP_RULES_LINK}">Group Rules</a>'
                )
            else:
                mentions_text = (
                    ", ".join(mentions[:-1]) + f" and {mentions[-1]}"
                )
                message = (
                    f"Hey {mentions_text}! 👋\n\n"
                    f"We noticed you haven't said anything yet since joining our group. "
                    f"Feel free to introduce yourselves and join our discussions! "
                    f"We'd love to hear from you all.\n\n"
                    f'<a href="{GROUP_RULES_LINK}">Group Rules</a>'
                )

            # Send the message
            reply_id_str = db.get_setting("welcome_message_id")
            reply_id = int(reply_id_str) if reply_id_str else None

            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                reply_to_message_id=reply_id,
            )

            # Mark all users as having received intro reminder
            db.mark_users_intro_sent(chat_id, user_ids_to_mark)

            logger.info(
                f"Sent intro reminder to {len(users)} users in chat {chat_id}"
            )

        except TelegramError as e:
            logger.error(
                f"Failed to send intro message to chat {chat_id}: {e}"
            )


async def process_event_message(message, context: ContextTypes.DEFAULT_TYPE):
    """Process an event message and store it in the database."""
    text = message.text
    chat_id = message.chat_id
    msg_id = message.message_id

    match = EVENT_REGEX.search(text)
    if not match:
        await context.bot.send_message(
            chat_id=message.from_user.id,
            text="❗ Invalid #event format. Use: `#event YYYY-MM-DD HH:MM Location`",
            parse_mode="Markdown",
        )
        return None

    date_part, time_part, location = match.groups()

    try:
        event_datetime = datetime.datetime.strptime(
            f"{date_part} {time_part}", "%Y-%m-%d %H:%M"
        )
        if event_datetime.tzinfo is None:
            event_datetime = eastern.localize(event_datetime)
    except Exception:
        await context.bot.send_message(
            chat_id=message.from_user.id,
            text="❗ Invalid date format. Use: `#event YYYY-MM-DD HH:MM Location`",
            parse_mode="Markdown",
        )
        return None

    now = datetime.datetime.now(eastern)
    if event_datetime <= now:
        await context.bot.send_message(
            chat_id=message.from_user.id,
            text="❗ Event date must be in the future.",
        )
        return None

    # Store event in database
    event_datetime = event_datetime.isoformat()
    db.add_event(
        chat_id, msg_id, message.from_user.id, event_datetime, location
    )

    return event_datetime, location


async def handle_event_tagged_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    message = update.message
    if not message or not message.text:
        return

    user = update.effective_user
    chat_id = message.chat_id

    # Admin check
    member = await context.bot.get_chat_member(chat_id, user.id)
    if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
        await message.reply_text("Only admins can schedule events.")
        return

    result = await process_event_message(message, context)
    if result:
        event_datetime, location = result
        event_datetime = datetime.datetime.fromisoformat(event_datetime)
        await context.bot.send_message(
            chat_id=user.id,
            text=f"✅ Event scheduled for *{event_datetime.strftime('%Y-%m-%d %H:%M')}*\n"
            f"📍 *Location:* {location}",
            parse_mode="Markdown",
        )


async def handle_event_tagged_message_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    logger.info("Handling edited message for event.")
    edited_msg = update.edited_message
    if (
        not edited_msg
        or not edited_msg.text
        or "#event" not in edited_msg.text
    ):
        return

    result = await process_event_message(edited_msg, context)
    if result:
        event_datetime, location = result
        event_datetime = datetime.datetime.fromisoformat(event_datetime)
        await context.bot.send_message(
            chat_id=edited_msg.from_user.id,
            text=f"✏️ Event updated to *{event_datetime.strftime('%Y-%m-%d %H:%M')}*\n"
            f"📍 *Location:* {location}",
            parse_mode="Markdown",
        )


async def check_and_send_event_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Check for events that need reminders and send them."""
    events = db.get_events_for_reminders()
    now = datetime.datetime.now(eastern)

    for (
        event_id,
        chat_id,
        message_id,
        _,
        event_datetime,
        location,
        reminders_sent_str,
        updated_at,
    ) in events:
        try:
            reminders_sent = (
                json.loads(reminders_sent_str) if reminders_sent_str else []
            )
        except json.JSONDecodeError:
            reminders_sent = []

        # Check each reminder period (7, 5, 1 days before)
        for days_before in [7, 5, 1]:
            if days_before in reminders_sent:
                continue

            reminder_time = event_datetime - datetime.timedelta(
                days=days_before
            )

            if reminder_time < datetime.datetime.fromisoformat(updated_at):
                logger.info(
                    f"Skipping reminder for {event_datetime} (reminder time: {reminder_time}, updated at: {updated_at})"
                )
                continue

            if now >= reminder_time:
                try:
                    users_to_notify = db.get_users_for_notification()
                    logger.info(
                        f"Sending {days_before}-day reminder to users: {users_to_notify}"
                    )
                    for user in users_to_notify:
                        try:
                            await context.bot.send_message(
                                chat_id=user[0],
                                text=(
                                    f"⏰ *Event Reminder* — {days_before} day(s) left!\n\n"
                                    f"📅 *Date:* {event_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                                    f"📍 *Location:* {location}"
                                ),
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to send reminder to user {user[0]}: {e}"
                            )

                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⏰ *Event Reminder* — {days_before} day(s) left!\n\n"
                            f"📅 *Date:* {event_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                            f"📍 *Location:* {location}"
                        ),
                        parse_mode="Markdown",
                        reply_to_message_id=message_id,
                    )

                    # Mark this reminder as sent
                    reminders_sent.append(days_before)
                    db.update_event_reminders(event_id, reminders_sent)

                    logger.info(
                        f"Sent {days_before}-day reminder for event {message_id}"
                    )

                except Exception as e:
                    logger.error(f"Failed to send reminder: {e}")


async def cleanup_deleted_events(context: ContextTypes.DEFAULT_TYPE):
    """Check for deleted event messages and clean up database."""
    events = db.get_events_for_reminders()

    for (
        event_id,
        chat_id,
        message_id,
        sender_id,
        event_datetime,
        location,
        _,
        _,
    ) in events:
        try:
            # Try to forward the message to check if it exists
            forwarded_msg = await context.bot.forward_message(
                chat_id=sender_id,
                from_chat_id=chat_id,
                message_id=message_id,
                disable_notification=True,
            )
            # Delete the forwarded message immediately
            await context.bot.delete_message(
                chat_id=sender_id, message_id=forwarded_msg.message_id
            )
        except BadRequest as e:
            if (
                "message to forward not found" in str(e).lower()
                or "message_id_invalid" in str(e).lower()
            ):
                # Message is deleted, notify sender and clean up
                try:
                    await context.bot.send_message(
                        chat_id=sender_id,
                        text=(
                            f"❗ The event message has been deleted. All reminders have been canceled.\n\n"
                            f"📅 *Date/Time:* {event_datetime.strftime('%Y-%m-%d %H:%M')}\n"
                            f"📍 *Location:* {location}"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass  # User might have blocked the bot

                # Delete from database
                db.delete_event(chat_id, message_id)
                logger.info(
                    f"Deleted event {message_id} due to deleted message"
                )


def unified_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if "#event" in message.text:
        return handle_event_tagged_message(update, context)
    else:
        return handle_user_message(update, context)


def main() -> None:
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("setgreeting", set_greeting))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CallbackQueryHandler(
            handle_toggle_subscription, pattern="^toggle_subscribe$"
        )
    )
    application.add_handler(
        CommandHandler("notifications", handle_notifications_command)
    )

    # Set commands and menu before polling starts
    application.post_init = set_menu_button_and_commands

    # Add message handler for new chat members
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, greet_new_members
        )
    )

    # Schedule daily welcome at 6PM Eastern
    application.job_queue.run_daily(
        send_daily_welcome,
        time=datetime.time(hour=15, minute=45, tzinfo=eastern),  # 6PM Eastern
        name="daily_welcome",
    )

    # Schedule intro reminder check daily at 9AM Eastern (will only send on specific days)
    application.job_queue.run_daily(
        send_intro_reminders,
        time=datetime.time(hour=15, minute=45, tzinfo=eastern),  # 9AM Eastern
        name="intro_reminders",
    )

    application.job_queue.run_repeating(
        check_and_send_event_reminders,
        interval=10,  # Check every 5 minutes
        first=10,  # Start 1 minute after bot startup
    )

    application.job_queue.run_repeating(
        cleanup_deleted_events,
        interval=10,  # Check every 30 minutes
        first=10,  # Start 90 seconds after bot startup
    )

    application.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE, handle_event_tagged_message_edit
        )
    )

    # Add message handler for all messages
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unified_handler)
    )

    # Log when the bot starts
    logger.info("Starting bot with SQLite database...")

    # Start the Bot with error handling
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot started successfully!")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise


if __name__ == "__main__":
    main()
