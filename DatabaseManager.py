import os
import sqlite3
import datetime
import json
import logging
import pytz

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

eastern = pytz.timezone("US/Eastern")


class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialize the database with required tables."""
        # Ensure directory exists
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        except (OSError, PermissionError) as e:
            logger.warning(
                f"Could not create directory {os.path.dirname(self.db_path)}: {e}"
            )
            # Fallback to current directory
            self.db_path = "./bot.sqlite"
            logger.info(f"Using fallback database path: {self.db_path}")

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()

            # Users table for tracking new members and their intro schedules
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    join_time TIMESTAMP NOT NULL,
                    first_intro_sent BOOLEAN DEFAULT 0,
                    second_intro_sent BOOLEAN DEFAULT 0,
                    notification_subscription BOOLEAN DEFAULT 0,
                    user_posted BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                )
            """
            )

            # Events table for scheduled events
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    event_datetime TIMESTAMP NOT NULL,
                    location TEXT NOT NULL,
                    reminders_sent TEXT DEFAULT '[]',  -- JSON array of sent reminder days
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, message_id)
                )
            """
            )

            # Settings table for bot configuration
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            conn.commit()
            logger.info("Database initialized successfully")

        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise
        finally:
            conn.close()

    def get_connection(self):
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        # Set row factory to return Row objects for easier column access
        conn.row_factory = sqlite3.Row
        return conn

    def add_new_user(self, chat_id, user_id, username, first_name):
        """Add a new user to the database."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO users
                (chat_id, user_id, username, first_name, join_time)
                VALUES (?, ?, ?, ?, ?)
            """,
                (
                    chat_id,
                    user_id,
                    username,
                    first_name,
                    datetime.datetime.now(eastern).isoformat(),
                ),
            )
            conn.commit()
            logger.info(f"Added user {user_id} to database")
        except Exception as e:
            logger.error(f"Error adding user: {e}")
        finally:
            conn.close()

    def get_user_private_chat(self, user_id):
        """Get user details from the database."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM users
                WHERE chat_id = ? AND user_id = ?
            """,
                (user_id, user_id),
            )
            return cursor.fetchone()
        except Exception as e:
            logger.error(f"Error getting user: {e}")
            return None
        finally:
            conn.close()

    def mark_user_posted(self, chat_id, user_id):
        """Mark that a user has posted a message."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE users SET user_posted = 1
                WHERE chat_id = ? AND user_id = ?
            """,
                (chat_id, user_id),
            )
            conn.commit()
            logger.info(f"Marked user {user_id} as posted")
        except Exception as e:
            logger.error(f"Error marking user as posted: {e}")
        finally:
            conn.close()

    def get_user_notification_status(self, user_id):
        """Get the notification subscription status for a user."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT notification_subscription
                FROM users
                WHERE user_id = ?
            """,
                (user_id,),
            )
            result = cursor.fetchone()
            return result["notification_subscription"] if result else None
        except Exception as e:
            logger.error(f"Error getting user notification status: {e}")
            return None
        finally:
            conn.close()

    def toggle_notification_subscription(self, user_id):
        """Toggle the notification subscription for a user."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE users SET notification_subscription = NOT notification_subscription
                WHERE chat_id = ? AND user_id = ?
            """,
                (user_id, user_id),
            )
            conn.commit()
            logger.info(
                f"Toggled notification subscription for user {user_id}"
            )
        except Exception as e:
            logger.error(f"Error toggling notification subscription: {e}")
        finally:
            conn.close()

    def get_users_for_notification(self):
        """Get users who are subscribed to notifications."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT chat_id, user_id, username, first_name
                FROM users
                WHERE notification_subscription = 1
            """
            )
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error getting users for notification: {e}")
            return []
        finally:
            conn.close()

    def get_users_for_intro(self, stage="first"):
        """Get users who need intro messages."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.datetime.now(eastern)

            if stage == "first":
                # Users who joined 3+ days ago, haven't posted, and haven't received first intro
                target_time = (
                    now - datetime.timedelta(seconds=15)
                ).isoformat()
                cursor.execute(
                    """
                    SELECT chat_id, user_id, username, first_name
                    FROM users
                    WHERE user_posted = 0
                    AND first_intro_sent = 0
                    AND join_time <= ?
                """,
                    (target_time,),
                )
            else:  # second
                # Users who joined 5+ days ago, haven't posted, and haven't received second intro
                target_time = (
                    now - datetime.timedelta(seconds=30)
                ).isoformat()
                cursor.execute(
                    """
                    SELECT chat_id, user_id, username, first_name
                    FROM users
                    WHERE user_posted = 0
                    AND second_intro_sent = 0
                    AND join_time <= ?
                """,
                    (target_time,),
                )

            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error getting users for intro: {e}")
            return []
        finally:
            conn.close()

    def mark_intro_sent(self, chat_id, user_id, stage):
        """Mark that an intro message has been sent."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            if stage == "first":
                cursor.execute(
                    """
                    UPDATE users SET first_intro_sent = 1
                    WHERE chat_id = ? AND user_id = ?
                """,
                    (chat_id, user_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE users SET second_intro_sent = 1
                    WHERE chat_id = ? AND user_id = ?
                """,
                    (chat_id, user_id),
                )
            conn.commit()
        except Exception as e:
            logger.error(f"Error marking intro as sent: {e}")
        finally:
            conn.close()

    def add_event(
        self, chat_id, message_id, sender_id, event_datetime, location
    ):
        """Add or update an event."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO events
                (chat_id, message_id, sender_id, event_datetime, location, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    chat_id,
                    message_id,
                    sender_id,
                    event_datetime,
                    location,
                    datetime.datetime.now(eastern).isoformat(),
                ),
            )
            conn.commit()
            logger.info(f"Added/updated event {message_id}")
        except Exception as e:
            logger.error(f"Error adding event: {e}")
        finally:
            conn.close()

    def get_events_for_reminders(self):
        """Get events that need reminders sent."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.datetime.now(eastern)

            cursor.execute(
                """
                SELECT id, chat_id, message_id, sender_id, event_datetime, location, reminders_sent, updated_at
                FROM events
                WHERE event_datetime > ?
            """,
                (now.isoformat(),),
            )

            results = []
            for row in cursor.fetchall():
                # Parse the datetime string back to datetime object
                try:
                    event_dt = datetime.datetime.fromisoformat(
                        row["event_datetime"]
                    )
                    results.append(
                        (
                            row["id"],
                            row["chat_id"],
                            row["message_id"],
                            row["sender_id"],
                            event_dt,
                            row["location"],
                            row["reminders_sent"],
                            row["updated_at"],
                        )
                    )
                except (ValueError, TypeError) as e:
                    logger.error(
                        f"Error parsing event datetime {row['event_datetime']}: {e}"
                    )
                    continue

            return results
        except Exception as e:
            logger.error(f"Error getting events for reminders: {e}")
            return []
        finally:
            conn.close()

    def update_event_reminders(self, event_id, reminders_sent):
        """Update the reminders sent for an event."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE events SET reminders_sent = ? WHERE id = ?
            """,
                (json.dumps(reminders_sent), event_id),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Error updating event reminders: {e}")
        finally:
            conn.close()

    def delete_event(self, chat_id, message_id):
        """Delete an event."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM events WHERE chat_id = ? AND message_id = ?
            """,
                (chat_id, message_id),
            )
            conn.commit()
            logger.info(f"Deleted event {message_id}")
        except Exception as e:
            logger.error(f"Error deleting event: {e}")
        finally:
            conn.close()

    def get_setting(self, key):
        """Get a setting value."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            result = cursor.fetchone()
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting setting: {e}")
            return None
        finally:
            conn.close()

    def set_setting(self, key, value):
        """Set a setting value."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
            """,
                (key, value, datetime.datetime.now(eastern).isoformat()),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Error setting setting: {e}")
        finally:
            conn.close()
