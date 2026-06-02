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
                    video_prompt TEXT,
                    trial_started_at INTEGER DEFAULT 0,
                    trial_invoiced BOOLEAN DEFAULT 0,
                    trial_used BOOLEAN DEFAULT 0
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
            
            # Create verification_logs table for bot creator reporting
            await db.execute("""
                CREATE TABLE IF NOT EXISTS verification_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    group_id INTEGER,
                    status TEXT NOT NULL,
                    duration_seconds INTEGER,
                    timestamp INTEGER NOT NULL
                )
            """)
            
            # Upgrade check: Alter groups to add video_prompt if it does not exist (backwards compatibility)
            try:
                await db.execute("ALTER TABLE groups ADD COLUMN video_prompt TEXT")
            except Exception:
                pass

            # Upgrade check: Alter pending_users to add username if it does not exist
            try:
                await db.execute("ALTER TABLE pending_users ADD COLUMN username TEXT")
            except Exception:
                pass

            # Upgrade check: Alter groups to add trial columns (backwards compatibility)
            for col, col_type in [("trial_started_at", "INTEGER DEFAULT 0"), 
                                  ("trial_invoiced", "BOOLEAN DEFAULT 0"), 
                                  ("trial_used", "BOOLEAN DEFAULT 0")]:
                try:
                    await db.execute(f"ALTER TABLE groups ADD COLUMN {col} {col_type}")
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
                        "video_prompt": row["video_prompt"],
                        "trial_started_at": row["trial_started_at"] if "trial_started_at" in row.keys() else 0,
                        "trial_invoiced": bool(row["trial_invoiced"]) if "trial_invoiced" in row.keys() else False,
                        "trial_used": bool(row["trial_used"]) if "trial_used" in row.keys() else False
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

    async def start_group_trial(self, group_id: int, start_time: int):
        """Starts a premium trial for a group."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE groups SET trial_started_at = ?, trial_used = 1 WHERE group_id = ?",
                (start_time, group_id)
            )
            await db.commit()
        logger.info(f"Group {group_id} started 7-day premium trial.")

    async def mark_trial_invoiced(self, group_id: int):
        """Marks a group's trial as having been invoiced."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE groups SET trial_invoiced = 1 WHERE group_id = ?",
                (group_id,)
            )
            await db.commit()
        logger.info(f"Group {group_id} premium trial marked as invoiced.")

    async def get_expired_uninvoiced_trials(self, current_time: int):
        """Retrieves groups whose trials have expired and have not been invoiced yet."""
        # 7 days = 604800 seconds
        expiration_threshold = current_time - 604800
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM groups WHERE trial_started_at > 0 AND trial_started_at < ? AND trial_invoiced = 0 AND is_premium = 0", 
                (expiration_threshold,)
            ) as cursor:
                rows = await cursor.fetchall()
                results = []
                for row in rows:
                    results.append({
                        "group_id": row["group_id"],
                        "owner_id": row["owner_id"],
                        "trial_started_at": row["trial_started_at"]
                    })
                return results

    # ----------------------------------------------------
    # PENDING USERS MANAGEMENT METHODS
    # ----------------------------------------------------
    async def add_pending_user(self, user_id: int, group_id: int, status: str, join_time: int, username: str = None):
        """Adds a new pending user to the verification funnel."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO pending_users (user_id, group_id, status, join_time, username) VALUES (?, ?, ?, ?, ?)",
                (user_id, group_id, status, join_time, username)
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

    async def get_pending_user_by_username(self, username: str, group_id: int):
        """Gets pending user credentials by username and group ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM pending_users WHERE LOWER(username) = LOWER(?) AND group_id = ?", (username, group_id)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "user_id": row["user_id"],
                        "group_id": row["group_id"],
                        "status": row["status"],
                        "join_time": row["join_time"],
                        "username": row["username"]
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

    # ----------------------------------------------------
    # REPORTING & STATISTICS METHODS
    # ----------------------------------------------------
    async def log_verification(self, user_id: int, group_id: int, status: str, duration_seconds: int, timestamp: int):
        """Logs the conclusion of a verification session."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO verification_logs (user_id, group_id, status, duration_seconds, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, group_id, status, duration_seconds, timestamp)
            )
            await db.commit()
        logger.info(f"Logged verification for user {user_id} in group {group_id}: {status} ({duration_seconds}s)")

    async def get_bot_statistics(self):
        """Aggregates all bot statistics for the creator report."""
        import time
        current_time = int(time.time())
        stats = {
            "total_groups": 0,
            "premium_groups": 0,
            "trial_groups": 0,
            "free_groups": 0,
            "total_verifications": 0,
            "success_verifications": 0,
            "failed_verifications": 0,
            "avg_duration": 0
        }
        
        async with aiosqlite.connect(self.db_path) as db:
            # 1. Group Stats
            async with db.execute("SELECT is_premium, trial_started_at FROM groups") as cursor:
                rows = await cursor.fetchall()
                stats["total_groups"] = len(rows)
                for row in rows:
                    if row[0]: # is_premium
                        stats["premium_groups"] += 1
                    else:
                        trial_start = row[1]
                        if trial_start > 0 and current_time < trial_start + 604800:
                            stats["trial_groups"] += 1
                        else:
                            stats["free_groups"] += 1
            
            # 2. Verification Stats
            async with db.execute("SELECT status, duration_seconds FROM verification_logs") as cursor:
                logs = await cursor.fetchall()
                stats["total_verifications"] = len(logs)
                success_count = 0
                total_duration = 0
                for row in logs:
                    if row[0] == "success":
                        stats["success_verifications"] += 1
                        success_count += 1
                        total_duration += row[1]
                    else:
                        stats["failed_verifications"] += 1
                        
                if success_count > 0:
                    stats["avg_duration"] = total_duration // success_count
                    
        return stats
