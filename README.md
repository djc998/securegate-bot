# SecureGate Telegram SaaS Bot 🔒

SecureGate is a production-ready, asynchronous Python Telegram bot designed to shield group chats from automated spam, user floods, and bots. It supports two modes of operation:
1. **Dynamic Public Mode**: A multi-tenant SaaS service utilizing native Telegram billing invoices (via Stripe) for premium Tier-2 identity upgrades.
2. **Whitelabel Mode**: Dedicated single-tenant client deployments locked statically to specific groups and owners.

It incorporates a robust **two-tier verification funnel** featuring a mathematical cognitive gate (Tier 1) followed by an identity review video submission gate (Tier 2).

---

## 💎 Features

* **Dual Deployment Mode**: Switch between a public premium SaaS and single-tenant whitelabel setups easily via `.env`.
* **Two-Tier Verification Funnel**:
  * **Tier 1 (Cognitive Gate)**: Intercepts joins, strips sending permissions (mutes), sends a randomized addition problem with inline option buttons, and schedules automated timed kicks.
  * **Tier 2 (Identity Video - Premium)**: Prompts cognitive gate graduates to DM the bot with a brief video. The bot pauses the timeout timer, forwards the video message to the group owner's private inbox with inline **Approve** and **Reject** action buttons, and resolves member permissions dynamically.
* **Inline Settings Dashboard**: Dynamic `/settings` dashboard in admin DMs to manage verification timeout durations with intuitive toggles.
* **Native Stripe Integration**: Integrates Telegram's native invoice APIs for premium subscription payments in public mode.

---

## 🛠️ Tech Stack

* **Language**: Python 3.10+
* **Framework**: `python-telegram-bot` (v20.0+) with `JobQueue` (APScheduler)
* **Database**: `aiosqlite` (asynchronous file-based SQLite database)
* **Containerization**: Docker & Docker Compose

---

## 🚀 Installation & Local Running

### 1. Configure the Environment
Create your `.env` file (copied from `.env.example`):
```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
DEPLOYMENT_MODE=WHITELABEL  # PUBLIC or WHITELABEL
TIME_LIMIT_SECONDS=300

# Stripe billing token (Required for PUBLIC mode payments)
PAYMENT_PROVIDER_TOKEN=YOUR_STRIPE_PROVIDER_TOKEN

# Whitelabel parameters (Used only when DEPLOYMENT_MODE=WHITELABEL)
CLIENT_OWNER_ID=YOUR_TELEGRAM_USER_ID
CLIENT_GROUP_CHAT_ID=-100YOUR_GROUP_CHAT_ID
```

### 2. Run Locally with Virtual Env
Ensure Python 3.10+ is installed. Set up your environment:

```bash
# Initialize and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the application
python main.py
```

### 3. Run with Docker Compose
To run persistence-mapped containers out-of-the-box:

```bash
# Build and boot container in detached mode
docker-compose up --build -d

# Check live logs
docker-compose logs -f
```

---

## 🕹️ Testing Workflows

### Setup Phase (Public Mode Only)
1. Add the bot to your Telegram group and promote it to **Administrator** with permission to **Restrict Members**.
2. Type `/setup` in the group chat. The bot registers the group and you as the owner.
3. To test payments, type `/premium` inside the group chat. The bot will DM you a native Stripe checkout invoice for `$9.99`. Upon successful test payment, premium features unlock!

### Verification Funnel Verification
1. Join the registered group using a test account.
2. The bot immediately restricts your permission levels (you will see "Sending messages is not allowed") and posts a calculation challenge card (e.g. `4 + 5 = ?`).
3. **Fail Case**: Click a wrong answer, or wait for the timeout limit (e.g. 5 minutes). The bot will ban and instantly unban your account (kicking you from the group) and update the challenge text.
4. **Pass Case (Free Group)**: Click the correct option. The bot immediately restores your sending permissions and welcomes you.
5. **Pass Case (Premium Group)**: Click the correct option. The bot transitions to **Tier-2 Video Verification** and asks you to click the link to DM it a video message.
   * Send the bot a video or video note in private chat DMs.
   * The countdown is paused. The bot forwards the video to the administrator's DM.
   * Admin reviews the video and clicks **Approve Access** or **Reject & Kick**.
   * User is unmuted or kicked instantly depending on the verdict!
