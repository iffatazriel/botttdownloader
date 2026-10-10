"""
Telegram TikTok Downloader Bot
Flow: paste link > choose download option > receive video/photos
Supports: No-Watermark Video (Auto HD) + Photo Slides + Audio + Direct URL links
APIs (fallback chain, all free, no key): TikWM -> Vercel(tiktokapi-src) -> TDown
Env vars:
  TELEGRAM_TOKEN   (required) bot token from @BotFather
  ADMIN_ID         (optional) Telegram user ID allowed to use /stats
  CHANNEL_USERNAME (optional) e.g. "mybots" -> footer invite "@mybots"
"""
import hashlib
import logging
import os
import re
import sqlite3
from datetime import date, timedelta

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- config
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TOKEN or ":" not in TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN env var. Example: export TELEGRAM_TOKEN='123:ABC'")

ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
CHANNEL = os.getenv("CHANNEL_USERNAME", "").strip().lstrip("@")

TIKTOK_RE = re.compile(
    r"(https?://)?((www|vm|vt|m)\.)?tiktok\.com/\S+|https?://(vt|vm)\.tiktok\.com/\S+",
    re.IGNORECASE,
)

TIKWM_API = "https://www.tikwm.com/api/"
VERCEL_API = "https://tiktok-downbloder.vercel.app/"
TDOWN_API = "https://tdownv4.sl-bjs.workers.dev/"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "users.db")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# video_id -> normalized data (so callbacks don't re-fetch)
CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------- helpers
def channel_footer() -> str:
    return f"\n📢 More bots & updates: @{CHANNEL}" if CHANNEL else ""


def is_slide(data: dict) -> bool:
    imgs = data.get("images")
    return bool(imgs and isinstance(imgs, list) and len(imgs) > 0)


def best_video_url(data: dict) -> tuple[str, str]:
    """Auto-detect best quality: prefer HD, fall back to SD no-watermark."""
    if data.get("hdplay"):
        return data["hdplay"], "HD"
    return data.get("play", ""), "SD"


def parse_count(v) -> int:
    """Parse counts like 0, 123, '15.9K', '2.3M' into int."""
    if isinstance(v, (int, float)):
        return int(v)
    if not v:
        return 0
    s = str(v).strip().upper().replace(",", "")
    try:
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        if s.endswith("B"):
            return int(float(s[:-1]) * 1_000_000_000)
        return int(float(s))
    except ValueError:
        return 0


def extract_video_id(text: str, fallback: str) -> str:
    m = re.search(r"(\d{15,25})", text or "")
    if m:
        return m.group(1)
    return "v_" + hashlib.md5(fallback.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- providers
async def _provider_tikwm(client: httpx.AsyncClient, url: str) -> dict:
    r = await client.get(TIKWM_API, params={"url": url})
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise ValueError(j.get("msg", "TikWM error"))
    d = dict(j["data"])
    d["provider"] = "tikwm"
    return d


async def _provider_vercel(client: httpx.AsyncClient, url: str) -> dict:
    """tiktokapi-src v2 via Vercel. Fields: type/video|image, desc, author,
    statistics, video, music, images?"""
    r = await client.get(VERCEL_API, params={"url": url})
    r.raise_for_status()
    j = r.json()
    if not j.get("success"):
        raise ValueError("Vercel API reported failure")
    res = ((j.get("result") or {}).get("raw") or {}).get("result") or {}
    video = res.get("video") or ""
    images = res.get("images") or []
    if not video and not images:
        raise ValueError("Vercel API returned no media")
    author = res.get("author") or {}
    stats = res.get("statistics") or {}
    d = {
        "id": extract_video_id(video or str(images[:1]), url),
        "title": res.get("desc") or res.get("description") or "TikTok video",
        "author": {"nickname": author.get("nickname") or "TikTok",
                   "unique_id": author.get("nickname") or ""},
        "cover": author.get("avatar") or "",
        "play": video,
        "wmplay": "",
        "hdplay": "",
        "music": res.get("music") or "",
        "images": images if isinstance(images, list) else [],
        "digg_count": parse_count(stats.get("likeCount")),
        "play_count": parse_count(stats.get("playCount") or stats.get("viewCount")),
        "provider": "vercel",
    }
    return d


async def _provider_tdown(client: httpx.AsyncClient, url: str) -> dict:
    """TDown Cloudflare worker. Schema varies -> parse common keys defensively."""
    r = await client.get(TDOWN_API, params={"down": url})
    r.raise_for_status()
    try:
        j = r.json()
    except Exception:
        raise ValueError("TDown returned non-JSON")
    if isinstance(j, dict) and j.get("error"):
        raise ValueError(str(j["error"])[:120])

    def pick(*keys):
        for k in keys:
            if isinstance(j, dict) and j.get(k):
                return j[k]
        # one level deep
        if isinstance(j, dict):
            for v in j.values():
                if isinstance(v, dict):
                    for k in keys:
                        if v.get(k):
                            return v[k]
        return ""

    video = pick("video", "videoUrl", "play", "noWatermark", "nowm", "url")
    if isinstance(video, list):
        video = video[0] if video else ""
    music = pick("music", "musicUrl", "audio", "mp3")
    if isinstance(music, list):
        music = music[0] if music else ""
    images = pick("images", "imageList", "photos", "pictures")
    if not isinstance(images, list):
        images = []
    title = pick("desc", "description", "title", "caption")
    author = pick("author", "nickname", "username")
    if isinstance(author, dict):
        author_name = author.get("nickname") or author.get("username") or "TikTok"
        avatar = author.get("avatar") or ""
    else:
        author_name = str(author) or "TikTok"
        avatar = ""
    if not video and not images:
        raise ValueError("TDown returned no media")
    return {
        "id": extract_video_id(str(video), url),
        "title": str(title) or "TikTok video",
        "author": {"nickname": author_name, "unique_id": ""},
        "cover": avatar or pick("cover", "thumbnail", "poster"),
        "play": str(video),
        "wmplay": "",
        "hdplay": str(pick("hd", "hdplay", "hdUrl") or ""),
        "music": str(music),
        "images": images,
        "digg_count": 0,
        "play_count": 0,
        "provider": "tdown",
    }


PROVIDERS = (
    ("tikwm", _provider_tikwm),
    ("vercel", _provider_vercel),
    ("tdown", _provider_tdown),
)


async def fetch_tiktok(url: str) -> dict:
    """Try providers in order, return first normalized success."""
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": "TelegramBot"}) as client:
        for name, fn in PROVIDERS:
            try:
                data = await fn(client, url)
                log.info("fetched via %s (id=%s)", name, data.get("id"))
                return data
            except Exception as e:
                log.warning("provider %s failed: %s", name, e)
                errors.append(f"{name}: {e}")
    raise ValueError("All download servers failed (" + "; ".join(errors) +
                     "). The video may be private, or try again in a minute.")


# ---------------------------------------------------------------- stats DB
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS daily("
        "day TEXT, user_id INTEGER, requests INTEGER DEFAULT 0, "
        "downloads INTEGER DEFAULT 0, PRIMARY KEY(day, user_id))"
    )
    return con


def track(user_id: int, field: str) -> None:
    try:
        con = _db()
        today = date.today().isoformat()
        con.execute(
            f"INSERT INTO daily(day, user_id, requests, downloads) VALUES(?,?,0,0) "
            f"ON CONFLICT(day, user_id) DO UPDATE SET {field}={field}+1",
            (today, user_id),
        )
        # ensure the triggering event itself is counted on first insert
        con.execute(
            f"UPDATE daily SET {field}={field}+1 WHERE day=? AND user_id=? "
            f"AND {field}=0",
            (today, user_id),
        )
        con.commit()
        con.close()
    except Exception:
        log.exception("stats track failed")


def read_stats() -> dict:
    con = _db()
    total_users = con.execute("SELECT COUNT(DISTINCT user_id) FROM daily").fetchone()[0]
    total_dl = con.execute("SELECT COALESCE(SUM(downloads),0) FROM daily").fetchone()[0]
    total_req = con.execute("SELECT COALESCE(SUM(requests),0) FROM daily").fetchone()[0]
    today = date.today().isoformat()
    t_users = con.execute("SELECT COUNT(*) FROM daily WHERE day=?", (today,)).fetchone()[0]
    t_dl = con.execute("SELECT COALESCE(SUM(downloads),0) FROM daily WHERE day=?",
                       (today,)).fetchone()[0]
    week = [(date.today() - timedelta(days=i)).isoformat() for i in range(7)]
    by_day = con.execute(
        f"SELECT day, COALESCE(SUM(downloads),0) FROM daily WHERE day IN "
        f"({','.join('?' * 7)}) GROUP BY day ORDER BY day",
        week,
    ).fetchall()
    con.close()
    return {"users": total_users, "downloads": total_dl, "requests": total_req,
            "today_users": t_users, "today_dl": t_dl, "week": by_day}


# ---------------------------------------------------------------- handlers
WELCOME = (
    "👋 <b>Hello! TikTok Downloader Bot</b>\n\n"
    "📎 <b>How to use:</b>\n"
    "1. Paste a TikTok link (e.g. https://vt.tiktok.com/xxxxx)\n"
    "2. Choose a download option\n"
    "3. The video / photos are sent here and can be saved\n\n"
    "✅ <b>No-Watermark Video (Auto HD)</b> & <b>Photo Slides</b> — auto-detected\n"
    "🔗 Every option includes a <b>direct URL button</b> for browser download"
    "{footer}"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME.format(footer=channel_footer()),
                                    parse_mode=ParseMode.HTML)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID or str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("⛔ This command is for the bot admin only.")
        return
    s = read_stats()
    week_lines = "\n".join(f"• {d}: {c} downloads" for d, c in s["week"]) or "• no data yet"
    await update.message.reply_text(
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total users: <b>{s['users']}</b>\n"
        f"📥 Total downloads: <b>{s['downloads']}</b> (requests: {s['requests']})\n"
        f"📅 Today: <b>{s['today_dl']}</b> downloads / {s['today_users']} users\n\n"
        f"<b>Last 7 days:</b>\n{week_lines}",
        parse_mode=ParseMode.HTML,
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    m = TIKTOK_RE.search(text)
    if not m:
        await update.message.reply_text(
            "❌ <b>That's not a TikTok link.</b>\n"
            "Please send a link like:\n<code>https://vt.tiktok.com/ZSxxxxxx</code>\n"
            "Make sure the video is public, then try again." + channel_footer(),
            parse_mode=ParseMode.HTML,
        )
        return

    url = m.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    track(update.effective_user.id, "requests")

    wait = await update.message.reply_text("⏳ Fetching video info, please wait...")
    try:
        data = await fetch_tiktok(url)
    except Exception as e:
        log.exception("fetch failed")
        await wait.edit_text(
            "❌ <b>Couldn't fetch this video.</b>\n"
            f"<code>{e}</code>\n\n"
            "Tips: use a public video link, or try again in a minute." + channel_footer(),
            parse_mode=ParseMode.HTML,
        )
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
        quality_line = ("✨ HD quality available (auto-selected)" if quality == "HD"
                        else "📹 SD quality")

    caption = (
        f"{media_type}\n"
        f"👤 <b>{author}</b>\n"
        f"📝 {title}\n"
        f"❤️ {parse_count(data.get('digg_count')):,} | "
        f"▶️ {parse_count(data.get('play_count')):,}\n"
        f"{quality_line}\n\n"
        f"👇 <b>Choose a download option:</b>"
        f"{channel_footer()}"
    )

    if slide:
        music_url = data.get("music", "")
        keyboard = [
            [InlineKeyboardButton("🖼️ Send All Photos (Album)",
                                  callback_data=f"album:{vid_id}")],
        ]
        if music_url:
            keyboard.append([InlineKeyboardButton("🎵 Download Audio (MP3)",
                                                  callback_data=f"music:{vid_id}")])
        imgs = data.get("images", [])[:8]
        row = []
        for i in range(len(imgs)):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"photo:{vid_id}:{i}"))
            if len(row) == 4:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        if music_url:
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: Audio", url=music_url)])
    else:
        best_url, quality = best_video_url(data)
        keyboard = [
            [InlineKeyboardButton(f"🎬 Video No Watermark (Auto {quality})",
                                  callback_data=f"nowm:{vid_id}")],
        ]
        if data.get("music"):
            keyboard.append([InlineKeyboardButton("🎵 Audio MP3",
                                                  callback_data=f"music:{vid_id}")])
        if data.get("wmplay"):
            keyboard.append([InlineKeyboardButton("💧 Watermark Version",
                                                  callback_data=f"wm:{vid_id}")])
        if best_url:
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: Video", url=best_url)])
        if data.get("music"):
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: Audio", url=data["music"])])
        if data.get("hdplay"):
            keyboard.append([InlineKeyboardButton("🔗 Direct Link: HD File",
                                                  url=data["hdplay"])])

    try:
        if cover:
            await wait.delete()
            await update.message.reply_photo(
                photo=cover, caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            await wait.edit_text(caption, parse_mode=ParseMode.HTML,
                                 reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        log.exception("send info card failed, falling back to text")
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML,
                                        reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Preparing your file...")
    try:
        kind, *rest = q.data.split(":")
        vid_id = rest[0]
        data = CACHE.get(vid_id)
        if not data:
            try:
                await q.edit_message_caption(
                    caption="❌ Session expired. Please send the link again.")
            except Exception:
                await q.edit_message_text("❌ Session expired. Please send the link again.")
            return

        title = (data.get("title") or "tiktok")[:50]
        author = data.get("author", {}).get("nickname", "TikTok")
        footer = channel_footer()

        if kind == "nowm":
            url, quality = best_video_url(data)
            if not url:
                raise ValueError("No video URL available from the server.")
            await q.message.reply_video(
                video=url,
                caption=f"🎬 {title}\n✅ No watermark | Quality: {quality} (auto){footer}",
                supports_streaming=True)
            await q.message.reply_text(
                f"🔗 <b>Direct download link ({quality}):</b>\n{url}{footer}",
                parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        elif kind == "wm":
            url = data.get("wmplay")
            if not url:
                raise ValueError("Watermark version not available for this video.")
            await q.message.reply_video(video=url,
                                        caption=f"🎬 {title}\n💧 Watermark version{footer}",
                                        supports_streaming=True)
            await q.message.reply_text(f"🔗 <b>Direct link:</b>\n{url}{footer}",
                                       parse_mode=ParseMode.HTML)
        elif kind == "music":
            url = data.get("music")
            if not url:
                raise ValueError("Audio not available for this video.")
            await q.message.reply_audio(audio=url, title=title, performer=author)
            await q.message.reply_text(f"🔗 <b>Direct audio link:</b>\n{url}{footer}",
                                       parse_mode=ParseMode.HTML)
        elif kind == "photo":
            idx = int(rest[1])
            imgs = data.get("images", [])
            if idx < len(imgs):
                await q.message.reply_photo(
                    photo=imgs[idx],
                    caption=f"🖼️ Photo {idx+1}/{len(imgs)}\n{title}{footer}")
                await q.message.reply_text(
                    f"🔗 <b>Direct photo link {idx+1}:</b>\n{imgs[idx]}{footer}",
                    parse_mode=ParseMode.HTML)
        elif kind == "album":
            imgs = data.get("images", [])[:10]  # Telegram limit: 10 per album
            if not imgs:
                raise ValueError("No photos available.")
            media = [InputMediaPhoto(
                media=u,
                caption=f"🖼️ Slides {i+1}/{len(data.get('images', []))}\n{title}{footer}"
                if i == 0 else "") for i, u in enumerate(imgs)]
            await q.message.reply_media_group(media=media)
            rest_imgs = data.get("images", [])[10:]
            for chunk_start in range(0, len(rest_imgs), 10):
                chunk = rest_imgs[chunk_start:chunk_start + 10]
                await q.message.reply_media_group(
                    media=[InputMediaPhoto(media=u) for u in chunk])

        track(q.from_user.id, "downloads")
        await q.answer("✅ Sent! Enjoy!")
    except Exception as e:
        log.exception("callback failed")
        try:
            await q.message.reply_text(
                f"❌ <b>Couldn't send the file.</b>\n<code>{e}</code>\n"
                f"Please try another option or resend the link.{channel_footer()}",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_choice))
    log.info("Bot v2 running (providers: tikwm -> vercel -> tdown). Waiting for links...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
