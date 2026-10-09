# TikTok Downloader Telegram Bot

Paste a TikTok link → choose a download option → receive the video / photos.

- 🎬 Video: No Watermark (Auto HD), Watermark version, Audio MP3
- 🖼️ Slideshow: all photos as album, per-photo buttons, Audio MP3
- 🔗 Direct URL buttons for browser download
- 🇬🇧 Bot language: English

## Run on Termux (Android)

```bash
pkg update && pkg upgrade -y
pkg install python git -y
git clone https://github.com/iffatazriel/botttdownloader.git
cd botttdownloader
pip install -r requirements.txt
export TELEGRAM_TOKEN="your-bot-token-here"
bash start.sh
```

Keep it alive: run `termux-wake-lock`, disable battery optimization for Termux.

## Run on Windows

```bat
set TELEGRAM_TOKEN=your-bot-token-here
run.bat
```

## Files

| File | Description |
| ---- | ----------- |
| `bot.py` | Bot source code (python-telegram-bot + TikWM API) |
| `requirements.txt` | Python dependencies |
| `start.sh` | Start script for Termux / Linux |
| `run.bat` | Start script for Windows |
| `.env.example` | Token template (copy to `.env`, never commit real token) |
