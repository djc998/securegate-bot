📄 Master Specification: SecureGate Telegram SaaS Bot
1. Project Overview
SecureGate is a Python-based, fully containerized Telegram bot designed to protect group chats from automated spam and unverified accounts. It operates as a SaaS (Software-as-a-Service) product, utilizing a two-tier verification system. It features a dual-mode deployment system: a dynamic multi-tenant public bot (freemium/premium via Stripe) and a statically configured white-label deployment for private clients.

2. Technical Stack
Language: Python 3.10+

Core Framework: python-telegram-bot (v20.0+) with JobQueue

Database: aiosqlite (Asynchronous SQLite)

Configuration: python-dotenv

Containerization: Docker & Docker Compose

3. File & Directory Structure
Plaintext
securegate-bot/
├── .env                  # Configuration and deployment mode toggles
├── requirements.txt      # Python dependencies
├── Dockerfile            # Container build instructions
├── docker-compose.yml    # Orchestration and volume mapping for SQLite
├── db_manager.py         # Asynchronous database logic
└── main.py               # Core application, Telegram handlers, and workflows
4. Environment Configuration (.env)
The application must dynamically adapt its behavior based on the .env file:

BOT_TOKEN: The Telegram API token.

TIME_LIMIT_SECONDS: Global timeout for user verification (Default: 300).

PAYMENT_PROVIDER_TOKEN: Stripe provider token for Telegram's native invoice API.

DEPLOYMENT_MODE:

If "PUBLIC": The bot enables the /setup command, requires group owners to register, and enforces the Stripe paywall for Tier-2 features.

If "WHITELABEL": The bot disables /setup and the Stripe paywall, hardcoding its routing to CLIENT_OWNER_ID and CLIENT_GROUP_CHAT_ID.

5. Database Schema (verification_bot.db)
Table: groups

group_id (INTEGER, Primary Key): The Telegram chat ID.

owner_id (INTEGER): The Telegram user ID of the admin.

is_premium (BOOLEAN): Defaults to False. If True, unlocks Tier-2 video verification.

timeout_seconds (INTEGER): Custom timeout duration (Default: 300).

Table: pending_users

user_id (INTEGER, Primary Key): ID of the new member.

group_id (INTEGER): ID of the chat they joined.

status (TEXT): State tracking (pending_math, pending_video, under_review).

join_time (INTEGER): Unix timestamp for background job timeout calculations.

6. Core Application Workflows
A. The Setup Phase (Public Mode Only)
Admin adds bot to a group and types /setup.

Bot verifies the user is a group admin.

Bot registers the group_id and owner_id into the groups database table.

B. The Verification Funnel
Tier 1 (Cognitive Gate): New user joins. Bot triggers restrict_and_challenge(). The user's permissions are stripped (muted). Bot generates a randomized math addition problem (e.g., "3 + 4") with 4 inline buttons. A JobQueue task is scheduled to kick the user if timeout_seconds is reached.

Tier 2 Route Check: User clicks the correct answer.

If Group is Free: Bot restores user permissions, sends a success message, and deletes the user from the pending_users table.

If Group is Premium: Status updates to pending_video. Bot instructs the user to DM it with a video.

Identity Gate (Video): User DMs a video to the bot. Bot pauses the timeout timer (status: under_review). Bot looks up the owner_id for that user's group_id and forwards the video to the owner's DMs with inline Approve and Reject buttons.

Admin Resolution: * Approve: Bot un-mutes the user in the main group and deletes them from the queue.

Reject: Bot bans, instantly unbans (kick), and deletes from the queue.

C. Monetization & Admin Dashboard
/premium: Sends a native Telegram Invoice (via Stripe) to the group owner for $9.99. On successful payment, the database updates is_premium to True.

/settings: Spawns an inline-button dashboard in the admin's DMs allowing them to dynamically change timeout_seconds (e.g., 2 mins vs. 5 mins) and view their Premium status.

7. Deployment Instructions (Docker)
Build requires a python:3.10-slim base image.

Dependencies installed via pip install --no-cache-dir -r requirements.txt.

docker-compose.yml must map a local volume ./verification_bot.db:/app/verification_bot.db to prevent data loss upon container restart.