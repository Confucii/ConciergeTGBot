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
from telegram.error import TelegramError
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
TOKEN = "7657786948:AAFHWIGrjXLLtqJ-6oFKXXRGXaNw22EaPTM"
eastern = pytz.timezone("US/Eastern")


def get_user_data(context):
    if "user_data" not in context.bot_data:
        context.bot_data["user_data"] = defaultdict(dict)
    return context.bot_data["user_data"]


async def set_greeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    logger.info(f"Received message: {message}")
    if not message or not message.reply_to_message:
        await message.reply_text(
            "❌ You must reply to the message you want to set as the greeting reference."
        )
        return

    user = update.effective_user
    chat_id = message.chat_id

    # Admin check
    member = await context.bot.get_chat_member(chat_id, user.id)
    if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
        await message.reply_text("Only admins can set the greeting reference.")
        return

    context.application.bot_data["welcome_message_id"] = (
        message.reply_to_message.message_id
    )
    await message.reply_text("✅ Greeting reference message has been set.")


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


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.reply_to_message:
        await message.reply_text(
            "❗ Reply to the message you want to attach the event to.\n"
            "Usage: /addevent YYYY-MM-DD HH:MM Location"
        )
        return

    # Admin check
    member = await context.bot.get_chat_member(
        message.chat_id, update.effective_user.id
    )
    if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
        await message.reply_text("Only admins can add events.")
        return

    if len(context.args) < 3:
        await message.reply_text(
            "❗ Please use:\n/addevent YYYY-MM-DD HH:MM Location"
        )
        return

    try:
        date_part = context.args[0]
        time_part = context.args[1]
        event_datetime = datetime.datetime.strptime(
            f"{date_part} {time_part}", "%Y-%m-%d %H:%M"
        )
        location = " ".join(context.args[2:])
    except Exception:
        await message.reply_text(
            "❗ Invalid format.\nUsage: /addevent YYYY-MM-DD HH:MM Location"
        )
        return

    bot_data = context.application.bot_data
    if "events" not in bot_data:
        bot_data["events"] = {}

    event_id = str(message.reply_to_message.message_id)
    chat_id = message.chat_id

    # Schedule reminders
    jobs = []
    for days_before in [7, 5, 1]:
        # Make sure event_datetime is timezone-aware if it isn't already
        if event_datetime.tzinfo is None:
            event_datetime = eastern.localize(event_datetime)

        reminder_dt = event_datetime - datetime.timedelta(days=days_before)

        # Get current time as timezone-aware
        now = datetime.datetime.now(eastern)

        if reminder_dt > now:
            job = context.job_queue.run_once(
                reminder_callback,
                when=reminder_dt,
                data={
                    "chat_id": chat_id,
                    "msg_id": int(event_id),
                    "event_datetime": event_datetime.strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "location": location,
                    "days_before": days_before,
                },
                name=f"event_reminder_{event_id}_{days_before}",
            )
            jobs.append(job)

    # Store event
    bot_data["events"][event_id] = {
        "chat_id": chat_id,
        "msg_id": int(event_id),
        "event_datetime": event_datetime.strftime("%Y-%m-%d %H:%M"),
        "location": location,
        "jobs": jobs,
    }

    await message.reply_text(
        f"✅ Event scheduled for *{event_datetime.strftime('%Y-%m-%d %H:%M')}*.\n"
        f"📍 *Location:* {location}",
        parse_mode="Markdown",
    )


async def edit_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.reply_to_message:
        await message.reply_text(
            "❗ Reply to the event message you want to edit.\n"
            "Usage: /editevent YYYY-MM-DD HH:MM Location"
        )
        return

    # Admin check
    member = await context.bot.get_chat_member(
        message.chat_id, update.effective_user.id
    )
    if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
        await message.reply_text("Only admins can edit events.")
        return

    if len(context.args) < 3:
        await message.reply_text(
            "❗ Please use:\n/editevent YYYY-MM-DD HH:MM Location"
        )
        return

    event_id = str(message.reply_to_message.message_id)
    bot_data = context.application.bot_data

    # Check if event exists
    if "events" not in bot_data or event_id not in bot_data["events"]:
        await message.reply_text(
            "❌ Event not found. Please reply to a valid event message."
        )
        return

    try:
        date_part = context.args[0]
        time_part = context.args[1]
        event_datetime = datetime.datetime.strptime(
            f"{date_part} {time_part}", "%Y-%m-%d %H:%M"
        )
        location = " ".join(context.args[2:])
    except Exception:
        await message.reply_text(
            "❗ Invalid format.\nUsage: /editevent YYYY-MM-DD HH:MM Location"
        )
        return

    chat_id = message.chat_id

    # Remove old jobs first
    for job in bot_data["events"][event_id]["jobs"]:
        job.schedule_removal()

    # Create new jobs with updated data
    jobs = []
    for days_before in [7, 5, 1]:
        # Make sure event_datetime is timezone-aware if it isn't already
        if event_datetime.tzinfo is None:
            event_datetime = eastern.localize(event_datetime)

        reminder_dt = event_datetime - datetime.timedelta(days=days_before)

        # Get current time as timezone-aware
        now = datetime.datetime.now(eastern)

        if reminder_dt > now:
            job = context.job_queue.run_once(
                reminder_callback,
                when=reminder_dt,
                data={
                    "chat_id": chat_id,
                    "msg_id": int(event_id),
                    "event_datetime": event_datetime.strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                    "location": location,
                    "days_before": days_before,
                },
                name=f"event_reminder_{event_id}_{days_before}",
            )
            jobs.append(job)

    # Update event data
    bot_data["events"][event_id] = {
        "chat_id": chat_id,
        "msg_id": int(event_id),
        "event_datetime": event_datetime.strftime("%Y-%m-%d %H:%M"),
        "location": location,
        "jobs": jobs,
    }

    await message.reply_text(
        f"✏️ Event updated to *{event_datetime.strftime('%Y-%m-%d %H:%M')}*.\n"
        f"📍 *Location:* {location}",
        parse_mode="Markdown",
    )


async def delete_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.reply_to_message:
        await message.reply_text("❌ Reply to the event message to delete it.")
        return

    member = await context.bot.get_chat_member(
        message.chat_id, update.effective_user.id
    )
    if member.status not in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
        await message.reply_text("Only admins can delete events.")
        return

    event_id = str(message.reply_to_message.message_id)
    bot_data = context.application.bot_data

    if "events" not in bot_data or event_id not in bot_data["events"]:
        await message.reply_text("❌ Event not found.")
        return

    for job in bot_data["events"][event_id]["jobs"]:
        job.schedule_removal()
    del bot_data["events"][event_id]

    await message.reply_text("🗑️ Event and its reminders have been deleted.")


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
        event_id = str(job_data["msg_id"])
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

    application.add_handler(CommandHandler("addevent", add_event))
    application.add_handler(CommandHandler("editevent", edit_event))
    application.add_handler(CommandHandler("deleteevent", delete_event))
    application.add_handler(CommandHandler("setgreeting", set_greeting))

    # Add message handler for new chat members
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, greet_new_members
        )
    )

    # Add message handler for all messages (to cancel scheduled intros)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message)
    )

    # Log when the bot starts
    logger.info("Starting bot...")

    # Start the Bot with error handling
    try:
        application.run_polling(allowed_updates=["message"])
        logger.info("Bot started successfully!")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        raise


if __name__ == "__main__":
    main()
