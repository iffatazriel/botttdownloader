"""
Telegram TikTok Downloader Bot
Flow: paste link > choose download option > receive video/photos
Supports: No-Watermark Video (Auto HD) + Photo Slides + Audio + Direct URL links
API: TikWM (free, no key required)
"""
import os
import re
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.constants import ParseMode

# Bot token — MUST be set via TELEGRAM_TOKEN env var. Never hardcode it.
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TOKEN or ":" not in TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN env var. Example: export TELEGRAM_TOKEN='123:ABC'")

TIKTOK_RE = re.compile(r"(https?://)?((www|vm|vt|m)\.)?tiktok\.com/\S+|https?://(vt|vm)\.tiktok\.com/\S+", re.IGNORECASE)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Simple cache: video_id -> TikWM data (so callbacks don't re-fetch)
CACHE: dict[str, dict] = {}


async def fetch_tiktok(url: str) -> dict:
    """Fetch video/slideshow info from TikWM."""
    api = "https://www.tikwm.com/api/"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(api, params={"url": url})
        r.raise_for_status()
        j = r.json()
    if j.get("code") != 0:
        raise ValueError(j.get("msg", "Failed to fetch data. Private / invalid link?"))
    return j["data"]


def is_slide(data: dict) -> bool:
    imgs = data.get("images")
    return bool(imgs and isinstance(imgs, list) and len(imgs) > 0)


def best_video_url(data: dict) -> tuple[str, str]:
    """Auto-detect best quality: prefer HD, fall back to SD no-watermark.
    Returns (url, quality_label)."""
    if data.get("hdplay"):
        return data["hdplay"], "HD"
    return data.get("play", ""), "SD"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Hello! TikTok Downloader Bot</b>\n\n"
        "📎 <b>How to use:</b>\n"
        "1. Paste a TikTok link (e.g. https://vt.tiktok.com/xxxxx)\n"
        "2. Choose a download option\n"
        "3. The video / photos are sent here and can be saved\n\n"
        "✅ Supports: <b>No-Watermark Video (Auto HD)</b> & <b>Photo Slides</b> (auto-detected from link)\n"
        "🔗 Every option also has a <b>direct URL button</b> for browser download",
        parse_mode=ParseMode.HTML,
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = TIKTOK_RE.search(text)
    if not m:
        await update.message.reply_text("❌ That's not a TikTok link.\nExample: https://vt.tiktok.com/ZSxxxxxx")
        return

    url = m.group(0)
    if not url.startswith("http"):
        url = "https://" + url

    wait = await update.message.reply_text("⏳ Fetching video info...")
    try:
        data = await fetch_tiktok(url)
    except Exception as e:
        log.exception("fetch failed")
        await wait.edit_text(f"❌ Failed to fetch video.\n<code>{e}</code>\nTry another link / make sure the video is public.", parse_mode=ParseMode.HTML)
        return

    title = (data.get("title") or "TikTok video")[:200]
    author = data.get("author", {}).get("nickname") or data.get("author", {}).get("unique_id") or "-"
    vid_id = str(data.get("id") or data.get("aweme_id") or wait.message_id)
    CACHE[vid_id] = data

    cover = data.get("cover") or data.get("origin_cover") or data.get("ai_dynamic_cover")
    slide = is_slide(data)

    if slide:
        media_type = "🖼️ Photo Slideshow"
        quality_line = f"📸 {len(data.get('images', []))} photos detected"
    else:
        _, quality = best_video_url(data)
        media_type = "🎬 Video"
        quality_line = "✨ HD quality available (auto-selected)" if quality == "HD" else "📹 SD quality"

    caption = (
        f"{media_type}\n"
        f"👤 <b>{author}</b>\n"
        f"📝 {title}\n"
        f"❤️ {data.get('digg_count', 0):,} | ▶️ {data.get('play_count', 0):,}\n"
        f"{quality_line}\n\n"
        f"👇 <b>Choose a download option:</b>"
    )

    if slide:
        music_url = data.get("music", "")
        keyboard = [
            [InlineKeyboardButton("🖼️ Send All Photos (Album)", callback_data=f"album:{vid_id}")],
            [InlineKeyboardButton("🎵 Download Audio (MP3)", callback_data=f"music:{vid_id}")],
        ]
        # per-photo buttons (max 8, rest via album)
        imgs = data.get("images", [])[:8]
        row = []
        for i in range(len(imgs)):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"photo:{vid_id}:{i}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        # direct URL buttons
        if music_url:
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: Audio", url=music_url)])
    else:
        best_url, quality = best_video_url(data)
        keyboard = [
            [InlineKeyboardButton(f"🎬 Video No Watermark (Auto {quality})", callback_data=f"nowm:{vid_id}")],
            [InlineKeyboardButton("🎵 Audio MP3", callback_data=f"music:{vid_id}")],
            [InlineKeyboardButton("💧 Watermark Version", callback_data=f"wm:{vid_id}")],
        ]
        # direct URL buttons (browser download)
        if best_url:
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: Video", url=best_url)])
        if data.get("music"):
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: Audio", url=data["music"])])
        if data.get("hdplay"):
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: HD File", url=data["hdplay"])])

    try:
        if cover:
            await wait.delete()
            await update.message.reply_photo(
                photo=cover, caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await wait.edit_text(caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Preparing...")
    try:
        kind, *rest = q.data.split(":")
        vid_id = rest[0]
        data = CACHE.get(vid_id)
        if not data:
            try:
                await q.edit_message_caption(caption="❌ Session expired. Please send the link again.")
            except Exception:
                await q.edit_message_text("❌ Session expired. Please send the link again.")
            return

        title = (data.get("title") or "tiktok")[:50]
        author = data.get("author", {}).get("nickname", "TikTok")

        if kind == "nowm":
            url, quality = best_video_url(data)
            await q.message.reply_video(video=url, caption=f"🎬 {title}\n✅ No watermark | Quality: {quality} (auto)",
                                        supports_streaming=True)
            # extra direct link for browser download
            await q.message.reply_text(f"🔗 <b>Direct download link ({quality}):</b>\n{url}",
                                       parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        elif kind == "wm":
            url = data.get("wmplay")
            await q.message.reply_video(video=url, caption=f"🎬 {title}\n💧 Watermark version",
                                        supports_streaming=True)
            await q.message.reply_text(f"🔗 <b>Direct link:</b>\n{url}", parse_mode=ParseMode.HTML)
        elif kind == "music":
            url = data.get("music")
            await q.message.reply_audio(audio=url, title=title, performer=author)
            await q.message.reply_text(f"🔗 <b>Direct audio link:</b>\n{url}", parse_mode=ParseMode.HTML)
        elif kind == "photo":
            idx = int(rest[1])
            imgs = data.get("images", [])
            if idx < len(imgs):
                await q.message.reply_photo(photo=imgs[idx], caption=f"🖼️ Photo {idx+1}/{len(imgs)}\n{title}")
                await q.message.reply_text(f"🔗 <b>Direct photo link {idx+1}:</b>\n{imgs[idx]}", parse_mode=ParseMode.HTML)
        elif kind == "album":
            imgs = data.get("images", [])[:10]  # Telegram limit: 10 per album
            media = [InputMediaPhoto(media=u, caption=f"🖼️ Slides {i+1}/{len(data.get('images', []))}\n{title}" if i == 0 else "") for i, u in enumerate(imgs)]
            await q.message.reply_media_group(media=media)
            rest_imgs = data.get("images", [])[10:]
            for chunk_start in range(0, len(rest_imgs), 10):
                chunk = rest_imgs[chunk_start:chunk_start + 10]
                await q.message.reply_media_group(media=[InputMediaPhoto(media=u) for u in chunk])

        await q.answer("✅ Sent!")
    except Exception as e:
        log.exception("callback failed")
        try:
            await q.message.reply_text(f"❌ Failed to send: <code>{e}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass


def main():
    if not TOKEN or ":" not in TOKEN:
        raise SystemExit("Invalid TOKEN. Set TELEGRAM_TOKEN env var.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_choice))
    log.info("Bot is running... send a TikTok link to test.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
