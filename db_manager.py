import aiosqlite
import logging

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path="verification_bot.db"):
        self.db_path = db_path

    async def init_db(self):
        """Initializes database tables if they do not exist."""
        async with aiosqlite.connect(self.db_path) as db:
            # Create groups table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    group_id INTEGER PRIMARY KEY,
                    owner_id INTEGER NOT NULL,
                    is_premium BOOLEAN DEFAULT 0,
                    timeout_seconds INTEGER DEFAULT 300,
                    video_prompt TEXT
                )
            """)
            
            # Create pending_users table (using compound key to support users joining multiple groups)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_users (
                    user_id INTEGER,
                    group_id INTEGER,
                    status TEXT NOT NULL,
                    join_time INTEGER NOT NULL,
                    PRIMARY KEY (user_id, group_id)
                )
            """)
            
            # Upgrade check: Alter groups to add video_prompt if it does not exist (backwards compatibility)
            try:
                await db.execute("ALTER TABLE groups ADD COLUMN video_prompt TEXT")
            except Exception:
                pass

            await db.commit()
        logger.info("Database tables initialized successfully.")

    # ----------------------------------------------------
    # GROUP MANAGEMENT METHODS
    # ----------------------------------------------------
    async def add_group(self, group_id: int, owner_id: int, is_premium: bool = False, timeout_seconds: int = 300, video_prompt: str = None):
        """Registers a new group in the database."""
        async with aiosqlite.connect(self.db_path) as db:
            # Insert or ignore to ensure we don't wipe custom prompt if registered again
            await db.execute(
                "INSERT OR IGNORE INTO groups (group_id, owner_id, is_premium, timeout_seconds, video_prompt) VALUES (?, ?, ?, ?, ?)",
                (group_id, owner_id, 1 if is_premium else 0, timeout_seconds, video_prompt)
            )
            # Update core params while preserving custom prompt
            await db.execute(
                "UPDATE groups SET owner_id = ?, is_premium = ?, timeout_seconds = ? WHERE group_id = ?",
                (owner_id, 1 if is_premium else 0, timeout_seconds, group_id)
            )
            await db.commit()
        logger.info(f"Group {group_id} registered/updated with owner {owner_id} (Premium: {is_premium}).")

    async def get_group(self, group_id: int):
        """Retrieves group details by group ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "group_id": row["group_id"],
                        "owner_id": row["owner_id"],
                        "is_premium": bool(row["is_premium"]),
                        "timeout_seconds": row["timeout_seconds"],
                        "video_prompt": row["video_prompt"]
                    }
                return None

    async def update_group_premium(self, group_id: int, is_premium: bool):
        """Updates the premium tier subscription status of a group."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE groups SET is_premium = ? WHERE group_id = ?",
                (1 if is_premium else 0, group_id)
            )
            await db.commit()
        logger.info(f"Group {group_id} premium status set to {is_premium}.")

    async def update_group_timeout(self, group_id: int, timeout_seconds: int):
        """Updates the custom timeout duration for user verification challenges."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE groups SET timeout_seconds = ? WHERE group_id = ?",
                (timeout_seconds, group_id)
            )
            await db.commit()
        logger.info(f"Group {group_id} verification timeout updated to {timeout_seconds}s.")

    async def update_group_video_prompt(self, group_id: int, video_prompt: str):
        """Updates the custom video verification prompt message for a group."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE groups SET video_prompt = ? WHERE group_id = ?",
                (video_prompt, group_id)
            )
            await db.commit()
        logger.info(f"Group {group_id} video prompt updated.")

    # ----------------------------------------------------
    # PENDING USERS MANAGEMENT METHODS
    # ----------------------------------------------------
    async def add_pending_user(self, user_id: int, group_id: int, status: str, join_time: int):
        """Adds a new pending user to the verification funnel."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO pending_users (user_id, group_id, status, join_time) VALUES (?, ?, ?, ?)",
                (user_id, group_id, status, join_time)
            )
            await db.commit()
        logger.info(f"User {user_id} added to pending queue for group {group_id} (Status: {status}).")

    async def get_pending_user(self, user_id: int, group_id: int):
        """Gets pending user credentials by user and group ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM pending_users WHERE user_id = ? AND group_id = ?", (user_id, group_id)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "user_id": row["user_id"],
                        "group_id": row["group_id"],
                        "status": row["status"],
                        "join_time": row["join_time"]
                    }
                return None

    async def get_pending_user_any_group(self, user_id: int):
        """Finds a pending user's active verification state across any group."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM pending_users WHERE user_id = ? ORDER BY join_time DESC LIMIT 1", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "user_id": row["user_id"],
                        "group_id": row["group_id"],
                        "status": row["status"],
                        "join_time": row["join_time"]
                    }
                return None

    async def update_pending_user_status(self, user_id: int, group_id: int, status: str):
        """Updates the verification status of a pending member."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE pending_users SET status = ? WHERE user_id = ? AND group_id = ?",
                (status, user_id, group_id)
            )
            await db.commit()
        logger.info(f"User {user_id} status in group {group_id} updated to {status}.")

    async def delete_pending_user(self, user_id: int, group_id: int):
        """Removes a user from the active verification queue."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM pending_users WHERE user_id = ? AND group_id = ?",
                (user_id, group_id)
            )
            await db.commit()
        logger.info(f"User {user_id} deleted from verification queue of group {group_id}.")
