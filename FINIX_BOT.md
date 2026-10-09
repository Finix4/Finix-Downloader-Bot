# Finix Telegram Downloader Bot

Finix downloads public videos from TikTok, Instagram, YouTube, and Facebook. It rejects videos longer than 60 minutes, adds a subtle “Finix Bot” watermark to video downloads, and can extract MP3 audio.

## Run

1. Revoke any bot token that was shared in chat and create a replacement with BotFather.
2. Save the replacement as the Replit Secret `BOT_TOKEN`.
3. Ensure Python 3.11 and FFmpeg are available.
4. Install the dependencies with `python -m pip install -r requirements.txt`.
5. Start the bot with `python -m finix_bot.bot`.

The bot uses long polling and must remain running to respond. It downloads only publicly accessible links on the supported platforms. Telegram's upload limit is handled with a conservative 49 MiB file-size ceiling.

## Supported links

- TikTok
- Instagram Reels and posts
- YouTube videos and Shorts
- Facebook videos and `fb.watch` links

The MP3 button is attached to each completed video. The About and Share buttons are available on the welcome message and download replies.
