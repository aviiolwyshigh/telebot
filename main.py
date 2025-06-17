import os
import logging
import string
import random
from datetime import datetime, timedelta
import threading
from flask import Flask, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app setup
app = Flask(__name__)

# Bot token from env
TOKEN = os.getenv("BOT_TOKEN")

# Database channel ID - replace with your database channel ID
DATABASE_CHANNEL_ID = -1002678155201  # Replace with your actual channel ID

# Track users who have previously joined the backup channel
channel_joined_users = set()

# Storage for message IDs in database channel (code -> message_id mapping)
channel_message_storage = {}

# Backup channel username (without @)
BACKUP_CHANNEL = "baapBolbey"  # Replace with your backup channel username

# Admin user ID - replace with your actual admin user ID
ADMIN_USER_ID = 1524529804  # Replace with your Telegram user ID


# Flask routes
@app.route('/')
def home():
    return jsonify({
        "status": "Bot is active",
        "bot_name": "Media Share Bot",
        "timestamp": datetime.utcnow().isoformat(),
        "backup_channel": f"@{BACKUP_CHANNEL}",
        "active_users": len(channel_joined_users)
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.utcnow().isoformat()})

@app.route('/stats')
def stats():
    return jsonify({
        "channel_members": len(channel_joined_users),
        "stored_codes": len(channel_message_storage),
        "backup_channel": BACKUP_CHANNEL,
        "database_channel_id": DATABASE_CHANNEL_ID
    })

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

def generate_code(length=6):
    return ''.join(
        random.choices(string.ascii_lowercase + string.digits, k=length))


async def save_media_to_channel(context,
                                file_id,
                                media_type,
                                minutes=2880,
                                files_data=None):
    """Save media to database channel and return access code"""
    code = generate_code()
    expires_at = datetime.utcnow() + timedelta(minutes=minutes)

    # Create metadata
    if files_data:  # Batch upload
        metadata = {
            "code": code,
            "type": "batch",
            "files": files_data,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }
        caption = f"🔗 Batch Media\nCode: {code}\nFiles: {len(files_data)}\nExpires: {expires_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    else:  # Single file
        metadata = {
            "code": code,
            "type": "single",
            "file_id": file_id,
            "media_type": media_type,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }
        caption = f"🔗 Media Link\nCode: {code}\nType: {media_type}\nExpires: {expires_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"

    try:
        # Send metadata to database channel
        if files_data:
            # For batch, send first file with metadata
            first_file_id, first_media_type = files_data[0]
            if first_media_type == "photo":
                message = await context.bot.send_photo(
                    chat_id=DATABASE_CHANNEL_ID,
                    photo=first_file_id,
                    caption=caption + f"\n\nMetadata: {str(metadata)}")
            elif first_media_type == "video":
                message = await context.bot.send_video(
                    chat_id=DATABASE_CHANNEL_ID,
                    video=first_file_id,
                    caption=caption + f"\n\nMetadata: {str(metadata)}")
            else:
                message = await context.bot.send_document(
                    chat_id=DATABASE_CHANNEL_ID,
                    document=first_file_id,
                    caption=caption + f"\n\nMetadata: {str(metadata)}")
        else:
            # For single file
            if media_type == "photo":
                message = await context.bot.send_photo(
                    chat_id=DATABASE_CHANNEL_ID,
                    photo=file_id,
                    caption=caption + f"\n\nMetadata: {str(metadata)}")
            elif media_type == "video":
                message = await context.bot.send_video(
                    chat_id=DATABASE_CHANNEL_ID,
                    video=file_id,
                    caption=caption + f"\n\nMetadata: {str(metadata)}")
            else:
                message = await context.bot.send_document(
                    chat_id=DATABASE_CHANNEL_ID,
                    document=file_id,
                    caption=caption + f"\n\nMetadata: {str(metadata)}")

        # Store message ID for retrieval
        channel_message_storage[code] = {
            'message_id': message.message_id,
            'metadata': metadata
        }

        logger.info(f"Saved media to database channel with code: {code}")
        return code
    except Exception as e:
        logger.error(f"Failed to save media to database channel: {e}")
        return None


async def get_media_from_channel(context, code):
    """Retrieve media metadata from database channel by code"""
    try:
        # Check if we have the code in our storage
        if code in channel_message_storage:
            stored_data = channel_message_storage[code]
            metadata = stored_data['metadata']

            # Check if not expired
            expires_at = datetime.fromisoformat(metadata["expires_at"])
            if expires_at > datetime.utcnow():
                logger.info(f"Retrieved media metadata for code: {code}")
                return metadata
            else:
                # Remove expired entry
                del channel_message_storage[code]
                logger.info(
                    f"Media with code {code} has expired and was removed")
                return None
        else:
            logger.info(f"Media with code {code} not found in storage")
            return None

    except Exception as e:
        logger.error(f"Failed to retrieve media from storage: {e}")
        return None


async def check_channel_membership(context, user_id):
    """Check if user is a member of the backup channel"""
    try:
        member = await context.bot.get_chat_member(f"@{BACKUP_CHANNEL}",
                                                   user_id)
        # Check if user is a member (not left or kicked)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.warning(f"Could not check membership for user {user_id}: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if args:
        code = args[0].replace("media_", "").replace("batch_", "")

        # Get media metadata from database channel
        metadata = await get_media_from_channel(context, code)

        if metadata:
            # Admins bypass channel membership check
            if user_id != ADMIN_USER_ID:
                # Skip backup channel check for users who previously joined
                if user_id not in channel_joined_users:
                    # Check if user has joined the backup channel
                    is_member = await check_channel_membership(
                        context, user_id)

                    if not is_member:
                        # Create join backup channel button for non-members
                        keyboard = [[
                            InlineKeyboardButton(
                                "📢 Join Backup Channel",
                                url=f"https://t.me/{BACKUP_CHANNEL}")
                        ]]
                        reply_markup = InlineKeyboardMarkup(keyboard)

                        await update.message.reply_text(
                            "🔒 **Access Restricted**\n\n"
                            "To access shared media, you must first join our backup channel.\n\n"
                            "After joining, try the link again:",
                            reply_markup=reply_markup,
                            parse_mode='Markdown')
                        return
                    else:
                        # User is a member, add them to joined users list
                        channel_joined_users.add(user_id)

            # Send media based on type
            if metadata["type"] == "single":
                file_id = metadata["file_id"]
                media_type = metadata["media_type"]

                if media_type == "photo":
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id, photo=file_id)
                elif media_type == "video":
                    await context.bot.send_video(
                        chat_id=update.effective_chat.id, video=file_id)
                elif media_type == "document":
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id, document=file_id)

            elif metadata["type"] == "batch":
                files_data = metadata["files"]
                await update.message.reply_text(
                    f"📦 **Batch Media ({len(files_data)} files)**")

                # Send all files in the batch
                for file_id, media_type in files_data:
                    if media_type == "photo":
                        await context.bot.send_photo(
                            chat_id=update.effective_chat.id, photo=file_id)
                    elif media_type == "video":
                        await context.bot.send_video(
                            chat_id=update.effective_chat.id, video=file_id)
                    elif media_type == "document":
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id, document=file_id)
        else:
            await update.message.reply_text("❌ Invalid or expired code.")
    else:
        # Welcome message for users
        if user_id == ADMIN_USER_ID:
            await update.message.reply_text(
                "🎉 Welcome Admin!\n\n"
                "📤 Send a file (document/photo/video) to generate a temporary link.\n"
                "⏰ Use `/admin timer <minutes>` to set custom expiration times."
            )
        else:
            # Create join backup channel button for regular users
            keyboard = [[
                InlineKeyboardButton("📢 Join Backup Channel",
                                     url=f"https://t.me/{BACKUP_CHANNEL}")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "🎉 Welcome to the Media Share Bot!\n\n"
                "📤 Administrators can send files to generate temporary shareable links.\n\n"
                "💡 For important updates and backup access, please join our backup channel:",
                reply_markup=reply_markup)


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Check if user is admin
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text(
            "🔒 **Access Denied**\n\n"
            "Only administrators can access the admin panel.",
            parse_mode='Markdown')
        return

    args = context.args

    if not args:
        # Show admin panel menu
        current_timer = context.user_data.get('custom_timer', 2880)
        hours = current_timer // 60
        remaining_mins = current_timer % 60
        time_str = f"{hours}h {remaining_mins}m" if hours > 0 and remaining_mins > 0 else f"{hours}h" if hours > 0 else f"{current_timer}m"

        channel_members = len(channel_joined_users)

        await update.message.reply_text(
            f"🔧 **Admin Control Panel**\n\n"
            f"📊 **Statistics:**\n"
            f"• Channel members: {channel_members}\n"
            f"• Current timer: {time_str}\n"
            f"• Database Channel: {DATABASE_CHANNEL_ID}\n\n"
            f"⚙️ **Available Commands:**\n"
            f"• `/admin timer <minutes>` - Set custom timer\n"
            f"• `/admin stats` - View statistics\n"
            f"• `/admin reset` - Reset timer to default (2 days)",
            parse_mode='Markdown')
        return

    command = args[0].lower()

    if command == "timer":
        if len(args) < 2:
            await update.message.reply_text(
                "⏰ **Set Timer**\n\n"
                "Usage: `/admin timer <minutes>`\n\n"
                "Examples:\n"
                "• `/admin timer 30` - Set 30 minutes\n"
                "• `/admin timer 120` - Set 2 hours\n"
                "• `/admin timer 1440` - Set 24 hours",
                parse_mode='Markdown')
            return

        try:
            minutes = int(args[1])
            if minutes <= 0:
                await update.message.reply_text("❌ Timer must be positive.")
                return
            if minutes > 10080:  # 1 week limit
                await update.message.reply_text(
                    "❌ Maximum timer is 10080 minutes (1 week).")
                return

            context.user_data['custom_timer'] = minutes

            hours = minutes // 60
            remaining_mins = minutes % 60
            time_str = f"{hours}h {remaining_mins}m" if hours > 0 and remaining_mins > 0 else f"{hours}h" if hours > 0 else f"{minutes}m"

            await update.message.reply_text(
                f"✅ **Timer Updated**\n\n"
                f"New expiration time: **{time_str}**",
                parse_mode='Markdown')
        except ValueError:
            await update.message.reply_text("❌ Please provide a valid number.")

    elif command == "stats":
        # Statistics
        channel_members = len(channel_joined_users)
        current_timer = context.user_data.get('custom_timer', 2880)

        await update.message.reply_text(
            f"📊 **Statistics**\n\n"
            f"• Channel members: {channel_members}\n"
            f"• Current timer: {current_timer} minutes\n"
            f"• Backup channel: @{BACKUP_CHANNEL}\n"
            f"• Database channel: {DATABASE_CHANNEL_ID}",
            parse_mode='Markdown')

    elif command == "reset":
        context.user_data['custom_timer'] = 2880
        await update.message.reply_text(
            "🔄 **Timer Reset**\n\n"
            "Timer has been reset to default (2 days).")

    else:
        await update.message.reply_text(
            "❌ **Unknown Command**\n\n"
            "Use `/admin` to see available commands.",
            parse_mode='Markdown')


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if effective_user exists (prevent channel message processing)
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    # Check if user is admin
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text(
            "🔒 **Access Denied**\n\n"
            "Only administrators can upload files and generate sharing links.",
            parse_mode='Markdown')
        return

    file = None
    media_type = None

    if update.message.document:
        file = update.message.document
        media_type = "document"
    elif update.message.video:
        file = update.message.video
        media_type = "video"
    elif update.message.photo:
        file = update.message.photo[-1]
        media_type = "photo"

    if file:
        # Get custom timer or use default
        custom_timer = context.user_data.get('custom_timer', 2880)

        file_id = file.file_id

        # Check if user is in batch mode
        if 'batch_files' not in context.user_data:
            context.user_data['batch_files'] = []

        # Add file to batch
        context.user_data['batch_files'].append((file_id, media_type))

        # Send options for batch or single upload
        keyboard = [[
            InlineKeyboardButton("📤 Generate Link Now",
                                 callback_data="generate_single")
        ], [
            InlineKeyboardButton("📦 Add More Files", callback_data="add_more")
        ],
                    [
                        InlineKeyboardButton("🔗 Generate Batch Link",
                                             callback_data="generate_batch")
                    ]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        file_count = len(context.user_data['batch_files'])

        await update.message.reply_text(
            f"📁 **File Added** ({file_count} total)\n\n"
            f"Choose what to do:",
            reply_markup=reply_markup,
            parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "❌ Unsupported media. Send document, video, or photo.")


async def handle_channel_media(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """Handle media uploaded directly to the database channel"""
    # Only process messages from the database channel
    if not update.effective_chat or update.effective_chat.id != DATABASE_CHANNEL_ID:
        return

    # Only process if the upload is from an admin
    if not update.effective_user or update.effective_user.id != ADMIN_USER_ID:
        return

    # Check if message exists
    if not update.message:
        return

    file = None
    media_type = None

    if update.message.document:
        file = update.message.document
        media_type = "document"
    elif update.message.video:
        file = update.message.video
        media_type = "video"
    elif update.message.photo:
        file = update.message.photo[-1]
        media_type = "photo"

    if file:
        file_id = file.file_id

        # Generate code and save metadata
        code = generate_code()
        expires_at = datetime.utcnow() + timedelta(
            minutes=2880)  # Default 2 days

        metadata = {
            "code": code,
            "type": "single",
            "file_id": file_id,
            "media_type": media_type,
            "expires_at": expires_at.isoformat(),
            "created_at": datetime.utcnow().isoformat()
        }

        # Store the metadata using the existing message
        channel_message_storage[code] = {
            'message_id': update.message.message_id,
            'metadata': metadata
        }

        # Get bot username for link generation
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=media_{code}"

        # Reply to the uploaded file with the link
        try:
            await context.bot.send_message(
                chat_id=DATABASE_CHANNEL_ID,
                text=f"🔗 **Auto-Generated Link**\n\n"
                f"Code: `{code}`\n"
                f"Type: {media_type}\n"
                f"Expires: {expires_at.strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
                f"Link: {link}",
                parse_mode='Markdown',
                reply_to_message_id=update.message.message_id)

            logger.info(
                f"Auto-generated link for channel upload with code: {code}")

        except Exception as e:
            logger.error(f"Failed to send link reply in database channel: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Check if user is admin
    if user_id != ADMIN_USER_ID:
        await query.edit_message_text("🔒 Access Denied")
        return

    custom_timer = context.user_data.get('custom_timer', 2880)
    batch_files = context.user_data.get('batch_files', [])

    if not batch_files:
        await query.edit_message_text("❌ No files to process.")
        return

    bot_username = (await context.bot.get_me()).username

    # Format time display
    hours = custom_timer // 60
    remaining_mins = custom_timer % 60
    time_str = f"{hours}h {remaining_mins}m" if hours > 0 and remaining_mins > 0 else f"{hours}h" if hours > 0 else f"{custom_timer}m"

    if query.data == "generate_single":
        # Generate link for the last file only
        file_id, media_type = batch_files[-1]
        code = await save_media_to_channel(context, file_id, media_type,
                                           custom_timer)

        if code:
            link = f"https://t.me/{bot_username}?start=media_{code}"
            await query.edit_message_text(
                f"✅ **Single File Link Generated**\n\n"
                f"🔗 Link (valid {time_str}):\n{link}")
        else:
            await query.edit_message_text(
                "❌ Failed to generate link. Please try again.")

        # Clear batch
        context.user_data['batch_files'] = []

    elif query.data == "generate_batch":
        # Generate batch link for all files
        code = await save_media_to_channel(context, None, None, custom_timer,
                                           batch_files)

        if code:
            link = f"https://t.me/{bot_username}?start=batch_{code}"
            await query.edit_message_text(
                f"✅ **Batch Link Generated** ({len(batch_files)} files)\n\n"
                f"🔗 Link (valid {time_str}):\n{link}")
        else:
            await query.edit_message_text(
                "❌ Failed to generate batch link. Please try again.")

        # Clear batch
        context.user_data['batch_files'] = []

    elif query.data == "add_more":
        await query.edit_message_text(
            f"📦 **Batch Mode Active** ({len(batch_files)} files)\n\n"
            f"Send more files to add them to the batch.")


def main():
    if not TOKEN:
        print("❌ Please set BOT_TOKEN in Replit Secrets")
        return

    # Start Flask server in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server started on http://0.0.0.0:5000")

    # Create bot application
    application = ApplicationBuilder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL &
            (filters.Document.ALL | filters.VIDEO | filters.PHOTO),
            handle_channel_media))
    application.add_handler(
        MessageHandler(
            ~filters.ChatType.CHANNEL &
            (filters.Document.ALL | filters.VIDEO | filters.PHOTO),
            handle_media))

    logger.info("🤖 Bot started...")

    # Run the bot
    application.run_polling()


if __name__ == "__main__":
    main()
