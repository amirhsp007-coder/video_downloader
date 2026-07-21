import os
import subprocess
import json
import signal
import sys
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.request import HTTPXRequest

# -------------------
# ENV & CONFIG
# -------------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# -------------------
# USER AUTHENTICATION
# -------------------
ALLOWED_USERNAMES = [
    "QuestionableCat",
    "someonesnosy",
]

def is_user_allowed(username: str) -> bool:
    if not username:
        return False
    username = username.lstrip('@')
    return username in ALLOWED_USERNAMES

# -------------------
# YT-DLP HELPERS (unchanged)
# -------------------
def get_video_info(url):
    try:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--dump-json",
            "--no-warnings",
            "--skip-download",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)

        info = {
            'title': data.get('title', 'Unknown Title'),
            'duration': data.get('duration', 0),
            'thumbnail': data.get('thumbnail', ''),
            'formats': [],
            'url': url
        }

        for fmt in data.get('formats', []):
            height = fmt.get('height')
            if height and height > 0:
                info['formats'].append({
                    'height': height,
                    'format_id': fmt.get('format_id'),
                    'ext': fmt.get('ext'),
                    'filesize': fmt.get('filesize', 0),
                    'acodec': fmt.get('acodec'),
                    'vcodec': fmt.get('vcodec')
                })

        # Remove duplicate heights (keep best quality per height)
        seen = set()
        unique = []
        for fmt in sorted(info['formats'], key=lambda x: x['height'], reverse=True):
            if fmt['height'] not in seen:
                seen.add(fmt['height'])
                unique.append(fmt)
        info['formats'] = unique
        return info
    except Exception as e:
        print(f"Error getting video info: {e}")
        return None

def download_video(url, format_string, quality_label):
    try:
        output_template = os.path.join(DOWNLOAD_DIR, f"%(title)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-o", output_template,
            "--format", format_string,
            "--no-progress",
            "--newline",
            url
        ]
        if "+" in format_string:
            cmd.extend(["--merge-output-format", "mp4"])
        if "bestaudio" in format_string:
            cmd.extend(["--extract-audio", "--audio-format", "mp3"])

        print(f"Executing: {' '.join(cmd)}")
        subprocess.run(cmd, capture_output=True, text=True, check=True)

        downloaded = list(Path(DOWNLOAD_DIR).glob("*"))
        if downloaded:
            latest = max(downloaded, key=lambda f: f.stat().st_mtime)
            return str(latest)
        return None
    except Exception as e:
        print(f"Download error: {e}")
        return None

# -------------------
# BOT HANDLERS (unchanged)
# -------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    if not is_user_allowed(username):
        await update.message.reply_text(
            f"🚫 **Access Denied**\n\n"
            f"Your username: @{username if username else 'No username set'}\n"
            f"Allowed users: {', '.join(['@' + u for u in ALLOWED_USERNAMES])}",
            parse_mode='Markdown'
        )
        return
    await update.message.reply_text(
        "🎬 Welcome to the YouTube Downloader Bot!\n\n"
        "Send me a YouTube, Instagram, TikTok, or other supported video URL.\n"
        "I'll fetch the available qualities and let you choose."
    )

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or ""
    if not is_user_allowed(username):
        print(f"🚫 Unauthorized access from @{username}")
        await update.message.reply_text("🚫 You are not authorized.")
        return

    url = update.message.text.strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        await update.message.reply_text("Please send a valid URL.")
        return

    print(f"📥 User @{username} requested: {url}")
    status_msg = await update.message.reply_text("🔄 Fetching video information...")

    video_info = get_video_info(url)
    if not video_info or not video_info.get('formats'):
        await status_msg.edit_text("❌ Could not fetch video information.")
        return

    context.user_data['video_info'] = video_info
    context.user_data['url'] = url

    keyboard = []
    keyboard.append([InlineKeyboardButton("🎵 Audio only (MP3)", callback_data="quality_audio")])
    for fmt in video_info['formats'][:6]:
        height = fmt['height']
        if height:
            has_audio = fmt.get('acodec') != 'none' if fmt.get('acodec') else True
            label = f"📹 {height}p"
            if not has_audio:
                label += " (video only)"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"quality_{fmt['format_id']}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = video_info.get('title', 'Unknown Title')
    duration = video_info.get('duration', 0)
    minutes = int(duration // 60)
    seconds = int(duration % 60)

    info_text = f"📹 **{title}**\n⏱ {minutes}:{seconds:02d}\n\nSelect quality:"
    await status_msg.edit_text(info_text, reply_markup=reply_markup, parse_mode='Markdown')

async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    username = user.username or ""
    if not is_user_allowed(username):
        await query.edit_message_text("🚫 You are not authorized.")
        return

    video_info = context.user_data.get('video_info')
    url = context.user_data.get('url')
    if not video_info or not url:
        await query.edit_message_text("❌ Session expired. Please send the URL again.")
        return

    callback_data = query.data
    format_string = None
    quality_label = ""

    if callback_data == "quality_audio":
        format_string = "bestaudio/best"
        quality_label = "Audio (MP3)"
    else:
        format_id = callback_data.replace("quality_", "")
        for fmt in video_info['formats']:
            if str(fmt['format_id']) == format_id:
                height = fmt['height']
                quality_label = f"{height}p"
                has_audio = fmt.get('acodec') != 'none' if fmt.get('acodec') else True
                if not has_audio:
                    format_string = f"{format_id}+bestaudio"
                    quality_label += " (with audio)"
                else:
                    format_string = format_id
                break

    if not format_string:
        await query.edit_message_text("❌ Invalid quality selection.")
        return

    await query.edit_message_text(f"📥 Downloading {quality_label}... Please wait.")
    downloaded_file = download_video(url, format_string, quality_label)

    if not downloaded_file or not os.path.exists(downloaded_file):
        await query.edit_message_text("❌ Download failed.")
        return

    file_size = os.path.getsize(downloaded_file) / (1024 * 1024)
    try:
        if file_size > 50:
            await query.edit_message_text(f"⚠️ File is {file_size:.1f}MB. Telegram limit is 50MB.")
            os.remove(downloaded_file)
            return

        ext = Path(downloaded_file).suffix.lower()
        await query.edit_message_text(f"📤 Uploading {quality_label}...")

        with open(downloaded_file, 'rb') as f:
            if ext in ['.mp3', '.m4a', '.aac', '.flac']:
                await context.bot.send_audio(
                    chat_id=query.message.chat.id,
                    audio=f,
                    title=video_info.get('title', 'Audio'),
                    duration=video_info.get('duration', 0)
                )
            else:
                await context.bot.send_video(
                    chat_id=query.message.chat.id,
                    video=f,
                    caption=f"✅ {quality_label} - {video_info.get('title', 'Video')}",
                    supports_streaming=True
                )

        os.remove(downloaded_file)
        await query.edit_message_text(f"✅ Download complete!\nQuality: {quality_label}\nSize: {file_size:.1f}MB")
    except Exception as e:
        await query.edit_message_text(f"❌ Error sending file: {str(e)}")
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)

# -------------------
# FLASK APP WITH HEALTH CHECK
# -------------------
web = Flask(__name__)

@web.route("/")
def home():
    return "🎬 Downloader Bot is running!"

@web.route("/health")
def health():
    return "OK"

# -------------------
# MAIN
# -------------------
def main():
    # Use a custom request with longer timeouts
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    application = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .build()
    )

    # Register handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    application.add_handler(CallbackQueryHandler(quality_callback, pattern="^quality_"))

    print("🤖 Bot is starting...")
    print(f"👥 Allowed usernames: {', '.join(['@' + u for u in ALLOWED_USERNAMES])}")

    # Use webhook if RENDER_EXTERNAL_URL is provided (Render sets this automatically)
   def main():
    # ... (application build code stays the same) ...

    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if external_url:
        webhook_url = f"{external_url}/{TOKEN}"
        print(f"🔗 Setting webhook to: {webhook_url}")
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            url_path=TOKEN,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    else:
        # fallback for local testing
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )

if __name__ == "__main__":
    # Graceful shutdown on SIGTERM (Render sends this)
    def signal_handler(sig, frame):
        print("\n🛑 Shutting down gracefully...")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
