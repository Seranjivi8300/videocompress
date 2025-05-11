import os
import logging
import tempfile
import time
from typing import Dict, Any, Optional
import asyncio
from functools import partial

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler
from telegram.ext import ContextTypes, filters
import ffmpeg

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB for regular users
PREMIUM_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB for premium users
TEMP_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "tg_video_compressor")
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

# Track active compression tasks
active_tasks: Dict[int, Dict[str, Any]] = {}

# Compression presets
COMPRESSION_PRESETS = {
    "low": {
        "crf": 28,
        "preset": "veryfast",
        "description": "Small file size, lower quality"
    },
    "medium": {
        "crf": 23,
        "preset": "medium",
        "description": "Balanced file size and quality"
    },
    "high": {
        "crf": 18,
        "preset": "slow",
        "description": "Larger file size, higher quality"
    }
}

# Help messages
WELCOME_MESSAGE = """
👋 Welcome to Video Compressor Bot!

I can help you compress your videos to save space or make them easier to share.

*Commands:*
/start - Show this welcome message
/help - Show detailed help information
/settings - Choose your default compression settings
/cancel - Cancel ongoing compression

*How to use:*
1. Just send me a video
2. Choose your compression quality
3. Wait for the compressed video

*Supported formats:* MP4, MOV, AVI, MKV, WEBM, FLV
"""

HELP_MESSAGE = """
*📋 Video Compressor Bot Help*

*Basic Usage:*
• Send any video to start compression
• Select quality level when prompted
• Wait for compression to complete
• Download your compressed video

*Quality Settings:*
• *Low:* Small file size, lower quality - best for sharing quickly
• *Medium:* Balanced quality and size - recommended for most videos
• *High:* Higher quality, larger file size - best for important videos

*Commands:*
/start - Show welcome message
/help - Show this help message
/settings - Set your default compression quality
/cancel - Cancel the current compression task

*File Size Limits:*
• Regular users: Up to 50MB
• Premium users: Up to 2GB

*Supported Video Formats:*
MP4, MOV, AVI, MKV, WEBM, FLV

*Tips:*
• For fastest results, use Low quality
• For important videos, use High quality
• Monitor progress with the status messages
"""

SETTINGS_MESSAGE = """
*⚙️ Compression Settings*

Choose your default compression quality:

*Current default:* {default_quality}

Select an option below to change:
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message when the command /start is issued."""
    await update.message.reply_text(
        WELCOME_MESSAGE,
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send detailed help information when the command /help is issued."""
    await update.message.reply_text(
        HELP_MESSAGE,
        parse_mode="Markdown"
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /settings command to set default quality."""
    user_id = update.effective_user.id
    
    # Get current default quality from user data or set to medium
    default_quality = context.user_data.get("default_quality", "medium")
    
    keyboard = [
        [
            InlineKeyboardButton("🔄 Low", callback_data="set_default_low"),
            InlineKeyboardButton("🔄 Medium", callback_data="set_default_medium"),
            InlineKeyboardButton("🔄 High", callback_data="set_default_high"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        SETTINGS_MESSAGE.format(default_quality=default_quality.capitalize()),
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle settings callback queries."""
    query = update.callback_query
    await query.answer()
    
    # Extract the quality from callback data
    quality = query.data.replace("set_default_", "")
    
    # Save user preference
    context.user_data["default_quality"] = quality
    
    await query.edit_message_text(
        f"✅ Default compression quality set to: *{quality.capitalize()}*\n\n"
        f"This will be used when you don't specify a quality level.",
        parse_mode="Markdown"
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the current compression task."""
    user_id = update.effective_user.id
    
    if user_id in active_tasks:
        # Mark as canceled in the task dict
        active_tasks[user_id]["canceled"] = True
        await update.message.reply_text(
            "⚠️ Canceling your compression task. Please wait...",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "You don't have any active compression tasks to cancel.",
            parse_mode="Markdown"
        )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle video files sent by users."""
    user_id = update.effective_user.id
    
    # Check if user already has an active compression task
    if user_id in active_tasks:
        await update.message.reply_text(
            "⚠️ You already have an active compression task. Please wait for it to complete or use /cancel."
        )
        return
    
    # Get the video file
    video = update.message.video or update.message.document
    
    if not video:
        await update.message.reply_text(
            "❌ No video detected. Please send a video file to compress."
        )
        return
    
    # Check file size
    file_size = video.file_size
    
    # Check if user is premium (this is just a placeholder - implement actual check)
    is_premium = context.user_data.get("is_premium", False)
    size_limit = PREMIUM_MAX_FILE_SIZE if is_premium else MAX_FILE_SIZE
    
    if file_size > size_limit:
        await update.message.reply_text(
            f"❌ Video is too large. Maximum size is {size_limit/(1024*1024):.1f}MB for "
            f"{'premium' if is_premium else 'regular'} users."
        )
        return
    
    # Display compression quality options
    keyboard = [
        [
            InlineKeyboardButton("🔄 Low", callback_data="compress_low"),
            InlineKeyboardButton("🔄 Medium", callback_data="compress_medium"),
            InlineKeyboardButton("🔄 High", callback_data="compress_high"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Get default quality
    default_quality = context.user_data.get("default_quality", "medium")
    
    await update.message.reply_text(
        f"🎬 Video received ({file_size/(1024*1024):.1f}MB)\n\n"
        f"Please select compression quality or use your default (*{default_quality.capitalize()}*) quality.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    
    # Save video info for later
    context.user_data["pending_video"] = {
        "file_id": video.file_id,
        "file_name": video.file_name if hasattr(video, "file_name") else f"video_{int(time.time())}.mp4",
        "file_size": file_size,
        "mime_type": video.mime_type if hasattr(video, "mime_type") else "video/mp4"
    }


async def handle_compression_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle compression quality selection callback."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    quality = query.data.replace("compress_", "")
    
    # Get pending video info
    video_info = context.user_data.get("pending_video")
    if not video_info:
        await query.edit_message_text(
            "❌ No pending video found. Please send a video file again."
        )
        return
    
    # Update message with compression status
    status_message = await query.edit_message_text(
        f"⚙️ Starting compression with *{quality.capitalize()}* quality preset...\n\n"
        f"Original size: {video_info['file_size']/(1024*1024):.1f}MB\n"
        f"Please wait, this may take some time depending on the video size.",
        parse_mode="Markdown"
    )
    
    # Create task entry
    active_tasks[user_id] = {
        "video_info": video_info,
        "quality": quality,
        "status_message_id": status_message.message_id,
        "start_time": time.time(),
        "canceled": False
    }
    
    # Start compression in background
    asyncio.create_task(process_compression(update, context, user_id))


async def process_compression(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Process video compression in the background."""
    try:
        task_info = active_tasks[user_id]
        video_info = task_info["video_info"]
        quality = task_info["quality"]
        status_message_id = task_info["status_message_id"]
        
        # Update status message
        await context.bot.edit_message_text(
            f"⬇️ Downloading video...\n\n"
            f"Original size: {video_info['file_size']/(1024*1024):.1f}MB\n"
            f"Quality preset: *{quality.capitalize()}*",
            chat_id=update.effective_chat.id,
            message_id=status_message_id,
            parse_mode="Markdown"
        )
        
        # Download video file
        video_file = await context.bot.get_file(video_info["file_id"])
        
        # Create unique filename
        input_path = os.path.join(TEMP_DOWNLOAD_DIR, f"input_{user_id}_{int(time.time())}_{video_info['file_name']}")
        output_path = os.path.join(TEMP_DOWNLOAD_DIR, f"output_{user_id}_{int(time.time())}_{video_info['file_name']}")
        
        # Download the file
        await video_file.download_to_drive(input_path)
        
        # Check for cancellation
        if active_tasks[user_id]["canceled"]:
            os.remove(input_path)
            await context.bot.edit_message_text(
                "❌ Compression canceled.",
                chat_id=update.effective_chat.id,
                message_id=status_message_id
            )
            del active_tasks[user_id]
            return
        
        # Update status message
        await context.bot.edit_message_text(
            f"🔄 Compressing video...\n\n"
            f"Quality preset: *{quality.capitalize()}*\n"
            f"({COMPRESSION_PRESETS[quality]['description']})\n\n"
            f"This may take several minutes for larger videos.",
            chat_id=update.effective_chat.id,
            message_id=status_message_id,
            parse_mode="Markdown"
        )
        
        # Get compression settings
        preset = COMPRESSION_PRESETS[quality]
        
        # Run compression in a separate thread to avoid blocking
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            partial(compress_video, input_path, output_path, preset)
        )
        
        # Check for cancellation
        if active_tasks[user_id]["canceled"]:
            # Clean up files
            for file_path in [input_path, output_path]:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    
            await context.bot.edit_message_text(
                "❌ Compression canceled.",
                chat_id=update.effective_chat.id,
                message_id=status_message_id
            )
            del active_tasks[user_id]
            return
        
        # Get compressed file size
        output_size = os.path.getsize(output_path)
        size_reduction = 100 - (output_size / video_info["file_size"] * 100)
        
        # Update status message
        await context.bot.edit_message_text(
            f"⬆️ Uploading compressed video...\n\n"
            f"Original size: {video_info['file_size']/(1024*1024):.1f}MB\n"
            f"Compressed size: {output_size/(1024*1024):.1f}MB\n"
            f"Reduction: {size_reduction:.1f}%",
            chat_id=update.effective_chat.id,
            message_id=status_message_id,
            parse_mode="Markdown"
        )
        
        # Send the compressed video back to the user
        with open(output_path, "rb") as video_file:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=video_file,
                caption=f"✅ Video compressed successfully!\n\n"
                        f"Quality preset: *{quality.capitalize()}*\n"
                        f"Original size: {video_info['file_size']/(1024*1024):.1f}MB\n"
                        f"Compressed size: {output_size/(1024*1024):.1f}MB\n"
                        f"Space saved: {size_reduction:.1f}%",
                parse_mode="Markdown",
                supports_streaming=True,
                filename=f"compressed_{video_info['file_name']}"
            )
        
        # Clean up the status message
        await context.bot.edit_message_text(
            f"✅ Compression completed!\n\n"
            f"Quality preset: *{quality.capitalize()}*\n"
            f"Original size: {video_info['file_size']/(1024*1024):.1f}MB\n"
            f"Compressed size: {output_size/(1024*1024):.1f}MB\n"
            f"Reduction: {size_reduction:.1f}%\n\n"
            f"Send another video to compress again.",
            chat_id=update.effective_chat.id,
            message_id=status_message_id,
            parse_mode="Markdown"
        )
    
    except Exception as e:
        logger.error(f"Error during compression: {e}")
        try:
            await context.bot.edit_message_text(
                f"❌ An error occurred during compression:\n{str(e)}\n\nPlease try again later.",
                chat_id=update.effective_chat.id,
                message_id=status_message_id
            )
        except Exception:
            pass
    
    finally:
        # Clean up
        try:
            for file_path in [input_path, output_path]:
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up files: {e}")
        
        # Remove task from active tasks
        if user_id in active_tasks:
            del active_tasks[user_id]


def compress_video(input_path: str, output_path: str, preset: Dict[str, Any]) -> None:
    """Compress video using ffmpeg."""
    try:
        # Create the ffmpeg process
        (
            ffmpeg
            .input(input_path)
            .output(
                output_path,
                vcodec='libx264',
                crf=preset["crf"],
                preset=preset["preset"],
                acodec='aac',
                audio_bitrate='128k',
                **{'-movflags': '+faststart'}
            )
            .global_args('-y')  # Overwrite output file if it exists
            .run(quiet=True, overwrite_output=True)
        )
    except ffmpeg.Error as e:
        logger.error(f"FFmpeg error: {e.stderr.decode() if e.stderr else str(e)}")
        raise Exception(f"Video compression failed: {e.stderr.decode() if e.stderr else str(e)}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors in the dispatcher."""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        # Send a message to the user
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ An error occurred while processing your request. Please try again later."
            )
    except Exception:
        pass


def main() -> None:
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    
    # Add callback query handlers
    application.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^set_default_"))
    application.add_handler(CallbackQueryHandler(handle_compression_callback, pattern=r"^compress_"))
    
    # Add video handler
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Run the bot until the user presses Ctrl-C
    application.run_polling()


if __name__ == "__main__":
    main()
