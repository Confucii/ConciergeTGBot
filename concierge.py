#!/usr/bin/env python
# Telegram Welcome Bot - Greets new members with scheduled follow-up messages
# Now with SQLite persistence
import logging
import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
    BotCommandScopeAllPrivateChats,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)
from telegram.error import TelegramError, BadRequest, Forbidden
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
GROUP_RULES_LINK = os.getenv("GROUP_RULES_LINK")
NEW_MEMBERS_FORM_LINK = os.getenv("NEW_MEMBERS_FORM_LINK")

# Initialize database manager
db = DatabaseManager.DatabaseManager(DB_PATH)


async def set_menu_button_and_commands(application):
    await application.bot.set_my_commands(
        [BotCommand("notifications", "Toggle event notifications")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands(),
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
        return f"✅ Статус подписки обновлён для {user_first_name}. Теперь вы *подписаны*."
    else:
        return f"✅ Статус подписки обновлён для {user_first_name}. Теперь вы *не подписаны*."


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
        return
    user = update.effective_user

    status_msg = await toggle_user_subscription(
        user.id, user.first_name, user.username
    )

    await update.message.reply_text(status_msg, parse_mode="Markdown")


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
                    f"Привет, {mentions[0]}! 👋 Добро пожаловать в нашу группу!\n\n"
                    f"Будьте добры, представьтесь: расскажите немного о себе — кем вы являетесь профессионально и по жизни, "
                    f"в чем нуждаетесь и как можете быть полезны другим участникам группы.\n\n"
                    f'После этого, пожалуйста, заполните форму <a href="{NEW_MEMBERS_FORM_LINK}"><b>базы участников группы</b></a>.\n\n'
                    f'И обязательно прочитайте <a href="{GROUP_RULES_LINK}"><b>Правила нашей группы</b></a> 🧐'
                )
            else:
                mentions_text = ", ".join(mentions[:-1]) + f" и {mentions[-1]}"
                welcome_message = (
                    f"Привет, {mentions_text}! 👋 Добро пожаловать в нашу группу!\n\n"
                    f"Будьте добры, представьтесь: расскажите немного о себе — кем вы являетесь профессионально и по жизни, "
                    f"в чем нуждаетесь и как можете быть полезны другим участникам группы.\n\n"
                    f'После этого, пожалуйста, заполните форму <a href="{NEW_MEMBERS_FORM_LINK}"><b>базы участников группы</b></a>.\n\n'
                    f'И обязательно прочитайте <a href="{GROUP_RULES_LINK}"><b>Правила нашей группы</b></a> 🧐'
                )

            await context.bot.send_message(
                chat_id=chat_id,
                text=welcome_message,
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
    if today.day not in [1, 8, 15, 22]:
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
                    f"Привет, {mentions[0]}! 👋\n\n"
                    f"Уже прошло несколько дней, но Вы так и не представились группе. "
                    f"Пожалуйста, расскажите немного о себе в чате — это помогает всем участникам быстрее адаптироваться.\n\n"
                    f'Не забудьте заполнить <a href="{NEW_MEMBERS_FORM_LINK}"><b>форму участника</b></a>.\n\n'
                    f'И обязательно прочитайте <a href="{GROUP_RULES_LINK}"><b>правила группы</b></a> 🧐'
                )
            else:
                mentions_text = ", ".join(mentions[:-1]) + f" и {mentions[-1]}"
                message = (
                    f"Привет, {mentions_text}! 👋\n\n"
                    f"Уже прошло несколько дней, но вы так и не представились группе. "
                    f"Пожалуйста, расскажите немного о себе в чате — это помогает всем участникам быстрее адаптироваться.\n\n"
                    f'Не забудьте заполнить <a href="{NEW_MEMBERS_FORM_LINK}"><b>форму участника</b></a>.\n\n'
                    f'И обязательно прочитайте <a href="{GROUP_RULES_LINK}"><b>правила группы</b></a> 🧐'
                )

            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
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
            text="❗ Неверный формат команды #event. Используйте: `#event ГГГГ-ММ-ДД ЧЧ:ММ Локация`",
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
            text="❗ Неверный формат даты. Используйте: `#event ГГГГ-ММ-ДД ЧЧ:ММ Локация`",
            parse_mode="Markdown",
        )
        return None

    now = datetime.datetime.now(eastern)
    if event_datetime <= now:
        await context.bot.send_message(
            chat_id=message.from_user.id,
            text="❗ Дата события должна быть в будущем.",
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
    admins = await context.bot.get_chat_administrators(chat_id)
    is_admin = any(admin.user.id == user.id for admin in admins)

    if not is_admin:
        await message.reply_text(
            "Только администраторы могут создавать события."
        )
        return

    result = await process_event_message(message, context)
    if result:
        event_datetime, location = result
        event_datetime = datetime.datetime.fromisoformat(event_datetime)

        # Send event created notification to all subscribed users
        await send_event_notification_to_subscribers(
            context, message, event_datetime, location, is_new_event=True
        )


async def handle_event_tagged_message_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    logger.info("Handling edited message for event.")
    user = update.effective_user
    edited_msg = update.edited_message
    chat_id = edited_msg.chat_id
    if (
        not edited_msg
        or not edited_msg.text
        or "#event" not in edited_msg.text
    ):
        return

    admins = await context.bot.get_chat_administrators(chat_id)
    is_admin = any(admin.user.id == user.id for admin in admins)

    if not is_admin:
        await edited_msg.reply_text(
            "Только администраторы могут создавать события."
        )
        return

    event_exists = db.get_event(edited_msg.chat_id, edited_msg.message_id)

    result = await process_event_message(edited_msg, context)
    if result:
        event_datetime, location = result
        event_datetime = datetime.datetime.fromisoformat(event_datetime)
        if event_exists:
            # Event updated
            await context.bot.send_message(
                chat_id=edited_msg.chat_id,
                text=f"✏️ *Митап обновлён*\n\n"
                f"📅 *Дата:* {event_datetime.strftime('%Y-%m-%d')}\n"
                f"⏰ *Время:* {event_datetime.strftime('%H:%M')}\n"
                f"📍 *Место:* {location}\n",
                parse_mode="Markdown",
                reply_to_message_id=edited_msg.message_id,
            )

            # Send event changed notification to all subscribed users
            await send_event_notification_to_subscribers(
                context,
                edited_msg,
                is_new_event=False,
            )
        else:
            await send_event_notification_to_subscribers(
                context,
                edited_msg,
                is_new_event=True,
            )


async def send_event_notification_to_subscribers(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    is_new_event: bool,
):
    """Send event notifications to all subscribed users."""
    try:
        users_to_notify = db.get_users_for_notification()
        action_text = "Новый митап" if is_new_event else "Митап обновлён"

        for user in users_to_notify:
            try:
                # Send notification text
                await context.bot.send_message(
                    chat_id=user[0],
                    text=f"📢 *{action_text}*\n\n",
                    parse_mode="Markdown",
                )

                # Forward the actual event message
                await context.bot.forward_message(
                    chat_id=user[0],
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                    disable_notification=True,
                )

            except Exception as e:
                logger.error(
                    f"Failed to send event notification to user {user[0]}: {e}"
                )

    except Exception as e:
        logger.error(f"Failed to get users for notification: {e}")


async def check_and_send_event_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Daily job to check for events that need reminders and send them at 9 AM."""
    events = db.get_events_for_reminders()
    today = datetime.date.today()

    logger.info(f"Checking event reminders for {today}")

    for (
        event_id,
        chat_id,
        message_id,
        _,
        event_datetime,
        location,
        updated_at,
    ) in events:
        # Check each reminder period (7, 3, 1 days before, and event day)
        reminder_days = [7, 3, 1, 0]  # 0 = event day
        event_date = event_datetime.date()

        # Find which reminder should be sent today (if any)
        todays_reminder = None
        for days_before in reminder_days:
            reminder_date = event_date - datetime.timedelta(days=days_before)
            if today == reminder_date:
                todays_reminder = days_before
                break

        # If no reminder is due today, skip this event
        if todays_reminder is None:
            continue

        reminder_datetime = eastern.localize(
            datetime.datetime.combine(today, datetime.time(9, 0))
        )

        # Ensure updated_at is timezone-aware
        updated_at_dt = datetime.datetime.fromisoformat(updated_at)
        if updated_at_dt.tzinfo is None:
            # Assume Eastern if updated_at is naive
            updated_at_dt = eastern.localize(updated_at_dt)

        if reminder_datetime < updated_at_dt:
            logger.info(
                f"Skipping reminder for event {event_id} (reminder time: {reminder_datetime}, updated at: {updated_at_dt})"
            )
            continue

        # Send only today's reminder (skip any missed previous reminders)
        days_before = todays_reminder
        try:
            # Determine reminder text based on days_before
            if days_before == 0:
                reminder_text = (
                    "📅 *Митап группы Нетворкинг состоится уже сегодня!*"
                )
                days_text = "Сегодня"
            elif days_before == 1:
                reminder_text = "⏰ *Напоминаем: митап группы Нетворкинг состоится завтра!*"
                days_text = "Завтра"
            else:
                if days_before >= 4:
                    day = "дней"
                else:
                    day = "дня"
                reminder_text = f"⏰ *Напоминаем: митап группы Нетворкинг состоится через {days_before} {day}!*"
                days_text = f"Через {days_before} {day}"

            message_content = (
                f"{reminder_text}\n\n"
                f"📅 *Дата:* {event_datetime.strftime('%Y-%m-%d')}\n"
                f"⏰ *Время:* {event_datetime.strftime('%H:%M')}\n"
                f"📍 *Место:* {location}\n"
            )

            # Send to subscribed users
            users_to_notify = db.get_users_for_notification()
            logger.info(
                f"Sending {days_text} reminder for event {event_id} to {len(users_to_notify)} users"
            )

            for user in users_to_notify:
                try:
                    await context.bot.send_message(
                        chat_id=user[0],
                        text=message_content,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to send reminder to user {user[0]}: {e}"
                    )

            # Send to group chat
            await context.bot.send_message(
                chat_id=chat_id,
                text=message_content,
                parse_mode="Markdown",
                reply_to_message_id=message_id,
            )

            logger.info(f"Sent {days_text} reminder for event {event_id}")

        except Exception as e:
            logger.error(f"Failed to send reminder for event {event_id}: {e}")


async def cleanup_deleted_events(context: ContextTypes.DEFAULT_TYPE):
    """Check for deleted event messages and clean up database."""
    events = db.get_events_for_reminders()

    for (
        event_id,
        chat_id,
        message_id,
        sender_id,
        event_datetime,
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
        except (BadRequest, Forbidden) as e:
            logger.warning(
                f"Failed to forward message {message_id} for event {event_id}: {e}"
            )
            if (
                "message to forward not found" in str(e).lower()
                or "message_id_invalid" in str(e).lower()
                or "bots can't send messages to bots" in str(e).lower()
                or "bot was blocked by the user" in str(e).lower()
            ):
                # Message is deleted, notify sender and clean up
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"❗**Внимание**: митап группы Нетворкинг отменился!\n\n"
                            f"📅 *Дата митапа:* {event_datetime.strftime('%Y-%m-%d')}\n"
                        ),
                        parse_mode="Markdown",
                    )
                    users_to_notify = db.get_users_for_notification()
                    for user in users_to_notify:
                        try:
                            await context.bot.send_message(
                                chat_id=user[0],
                                text=(
                                    f"❗**Внимание**: митап группы Нетворкинг отменился!\n\n"
                                    f"📅 *Дата митапа:* {event_datetime.strftime('%Y-%m-%d')}\n"
                                ),
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to send event notification to user {user[0]}: {e}"
                            )
                except Exception:
                    pass  # User might have blocked the bot

                # Delete from database
                db.delete_event(chat_id, message_id)
                logger.info(f"Deleted event {event_id} due to deleted message")


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

    # Set commands and menu before polling starts
    application.post_init = set_menu_button_and_commands

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CallbackQueryHandler(
            handle_toggle_subscription, pattern="^toggle_subscribe$"
        )
    )
    application.add_handler(
        CommandHandler("notifications", handle_notifications_command)
    )

    # Add message handler for new chat members
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, greet_new_members
        )
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

    # Schedule daily welcome at 6PM Eastern
    application.job_queue.run_daily(
        send_daily_welcome,
        time=datetime.time(hour=18, minute=00, tzinfo=eastern),  # 6PM Eastern
        name="daily_welcome",
    )

    # Schedule intro reminder check daily at 20PM Eastern (will only send on specific days)
    application.job_queue.run_daily(
        send_intro_reminders,
        time=datetime.time(hour=20, minute=00, tzinfo=eastern),  # 20PM Eastern
        name="intro_reminders",
    )

    application.job_queue.run_daily(
        check_and_send_event_reminders,
        time=datetime.time(hour=9, minute=0, tzinfo=eastern),  # 9 AM Eastern
        name="daily_event_reminders",
    )

    application.job_queue.run_repeating(
        cleanup_deleted_events,
        interval=10800,  # Check every 3 hours
        first=20,  # Start 20 seconds after bot startup
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
