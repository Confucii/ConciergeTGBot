#!/usr/bin/env python
# Telegram Welcome Bot - Greets new members with scheduled follow-up messages

from collections import defaultdict
import logging
import datetime
from telegram import Update, ChatMember
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, BadRequest
import pytz
import re

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
TOKEN = "YOUR_TOKEN"
eastern = pytz.timezone("US/Eastern")


def get_user_data(context):
    if "user_data" not in context.bot_data:
        context.bot_data["user_data"] = defaultdict(dict)
    return context.bot_data["user_data"]


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

    context.application.bot_data["welcome_message_id"] = (
        message.reply_to_message.message_id
    )
    await context.bot.send_message(
        chat_id=user.id, text="✅ Greeting reference message has been set."
    )


# Welcome new members and schedule follow-up messages
async def greet_new_members(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Greets new members and schedules follow-up messages."""
    # Make sure we have new members
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat
    user_data = get_user_data(context)

    # Welcome each new member
    for new_member in update.message.new_chat_members:
        # Skip welcoming the bot itself
        if new_member.id == context.bot.id:
            continue

        if new_member.username:
            mention = f"@{new_member.username}"
        else:
            mention = f'<a href="tg://user?id={new_member.id}">{new_member.first_name}</a>'

        # Create a welcome message
        welcome_message = (
            f"Welcome to {chat.title}, {mention}! 👋\n\n"
            f"We're glad to have you here!\n\n"
            f"Please take a moment to read our group rules and guidelines. "
        )

        # Send the welcome message
        try:
            reply_id = context.application.bot_data.get("welcome_message_id")

            await context.bot.send_message(
                chat_id=chat.id,
                text=welcome_message,
                reply_to_message_id=reply_id if reply_id else None,
            )
            logger.info(f"Welcomed {mention} to {chat.title}")

            # Schedule first intro message for 3 days later
            chat_and_user = f"{chat.id}_{new_member.id}"
            user_data[chat_and_user]["join_time"] = datetime.datetime.now()

            # Schedule first intro (3 days)
            first_intro_job = context.job_queue.run_once(
                send_intro,
                datetime.timedelta(
                    seconds=10
                ),  # days 3 or seconds=10 for testing
                data={
                    "chat_id": chat.id,
                    "user_id": new_member.id,
                    "user_name": new_member.username,
                    "user_first_name": new_member.first_name,
                    "stage": "first",
                },
                name=f"first_intro_{chat_and_user}",
            )
            user_data[chat_and_user][
                "first_intro_job_id"
            ] = first_intro_job.job.id

            # Schedule second intro (5 days)
            second_intro_job = context.job_queue.run_once(
                send_intro,
                datetime.timedelta(
                    seconds=30
                ),  # days 5 or seconds=60 for testing
                data={
                    "chat_id": chat.id,
                    "user_id": new_member.id,
                    "user_name": new_member.username,
                    "user_first_name": new_member.first_name,
                    "stage": "second",
                },
                name=f"second_intro_{chat_and_user}",
            )
            user_data[chat_and_user][
                "second_intro_job_id"
            ] = second_intro_job.job.id

            logger.info(
                f"Scheduled intro messages for {new_member.first_name} in {chat.title}"
            )

        except TelegramError as e:
            logger.error(f"Failed to send welcome message: {e}")


async def send_intro(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a personalized intro message based on stage (first or second)."""
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    user_id = job_data["user_id"]
    user_name = job_data["user_name"]
    user_first_name = job_data["user_first_name"]
    stage = job_data["stage"]  # "first" or "second"

    user_data = get_user_data(context)
    chat_and_user = f"{chat_id}_{user_id}"

    if user_name:
        mention = f"@{user_name}"
    else:
        mention = f'<a href="tg://user?id={user_id}">{user_first_name}</a>'

    if stage == "first":
        message = (
            f"Hey {mention}! It's been 3 days since you joined our group.\n\n"
            f"We noticed you haven't said anything yet. Feel free to introduce yourself "
            f"and join our discussions! We'd love to hear from you."
        )
        job_key = "first_intro_job_id"
    elif stage == "second":
        message = (
            f"Hello again {mention}!\n\n"
            f"Just checking in as it's been almost a week since you joined. "
            f"If you have any questions about our group or need help with anything, "
            f"don't hesitate to ask! We're here to help."
        )
        job_key = "second_intro_job_id"
    else:
        logger.warning(f"Unknown intro stage: {stage}")
        return

    try:
        reply_id = context.application.bot_data.get("welcome_message_id")
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML",
            reply_to_message_id=reply_id,
        )
        del user_data[chat_and_user][job_key]
        logger.info(f"Sent {stage} intro message to {user_name}")
    except TelegramError as e:
        logger.error(f"Failed to send {stage} intro message: {e}")


async def handle_user_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Cancel scheduled intro messages when a user posts anything."""
    # Only process text messages from users
    if (
        not update.message
        or not update.message.text
        or update.message.new_chat_members
    ):
        logger.info("Ignoring non-text message or new chat members.")
        return

    logger.info("Handling user message to cancel scheduled intros.")

    user = update.effective_user
    chat = update.effective_chat
    chat_and_user = f"{chat.id}_{user.id}"

    user_data = get_user_data(context)

    # Check if this user has scheduled intros
    if chat_and_user in user_data:
        # Cancel first intro if it exists
        if "first_intro_job_id" in user_data[chat_and_user]:
            job_id = user_data[chat_and_user]["first_intro_job_id"]
            current_jobs = context.job_queue.get_jobs_by_name(
                f"first_intro_{chat_and_user}"
            )
            for job in current_jobs:
                if job.id == job_id:
                    job.schedule_removal()
                    logger.info(
                        f"Canceled first intro message for {user.first_name}"
                    )

        # Cancel second intro if it exists
        if "second_intro_job_id" in user_data[chat_and_user]:
            job_id = user_data[chat_and_user]["second_intro_job_id"]
            current_jobs = context.job_queue.get_jobs_by_name(
                f"second_intro_{chat_and_user}"
            )
            for job in current_jobs:
                if job.id == job_id:
                    job.schedule_removal()
                    logger.info(
                        f"Canceled second intro message for {user.first_name}"
                    )

        # Clean up user data
        del user_data[chat_and_user]


async def process_event_message(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    bot_data: dict,
    job_queue,
):
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

    # Cancel old jobs if editing
    if msg_id in bot_data.get("events", {}):
        for job in bot_data["events"][msg_id]["jobs"]:
            job.schedule_removal()

    jobs = []
    for days_before in [7, 5, 1]:
        reminder_dt = event_datetime - datetime.timedelta(days=days_before)
        if reminder_dt > now:
            job = job_queue.run_once(
                reminder_callback,
                when=reminder_dt,
                data={
                    "chat_id": chat_id,
                    "msg_id": msg_id,
                    "event_datetime": event_datetime.strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "location": location,
                    "days_before": days_before,
                },
                name=f"event_reminder_{msg_id}_{days_before}",
            )
            jobs.append(job)

    bot_data.setdefault("events", {})[msg_id] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "event_datetime": event_datetime.strftime("%Y-%m-%d %H:%M"),
        "sender_id": message.from_user.id,
        "location": location,
        "jobs": jobs,
    }

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

    event_datetime, location = await process_event_message(
        context=context,
        message=message,
        bot_data=context.application.bot_data,
        job_queue=context.job_queue,
    )

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

    event_datetime, location = await process_event_message(
        context=context,
        message=edited_msg,
        bot_data=context.application.bot_data,
        job_queue=context.job_queue,
    )

    await context.bot.send_message(
        chat_id=edited_msg.from_user.id,
        text=f"✏️ Event updated to *{event_datetime.strftime('%Y-%m-%d %H:%M')}*\n"
        f"📍 *Location:* {location}",
        parse_mode="Markdown",
    )


async def cleanup_deleted_events(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    bot_data = context.application.bot_data

    if "events" not in bot_data:
        return

    to_delete = []

    for msg_id, event in bot_data["events"].items():
        chat_id = event["chat_id"]
        sender_id = event.get("sender_id")

        if sender_id:
            # Try to silently forward the message to the original sender
            # This will fail if the message doesn't exist anymore
            try:
                # Forward to the original sender with notification disabled
                forwarded_msg = await bot.forward_message(
                    chat_id=sender_id,
                    from_chat_id=chat_id,
                    message_id=msg_id,
                    disable_notification=True,
                )
                # Delete the forwarded message immediately
                await bot.delete_message(
                    chat_id=sender_id, message_id=forwarded_msg.message_id
                )
            except BadRequest as e:
                if (
                    "message to forward not found" in str(e).lower()
                    or "message_id_invalid" in str(e).lower()
                ):
                    # Send a message to the original poster with event details
                    await context.bot.send_message(
                        chat_id=sender_id,
                        text=(
                            f"❗ The event message has been deleted. All reminders have been canceled.\n\n"
                            f"📅 *Date/Time:* {event.get('event_datetime')}\n"
                            f"📍 *Location:* {event.get('location')}"
                        ),
                        parse_mode="Markdown",
                    )
                    # Message is deleted
                    for job in event["jobs"]:
                        job.schedule_removal()
                    to_delete.append(msg_id)
        # Delete entries for messages that no longer exist
    for msg_id in to_delete:
        del bot_data["events"][msg_id]

    if to_delete:
        logger.info(
            f"Deleted {len(to_delete)} events due to deleted messages."
        )


def unified_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if "#event" in message.text:
        return handle_event_tagged_message(update, context)
    else:
        return handle_user_message(update, context)


async def reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    try:
        await context.bot.send_message(
            chat_id=job_data["chat_id"],
            text=(
                f"⏰ *Event Reminder* — {job_data['days_before']} day(s) left!\n\n"
                f"📅 *Date:* {job_data['event_datetime']}\n"
                f"📍 *Location:* {job_data['location']}"
            ),
            parse_mode="Markdown",
            reply_to_message_id=job_data["msg_id"],
        )

        # Remove this job from the event's jobs list
        event_id = job_data["msg_id"]
        if (
            "events" in context.application.bot_data
            and event_id in context.application.bot_data["events"]
        ):
            # Find and remove this job from the list
            jobs_list = context.application.bot_data["events"][event_id][
                "jobs"
            ]
            if context.job in jobs_list:
                jobs_list.remove(context.job)

            # If no more jobs left, delete the entire event
            if not jobs_list:
                del context.application.bot_data["events"][event_id]
                print(
                    f"Event {event_id} deleted as all reminders have been sent."
                )

    except Exception as e:
        print(f"Failed to send reminder: {e}")


def main() -> None:
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("setgreeting", set_greeting))

    # Add message handler for new chat members
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, greet_new_members
        )
    )

    application.job_queue.run_repeating(
        cleanup_deleted_events,
        interval=10,  # 30 minutes
        first=10,  # Start 10 seconds after bot startup
    )

    application.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE, handle_event_tagged_message_edit
        )
    )

    # Add message handler for all messages (to cancel scheduled intros)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unified_handler)
    )

    # Log when the bot starts
    logger.info("Starting bot...")

    # Start the Bot with error handling
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot started successfully!")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise


if __name__ == "__main__":
    main()
