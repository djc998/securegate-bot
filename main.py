import os
import random
import time
import logging
import aiosqlite
from dotenv import load_dotenv

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

from db_manager import DatabaseManager

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "WHITELABEL").upper()
TIME_LIMIT_SECONDS = int(os.getenv("TIME_LIMIT_SECONDS", "300"))
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")
PREMIUM_BYPASS_CODE = os.getenv("PREMIUM_BYPASS_CODE", "BYPASS123")

CLIENT_OWNER_ID = os.getenv("CLIENT_OWNER_ID")
CLIENT_GROUP_CHAT_ID = os.getenv("CLIENT_GROUP_CHAT_ID")

if CLIENT_OWNER_ID:
    CLIENT_OWNER_ID = int(CLIENT_OWNER_ID)
if CLIENT_GROUP_CHAT_ID:
    CLIENT_GROUP_CHAT_ID = int(CLIENT_GROUP_CHAT_ID)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize database manager
db = DatabaseManager()

# Global dictionary to track pending admin settings changes
# Key: user_id (admin ID), Value: {"group_id": group_id, "action": "awaiting_video_prompt"}
pending_admin_configs = {}

def is_group_premium(group: dict) -> bool:
    if group.get("is_premium"):
        return True
    trial_start = group.get("trial_started_at", 0)
    if trial_start > 0 and time.time() < trial_start + 604800:
        return True
    return False

# ----------------------------------------------------
# TIMEOUT KICK CALLBACK
# ----------------------------------------------------
async def kick_timeout_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    user_id = data["user_id"]
    group_id = data["group_id"]
    msg_id = data["msg_id"]
    username = data["username"]

    # Check if user is still pending
    pending = await db.get_pending_user(user_id, group_id)
    if pending:
        logger.info(f"User {user_id} timed out in group {group_id}. Kicking...")
        try:
            # Kick is ban followed by unban
            await context.bot.ban_chat_member(group_id, user_id)
            await context.bot.unban_chat_member(group_id, user_id)

            # Edit message in group
            await context.bot.edit_message_text(
                chat_id=group_id,
                message_id=msg_id,
                text=f"⏳ <b>Verification Timeout</b>: User @{username} failed to verify in time and has been kicked.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error kicking timed out user {user_id}: {e}")

        # Log timeout and delete pending entry
        duration = int(time.time()) - pending["join_time"]
        await db.log_verification(user_id, group_id, "timeout", duration, int(time.time()))
        await db.delete_pending_user(user_id, group_id)

# ----------------------------------------------------
# 1. CORE EVENTS & MIDDLEWARES
# ----------------------------------------------------

# chat member updated handler
async def on_chat_member_updated(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    if not chat_member:
        return

    group_id = chat_member.chat.id
    user_id = chat_member.new_chat_member.user.id
    username = chat_member.new_chat_member.user.username or chat_member.new_chat_member.user.first_name

    old_status = chat_member.old_chat_member.status
    new_status = chat_member.new_chat_member.status

    # We match standard user joining statuses
    is_joining = (new_status in ["member", "restricted"]) and (old_status in ["left", "kicked", "banned", None])
    if not is_joining:
        return

    # Skip bot joining
    if chat_member.new_chat_member.user.is_bot:
        return

    # Check group registration
    group = await db.get_group(group_id)
    if not group:
        if DEPLOYMENT_MODE == "WHITELABEL" and group_id == CLIENT_GROUP_CHAT_ID:
            # Dynamic setup for Whitelabel
            await db.add_group(CLIENT_GROUP_CHAT_ID, CLIENT_OWNER_ID, is_premium=True, timeout_seconds=TIME_LIMIT_SECONDS)
            group = await db.get_group(group_id)
        else:
            logger.info(f"Group {group_id} not registered in database. Skipping restriction.")
            return

    logger.info(f"User {user_id} (@{username}) joined group {group_id}. Restricting and challenging...")

    # Restrict permissions (Mute)
    mute_permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False
    )
    
    try:
        await context.bot.restrict_chat_member(group_id, user_id, permissions=mute_permissions)
    except Exception as e:
        logger.error(f"Failed to restrict member {user_id}: {e}")
        return

    # Generate math addition challenge
    num1 = random.randint(1, 9)
    num2 = random.randint(1, 9)
    correct_ans = num1 + num2

    options = [correct_ans]
    while len(options) < 4:
        wrong = random.randint(2, 18)
        if wrong not in options:
            options.append(wrong)
    random.shuffle(options)

    # Store status containing the answer: "pending_math:ANS"
    status_str = f"pending_math:{correct_ans}"
    await db.add_pending_user(user_id, group_id, status_str, int(time.time()), chat_member.new_chat_member.user.username)

    # Build inline keyboard buttons
    buttons = [[
        InlineKeyboardButton(str(opt), callback_data=f"math_{user_id}_{group_id}_{opt}") 
        for opt in options
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)

    # Prompt message in group
    user_mention = chat_member.new_chat_member.user.mention_html()
    msg = await context.bot.send_message(
        chat_id=group_id,
        text=f"🤖 <b>SecureGate Challenge</b>\n\nWelcome {user_mention}! To protect this space from bots, you must solve the mathematical puzzle below within <b>{group['timeout_seconds']} seconds</b> to be unmuted:\n\n💬 <code>{num1} + {num2} = ?</code>",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

    # Schedule Timeout Kick Job
    job_name = f"kick_{user_id}_{group_id}"
    job_data = {
        "user_id": user_id,
        "group_id": group_id,
        "msg_id": msg.message_id,
        "username": username
    }
    context.job_queue.run_once(
        kick_timeout_callback, 
        group["timeout_seconds"], 
        data=job_data, 
        name=job_name
    )

# ----------------------------------------------------
# 2. COGNITIVE MATH CHALLENGE CALLBACK
# ----------------------------------------------------
async def on_math_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    target_user_id = int(parts[1])
    target_group_id = int(parts[2])
    chosen_ans = int(parts[3])

    # Check if the user clicking is the one challenged
    if query.from_user.id != target_user_id:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⚠️ @{query.from_user.username or query.from_user.first_name}, this challenge is intended for another user.",
            reply_to_message_id=query.message.message_id
        )
        return

    # Fetch user pending status
    pending = await db.get_pending_user(target_user_id, target_group_id)
    if not pending or not pending["status"].startswith("pending_math"):
        return

    # Parse correct answer
    correct_ans = int(pending["status"].split(":")[1])

    # Cancel scheduled timeout job
    jobs = context.job_queue.get_jobs_by_name(f"kick_{target_user_id}_{target_group_id}")
    for job in jobs:
        job.schedule_removal()

    group = await db.get_group(target_group_id)

    if chosen_ans == correct_ans:
        # Correct answer!
        if is_group_premium(group):
            # Premium -> Proceed to Video verification Identity Gate
            await db.update_pending_user_status(target_user_id, target_group_id, "pending_video")
            
            # Reschedule timeout for video DM submission (allow them default timeout seconds again)
            job_name = f"kick_{target_user_id}_{target_group_id}"
            job_data = {
                "user_id": target_user_id,
                "group_id": target_group_id,
                "msg_id": query.message.message_id,
                "username": query.from_user.username or query.from_user.first_name
            }
            context.job_queue.run_once(
                kick_timeout_callback, 
                group["timeout_seconds"], 
                data=job_data, 
                name=job_name
            )

            # Edit group message to prompt video verification
            bot_info = await context.bot.get_me()
            await query.edit_message_text(
                text=f"✅ <b>Tier 1 Passed!</b>\n\nUser {query.from_user.mention_html()} passed the cognitive gate. However, this is a premium protected channel.\n\n📹 <b>Tier 2 Identity Verification</b>: Please click the button below to DM the bot with a brief video message containing yourself to complete verification.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔑 Video Verification Portal", url=f"https://t.me/{bot_info.username}?start=verify")
                ]]),
                parse_mode="HTML"
            )
        else:
            # Free group -> Unmute instantly and delete from pending queue
            await unmute_member(context.bot, target_group_id, target_user_id)
            duration = int(time.time()) - pending["join_time"]
            await db.log_verification(target_user_id, target_group_id, "success", duration, int(time.time()))
            await db.delete_pending_user(target_user_id, target_group_id)

            await query.edit_message_text(
                text=f"✅ <b>Cognitive Gate Solved!</b>\n\nWelcome {query.from_user.mention_html()}! You solved the math puzzle and have been unmuted.",
                parse_mode="HTML"
            )
    else:
        # Incorrect answer -> Kick user instantly
        logger.info(f"User {target_user_id} solved math puzzle incorrectly. Kicking...")
        try:
            await context.bot.ban_chat_member(target_group_id, target_user_id)
            await context.bot.unban_chat_member(target_group_id, target_user_id)
            
            await query.edit_message_text(
                text=f"❌ <b>Cognitive Gate Failed</b>: User {query.from_user.mention_html()} selected the wrong math solution and has been kicked."
            )
        except Exception as e:
            logger.error(f"Error kicking failed math user {target_user_id}: {e}")

        duration = int(time.time()) - pending["join_time"]
        await db.log_verification(target_user_id, target_group_id, "failed_math", duration, int(time.time()))
        await db.delete_pending_user(target_user_id, target_group_id)

# Helper function to unmute a member
async def unmute_member(bot, group_id: int, user_id: int):
    unmute_permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True
    )
    try:
        await bot.restrict_chat_member(group_id, user_id, permissions=unmute_permissions)
    except Exception as e:
        logger.error(f"Error unmuting user {user_id} in chat {group_id}: {e}")

# ----------------------------------------------------
# 3. IDENTITY GATE (VIDEO SUBMISSION) & DM HANDLER
# ----------------------------------------------------
async def on_private_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id
    
    # Locate user's active verification session
    pending = await db.get_pending_user_any_group(user_id)
    
    if not pending or pending["status"] != "pending_video":
        await message.reply_text("❌ No active video verification queue found for your account.")
        return

    group_id = pending["group_id"]
    group = await db.get_group(group_id)

    # Cancel/Pause timeout kick job
    jobs = context.job_queue.get_jobs_by_name(f"kick_{user_id}_{group_id}")
    for job in jobs:
        job.schedule_removal()

    # Update state to under_review
    await db.update_pending_user_status(user_id, group_id, "under_review")

    # Send video to admin
    owner_id = group["owner_id"]
    user_mention = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    admin_caption = (
        f"📹 <b>Identity Verification Submission</b>\n\n"
        f"New member {user_mention} (ID: <code>{user_id}</code>) submitted a verification video for your premium group: <code>{group_id}</code>.\n\n"
        f"Please review the video and select the outcome below:"
    )

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve Access ✅", callback_data=f"admin_approve_{user_id}_{group_id}"),
            InlineKeyboardButton("Reject & Kick ❌", callback_data=f"admin_reject_{user_id}_{group_id}")
        ]
    ])

    try:
        if message.video:
            await context.bot.send_video(
                chat_id=owner_id, 
                video=message.video.file_id, 
                caption=admin_caption, 
                reply_markup=markup,
                parse_mode="HTML"
            )
        elif message.video_note:
            # Video note notes do not support captions natively, so we send the file followed by description
            await context.bot.send_video_note(chat_id=owner_id, video_note=message.video_note.file_id)
            await context.bot.send_message(
                chat_id=owner_id, 
                text=admin_caption, 
                reply_markup=markup,
                parse_mode="HTML"
            )
        
        await message.reply_text("✅ <b>Video Received!</b>\n\nYour video has been securely forwarded to the group administrator. The countdown timer has been paused. You will be unmuted instantly upon review.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to forward verification video to admin {owner_id}: {e}")
        await message.reply_text("❌ An error occurred transmitting your video. Please contact a group administrator.")

# Text message handler in DMs for settings configuration
async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type != "private":
        return

    user_id = message.from_user.id

    if user_id in pending_admin_configs:
        config = pending_admin_configs[user_id]
        if config["action"] == "awaiting_video_prompt":
            group_id = config["group_id"]
            custom_text = message.text.strip()

            if custom_text == "/cancel":
                del pending_admin_configs[user_id]
                await message.reply_text("❌ Configuration changes cancelled.")
                # Render settings dashboard
                await render_settings_dashboard(message, context, group_id, edit=False)
                return

            # Update DB
            await db.update_group_video_prompt(group_id, custom_text)

            # Clear state
            del pending_admin_configs[user_id]

            await message.reply_text(
                "✅ <b>Success!</b>\n\nYour custom video verification instructions have been updated successfully.",
                parse_mode="HTML"
            )

            # Rerender settings dashboard
            await render_settings_dashboard(message, context, group_id, edit=False)
            return

# Start DM command catcher
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type != "private":
        return

    # Check if they sent /start verify
    args = context.args
    user_id = message.from_user.id
    
    if args and args[0] == "verify":
        pending = await db.get_pending_user_any_group(user_id)
        if pending and pending["status"] == "pending_video":
            group = await db.get_group(pending["group_id"])
            prompt = group["video_prompt"] if group and group["video_prompt"] else (
                "Please record or send a brief video message showing yourself (video note or video file). "
                "The bot will forward it to the channel owners to verify you are a genuine human."
            )
            await message.reply_text(
                f"📹 <b>SecureGate Video Gate Portal</b>\n\n{prompt}",
                parse_mode="HTML"
            )
            return
            
    await message.reply_text(
        "🔒 <b>Welcome to SecureGate Control Portal!</b>\n\n"
        "I protect Telegram groups from spammers. Add me to your group, elevate me to Administrator, and configure settings using command /settings in DM.",
        parse_mode="HTML"
    )

# ----------------------------------------------------
# 4. ADMINISTRATOR RESOLUTION CALLBACK
# ----------------------------------------------------
async def on_admin_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[1]
    target_user_id = int(parts[2])
    target_group_id = int(parts[3])

    group = await db.get_group(target_group_id)
    if not group:
        return

    # Check if the user executing the action is the group owner
    if query.from_user.id != group["owner_id"]:
        await query.message.reply_text("⚠️ This resolution action can only be completed by the registered group owner.")
        return

    pending = await db.get_pending_user(target_user_id, target_group_id)
    if not pending or pending["status"] != "under_review":
        await query.edit_message_text("⚠️ Verification session expired or already resolved.")
        return

    try:
        user_member = await context.bot.get_chat_member(target_group_id, target_user_id)
        username = user_member.user.username or user_member.user.first_name
        mention = user_member.user.mention_html()
    except Exception:
        username = f"User_{target_user_id}"
        mention = f"User (ID: {target_user_id})"

    if action == "approve":
        # UNMUTE & Clear queue
        await unmute_member(context.bot, target_group_id, target_user_id)
        duration = int(time.time()) - pending["join_time"]
        await db.log_verification(target_user_id, target_group_id, "success", duration, int(time.time()))
        await db.delete_pending_user(target_user_id, target_group_id)

        # Notify admin DM
        await query.edit_message_text(f"✅ Approved. {mention} unmuted in group.")

        # Notify user DM
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"🎉 <b>Access Approved!</b>\n\nThe administrator verified your video. You are now unmuted in group <code>{target_group_id}</code>.",
                parse_mode="HTML"
            )
        except Exception:
            pass
            
        # Send confirmation to group chat
        await context.bot.send_message(
            chat_id=target_group_id,
            text=f"✅ <b>Identity Verified!</b>\n\nAdmin approved user {mention} and they have been unmuted.",
            parse_mode="HTML"
        )
    elif action == "reject":
        # Kick (Ban and Unban) & Clear Queue
        try:
            await context.bot.ban_chat_member(target_group_id, target_user_id)
            await context.bot.unban_chat_member(target_group_id, target_user_id)
            
            # Notify user DM
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text="❌ <b>Access Rejected</b>\n\nYour video verification was rejected by the group administrator.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error kicking rejected user: {e}")

        duration = int(time.time()) - pending["join_time"]
        await db.log_verification(target_user_id, target_group_id, "rejected_video", duration, int(time.time()))
        await db.delete_pending_user(target_user_id, target_group_id)
        
        # Notify admin DM
        await query.edit_message_text(f"❌ Rejected. User @{username} has been kicked.")

# ----------------------------------------------------
# 5. PUBLIC SETUP & /PREMIUM (STRIPE BILLING) ROUTINES
# ----------------------------------------------------
async def setup_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type in ["private"]:
        await message.reply_text("❌ Setup must be initiated within the target Telegram group.")
        return

    if DEPLOYMENT_MODE != "PUBLIC":
        await message.reply_text("🛡️ This bot is whitelabeled. Custom setups are locked.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Verify sender is a group admin
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["administrator", "creator"]:
            await message.reply_text("❌ Only group administrators can run the /setup command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    # Verify bot is an admin
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status != "administrator":
            await message.reply_text("❌ Please elevate the bot to Administrator with group restriction rights first.")
            return
    except Exception as e:
        logger.error(f"Error checking bot status: {e}")
        return

    # Add to groups DB
    await db.add_group(chat_id, user_id, is_premium=False, timeout_seconds=TIME_LIMIT_SECONDS)
    
    await message.reply_text(
        "🛡️ <b>MySecureGate Setup Completed!</b>\n\n"
        "This group is now registered. I will intercept and challenge all new members joining.\n\n"
        "💡 <b>Admin Customization</b>:\n"
        "• Type /settings in my private DMs to configure verification timeouts.\n"
        "• Type /premium inside the group to unlock Tier-2 Video Verification.",
        parse_mode="HTML"
    )

async def verify_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type in ["private"]:
        await message.reply_text("❌ This command must be run within a group chat.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Verify sender is a group admin
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["administrator", "creator"]:
            await message.reply_text("❌ Only group administrators can use this command.")
            return
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return

    target_user_id = None
    target_mention = None

    # Check if there are arguments like /verify @username
    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("@"):
            username = arg[1:]
            pending = await db.get_pending_user_by_username(username, chat_id)
            if pending:
                target_user_id = pending["user_id"]
                target_mention = f"@{username}"
        
    # Check if there is a text_mention
    if not target_user_id:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user:
                target_user_id = entity.user.id
                target_mention = entity.user.first_name
                break

    # Check if replied to a user
    if not target_user_id and message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
        target_mention = f"@{message.reply_to_message.from_user.username}" if message.reply_to_message.from_user.username else message.reply_to_message.from_user.first_name

    if not target_user_id:
        await message.reply_text("❌ Could not identify user. Please either `/verify @username` (if they are pending) or reply to one of their messages with `/verify`.")
        return

    # UNMUTE & Clear queue
    await unmute_member(context.bot, chat_id, target_user_id)
    
    # Check if there is a pending user record to calculate duration
    pending = await db.get_pending_user(target_user_id, chat_id)
    if pending:
        duration = int(time.time()) - pending["join_time"]
        await db.log_verification(target_user_id, chat_id, "success_manual", duration, int(time.time()))
        
    await db.delete_pending_user(target_user_id, chat_id)

    # Cancel scheduled timeout job if exists
    jobs = context.job_queue.get_jobs_by_name(f"kick_{target_user_id}_{chat_id}")
    for job in jobs:
        job.schedule_removal()

    # Send confirmation to group chat
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ <b>Manually Verified!</b>\n\nAdmin manually approved user {target_mention} and they have been unmuted.",
        parse_mode="HTML"
    )

async def premium_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type in ["private"]:
        await message.reply_text("❌ Premium commands must be run within your registered group chat.")
        return

    if DEPLOYMENT_MODE != "PUBLIC":
        await message.reply_text("🛡️ This bot is a whitelabel deployment. Premium is pre-activated.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    group = await db.get_group(chat_id)
    if not group:
        await message.reply_text("❌ Please run /setup first to register this group in the database.")
        return

    if group["owner_id"] != user_id:
        await message.reply_text("❌ Only the registered group owner can unlock premium tiers.")
        return

    if group["is_premium"]:
        await message.reply_text("🌟 Premium is already fully unlocked for this group!")
        return

    # Check for bypass promo code argument
    if context.args:
        input_code = context.args[0].strip()
        if input_code.upper() == PREMIUM_BYPASS_CODE.upper():
            await db.update_group_premium(chat_id, is_premium=True)
            await message.reply_text(
                "🎉 <b>Bypass Code Activated!</b>\n\n"
                "Premium subscription tier has been successfully unlocked for this group for free! "
                "Identity Gate (Video Verification) and customization dashboards are now active.",
                parse_mode="HTML"
            )
            return
        else:
            await message.reply_text("❌ The entered premium activation code is invalid.")
            return

    trial_start = group.get("trial_started_at", 0)
    
    if trial_start > 0 and time.time() < trial_start + 604800:
        await message.reply_text("🎁 Your 7-Day Free Trial is currently active! You can still purchase a lifetime upgrade below.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Buy Premium (Stars) 🌟", callback_data=f"buy_premium_{chat_id}")]]), parse_mode="HTML")
        return
        
    if not group.get("trial_used"):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start 7-Day Free Trial 🎁", callback_data=f"start_trial_{chat_id}")],
            [InlineKeyboardButton("Buy Premium (Stars) 🌟", callback_data=f"buy_premium_{chat_id}")]
        ])
        await message.reply_text(
            "🌟 <b>Upgrade to SecureGate Premium</b>\n\n"
            "Unlock Tier-2 Video Identity Verification and advanced settings for your channel.\n\n"
            "You have a one-time 7-day free trial available. Would you like to start it now, or purchase a lifetime upgrade?",
            reply_markup=markup,
            parse_mode="HTML"
        )
        return

    # Send Native Telegram Stars Invoice
    await send_premium_invoice(context, user_id, chat_id, message)

async def send_premium_invoice(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, reply_to_message=None):
    title = "MySecureGate Premium Channel Upgrade"
    description = "Unlocks Tier-2 Video Identity Verification checks and settings dashboards for your channel."
    payload = f"premium_upgrade_{chat_id}"
    currency = "XTR"  # XTR is the currency code for Telegram Stars
    stars_amount = 250  # Charge 250 Telegram Stars
    prices = [LabeledPrice("Premium Upgrade (Stars)", stars_amount)]

    try:
        # We send it to their DMs to protect pricing and invoices from group chats
        await context.bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",  # Must be empty for Telegram Stars!
            currency=currency,
            prices=prices,
            start_parameter="premium-stars-upgrade"
        )
        if reply_to_message:
            await reply_to_message.reply_text("📬 <b>Invoice Sent!</b>\n\nI have sent a secure Telegram Stars invoice to your private DMs. Click it to complete checkout with Stars.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error dispatching Stars invoice: {e}")
        if reply_to_message:
            await reply_to_message.reply_text("❌ Could not dispatch payment details. Please check if you have started the bot in DMs.")

async def on_premium_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[0] + "_" + parts[1] # "start_trial" or "buy_premium"
    group_id = int(parts[2])

    group = await db.get_group(group_id)
    if not group:
        return
        
    if query.from_user.id != group["owner_id"]:
        await query.message.reply_text("❌ Only the registered group owner can unlock premium tiers.")
        return

    if action == "start_trial":
        if group.get("trial_used"):
            await query.edit_message_text("❌ You have already used your free trial for this group.")
            return
        
        await db.start_group_trial(group_id, int(time.time()))
        await query.edit_message_text(
            "🎉 <b>7-Day Free Trial Activated!</b>\n\n"
            "Premium features (Tier-2 Video Verification and custom settings) are now unlocked for 7 days. "
            "You will be invoiced after the trial expires.",
            parse_mode="HTML"
        )
    elif action == "buy_premium":
        await send_premium_invoice(context, query.from_user.id, group_id, query.message)

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("premium_upgrade_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="SecureGate billing matching error.")

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    group_id = int(payload.split("_")[2])

    # Unlock Tier-2
    await db.update_group_premium(group_id, is_premium=True)
    
    await update.message.reply_text(
        "🌟 <b>Upgrade Successful!</b>\n\n"
        "Thank you! Premium subscription tier has been fully unlocked. "
        "Identity Gate (Video Verification) is now active for your channel.",
        parse_mode="HTML"
    )

# ----------------------------------------------------
# 6. SETTINGS PANEL (ADMIN DM INTERFACES)
# ----------------------------------------------------
async def settings_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type != "private":
        await message.reply_text("❌ The settings dashboard can only be accessed privately in DMs (@MySecureGateBot).")
        return

    user_id = message.from_user.id
    
    # We retrieve all registered groups for this owner
    # For a simple local setup, let's fetch groups from database
    groups = []
    
    if DEPLOYMENT_MODE == "WHITELABEL":
        if user_id == CLIENT_OWNER_ID:
            group = await db.get_group(CLIENT_GROUP_CHAT_ID)
            if group:
                groups.append(group)
    else:
        # Public Mode: search sqlite database for all groups owned by user_id
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM groups WHERE owner_id = ?", (user_id,)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    groups.append({
                        "group_id": row["group_id"],
                        "owner_id": row["owner_id"],
                        "is_premium": bool(row["is_premium"]),
                        "timeout_seconds": row["timeout_seconds"]
                    })

    if not groups:
        await message.reply_text("❌ You do not own any registered groups. Add me to a group and run /setup inside the group chat.")
        return

    # If owner has multiple groups, show selection menu first
    if len(groups) > 1:
        text = "🛡️ <b>MySecureGate Settings Control</b>\n\nYou manage multiple registered groups. Please select which group you would like to customize below:"
        buttons = []
        for g in groups:
            gid = g['group_id']
            try:
                chat = await context.bot.get_chat(gid)
                chat_title = chat.title
            except Exception:
                chat_title = "Unknown Group"
            buttons.append([InlineKeyboardButton(f"👥 {chat_title} (ID: {gid})", callback_data=f"set_time_{gid}_show")])
        markup = InlineKeyboardMarkup(buttons)
        await message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        return

    # Render settings list directly if they only have one group
    await render_settings_dashboard(message, context, groups[0]["group_id"], edit=False)

async def render_settings_dashboard(message_object, context: ContextTypes.DEFAULT_TYPE, group_id: int, edit: bool = False):
    group = await db.get_group(group_id)
    if not group:
        return

    try:
        chat = await context.bot.get_chat(group_id)
        chat_title = chat.title
    except Exception:
        chat_title = "Unknown Group"

    tier_status = "🆓 Free Plan (Math Challenge Only)"
    if group["is_premium"]:
        tier_status = "🌟 Premium (Tier 2 Active)"
    elif is_group_premium(group):
        tier_status = "🎁 7-Day Free Trial (Tier 2 Active)"

    custom_prompt = group['video_prompt'] if group['video_prompt'] else "[Default Standard Instructions]"
    text = (
        f"⚙️ <b>MySecureGate Settings Control</b>\n\n"
        f"Group: <b>{chat_title}</b> <code>({group_id})</code>\n"
        f"Tier Status: {tier_status}\n"
        f"Verification Timeout: <b>{group['timeout_seconds']} seconds</b>\n"
        f"Custom Video Prompt: <code>{custom_prompt}</code>\n\n"
        f"Adjust the settings below:"
    )

    buttons = [
        [
            InlineKeyboardButton("-30 Seconds ⬇️", callback_data=f"set_time_{group_id}_minus30"),
            InlineKeyboardButton("+30 Seconds ⬆️", callback_data=f"set_time_{group_id}_plus30")
        ],
        [
            InlineKeyboardButton("Set to 2 Minutes ⏱️", callback_data=f"set_time_{group_id}_120"),
            InlineKeyboardButton("Set to 5 Minutes ⏱️", callback_data=f"set_time_{group_id}_300")
        ],
        [
            InlineKeyboardButton("Set to 15 Minutes ⏱️", callback_data=f"set_time_{group_id}_900"),
            InlineKeyboardButton("Set to 30 Minutes ⏱️", callback_data=f"set_time_{group_id}_1800")
        ],
        [
            InlineKeyboardButton("Set to 1 Hour ⏱️", callback_data=f"set_time_{group_id}_3600")
        ]
    ]

    if is_group_premium(group):
        buttons.append([
            InlineKeyboardButton("Edit Video Prompt ✏️", callback_data=f"set_time_{group_id}_edit")
        ])
    else:
        buttons.append([
            InlineKeyboardButton("Unlock Premium Video Gate 🌟", callback_data=f"set_time_{group_id}_unlock")
        ])

    # Check if this owner has multiple groups to render "Back to selector" button
    owner_id = group["owner_id"]
    owner_groups = []
    async with aiosqlite.connect(db.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT group_id FROM groups WHERE owner_id = ?", (owner_id,)) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                owner_groups.append(r["group_id"])

    if len(owner_groups) > 1:
        buttons.append([
            InlineKeyboardButton("⬅️ Select Another Group", callback_data=f"set_time_{group_id}_list")
        ])

    markup = InlineKeyboardMarkup(buttons)

    if edit:
        await message_object.edit_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await message_object.reply_text(text, reply_markup=markup, parse_mode="HTML")

async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    group_id = int(parts[2])
    action = parts[3]

    # Quick selector list bypass
    if action == "list":
        groups = []
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT group_id FROM groups WHERE owner_id = ?", (query.from_user.id,)) as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    groups.append(row["group_id"])
                    
        text = "🛡️ <b>MySecureGate Settings Control</b>\n\nYou manage multiple registered groups. Please select which group you would like to customize below:"
        buttons = []
        for gid in groups:
            try:
                chat = await context.bot.get_chat(gid)
                chat_title = chat.title
            except Exception:
                chat_title = "Unknown Group"
            buttons.append([InlineKeyboardButton(f"👥 {chat_title} (ID: {gid})", callback_data=f"set_time_{gid}_show")])
        markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        return

    group = await db.get_group(group_id)
    if not group:
        return

    if query.from_user.id != group["owner_id"]:
        return

    current_timeout = group["timeout_seconds"]
    
    if action == "minus30":
        new_timeout = max(30, current_timeout - 30)
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "plus30":
        new_timeout = min(3600, current_timeout + 30)
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "120":
        new_timeout = 120
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "300":
        new_timeout = 300
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "900":
        new_timeout = 900
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "1800":
        new_timeout = 1800
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "3600":
        new_timeout = 3600
        if new_timeout != current_timeout:
            await db.update_group_timeout(group_id, new_timeout)
            await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "show":
        await render_settings_dashboard(query.message, context, group_id, edit=True)
    elif action == "edit":
        # Awaiting custom prompt input
        pending_admin_configs[query.from_user.id] = {"group_id": group_id, "action": "awaiting_video_prompt"}
        await query.edit_message_text(
            text=(
                f"✏️ <b>Edit Custom Video Verification Prompt</b>\n\n"
                f"Group: <code>{group_id}</code>\n\n"
                f"Please type and send your custom verification instructions in your next message. "
                f"Your prompt will be presented to joining users when they are directed to DM the bot with a video.\n\n"
                f"<i>Example: 'Please say your first name and your favorite color to verify.'</i>\n\n"
                f"To cancel and return to settings, type `/cancel`."
            ),
            parse_mode="HTML"
        )
    elif action == "unlock":
        await query.edit_message_text(
            text=(
                f"🌟 <b>Unlock SecureGate Premium</b>\n\n"
                f"Unlocking premium activates Tier-2 Video Gating and settings dashboards for your channel.\n\n"
                f"👉 <b>How to Upgrade</b>: Go back to your group chat and type the command **`/premium`**. "
                f"I will private message you a secure checkout link using Telegram Stars!"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Settings", callback_data=f"set_time_{group_id}_back")
            ]])
        )
    elif action == "back":
        await render_settings_dashboard(query.message, context, group_id, edit=True)

# ----------------------------------------------------
# 7. BOT CREATOR REPORTING DASHBOARD
# ----------------------------------------------------
async def botstats_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message.chat.type != "private":
        await message.reply_text("❌ The stats dashboard can only be accessed privately in DMs.")
        return

    user_id = message.from_user.id
    if CLIENT_OWNER_ID and user_id != CLIENT_OWNER_ID:
        await message.reply_text("❌ Unauthorized. Only the configured Bot Creator can view global statistics.")
        return

    # Fetch stats
    stats = await db.get_bot_statistics()
    
    report_text = (
        "📊 <b>SecureGate Global Statistics</b>\n\n"
        "🏢 <b>Group Usage</b>\n"
        f"• Total Groups: <code>{stats['total_groups']}</code>\n"
        f"• Premium Channels: <code>{stats['premium_groups']}</code>\n"
        f"• Active Free Trials: <code>{stats['trial_groups']}</code>\n"
        f"• Free Channels: <code>{stats['free_groups']}</code>\n\n"
        "🛡️ <b>Verification Metrics</b>\n"
        f"• Total Challenges: <code>{stats['total_verifications']}</code>\n"
        f"• Passed (Unmuted): <code>{stats['success_verifications']}</code>\n"
        f"• Failed/Timeout/Rejected: <code>{stats['failed_verifications']}</code>\n"
        f"• Avg Completion Time: <code>{stats['avg_duration'] // 60}m {stats['avg_duration'] % 60}s</code>"
    )
    
    await message.reply_text(report_text, parse_mode="HTML")

# ----------------------------------------------------
# MAIN INITIALIZER
# ----------------------------------------------------
async def check_trials_job(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Checking for expired premium trials...")
    expired = await db.get_expired_uninvoiced_trials(int(time.time()))
    for group in expired:
        group_id = group["group_id"]
        owner_id = group["owner_id"]
        logger.info(f"Trial expired for group {group_id}. Sending invoice to owner {owner_id}.")
        
        try:
            # Send message first
            await context.bot.send_message(
                chat_id=owner_id,
                text=f"⚠️ <b>Premium Trial Expired</b>\n\nYour 7-day free trial for group <code>{group_id}</code> has expired. Premium features have been paused.\n\nPlease pay the invoice below to permanently upgrade and restore Tier-2 Video Verification.",
                parse_mode="HTML"
            )
            # Send invoice
            await send_premium_invoice(context, owner_id, group_id)
            await db.mark_trial_invoiced(group_id)
        except Exception as e:
            logger.error(f"Failed to send expiration invoice to {owner_id} for group {group_id}: {e}")

async def post_init_callback(application):
    # Initialize DB
    await db.init_db()

    # Pre-register whitelabel if applicable
    if DEPLOYMENT_MODE == "WHITELABEL":
        if CLIENT_GROUP_CHAT_ID and CLIENT_OWNER_ID:
            await db.add_group(CLIENT_GROUP_CHAT_ID, CLIENT_OWNER_ID, is_premium=True, timeout_seconds=TIME_LIMIT_SECONDS)
            logger.info(f"WHITELABEL MODE ACTIVE: hardcoded group {CLIENT_GROUP_CHAT_ID} registered to owner {CLIENT_OWNER_ID}.")

    # Start periodic trial expiration check (every hour = 3600 seconds)
    application.job_queue.run_repeating(check_trials_job, interval=3600, first=10)

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing from your environment variables!")
        return

    logger.info("Initializing SecureGate Bot application...")
    
    # Enable Job Queue
    application = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init_callback).build()

    # Handlers Configuration
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("setup", setup_command_handler))
    application.add_handler(CommandHandler("premium", premium_command_handler))
    application.add_handler(CommandHandler("settings", settings_command_handler))
    application.add_handler(CommandHandler("verify", verify_command_handler))
    application.add_handler(CommandHandler("botstats", botstats_command_handler))

    # Math buttons query response
    application.add_handler(CallbackQueryHandler(on_math_callback, pattern="^math_"))
    
    # Video note/Video verification responses in private chat DMs
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.VIDEO | filters.VIDEO_NOTE), 
        on_private_video
    ))

    # Text configuration handlers for administrators in private DMs
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        on_private_text
    ))

    # Admin verification decisions Callback Query
    application.add_handler(CallbackQueryHandler(on_admin_decision_callback, pattern="^admin_"))

    # Admin timeout configurations Callback Query
    application.add_handler(CallbackQueryHandler(on_settings_callback, pattern="^set_time_"))

    # Premium Trial and Buy buttons
    application.add_handler(CallbackQueryHandler(on_premium_buttons_callback, pattern="^(start_trial|buy_premium)_"))

    # Stripe Billing handlers
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # Intercept new members joining groups
    application.add_handler(ChatMemberHandler(on_chat_member_updated, ChatMemberHandler.CHAT_MEMBER))

    # Run Bot Polling
    logger.info("SecureGate bot starting polling...")
    application.run_polling(allowed_updates=["message", "callback_query", "chat_member", "pre_checkout_query"])

if __name__ == "__main__":
    main()
