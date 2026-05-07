from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import yt_dlp
import httpx
import asyncio
import os
import re
from typing import Optional

app = FastAPI(title="MediaDrop API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ──
def format_number(n):
    if not n: return "—"
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f} juta"
    if n >= 1_000: return f"{n/1_000:.1f} rb"
    return str(n)

def format_duration(sec):
    if not sec: return None
    sec = int(sec)
    h, m, s = sec//3600, (sec%3600)//60, sec%60
    if h: return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"

def format_size(bytes_val):
    if not bytes_val: return "—"
    for unit in ['B','KB','MB','GB']:
        if bytes_val < 1024: return f"~{bytes_val:.0f} {unit}"
        bytes_val /= 1024
    return f"~{bytes_val:.1f} GB"

def detect_platform(url: str) -> str:
    url = url.lower()
    if "youtube" in url or "youtu.be" in url: return "YouTube"
    if "instagram" in url: return "Instagram"
    if "tiktok" in url: return "TikTok"
    if "twitter" in url or "x.com" in url: return "Twitter/X"
    if "facebook" in url or "fb.watch" in url: return "Facebook"
    if "pinterest" in url: return "Pinterest"
    if "vimeo" in url: return "Vimeo"
    if "reddit" in url: return "Reddit"
    if "dailymotion" in url: return "Dailymotion"
    return "Web"

# ── GET /info ──
@app.get("/info")
async def get_info(url: str = Query(..., description="URL media")):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    try:
        loop = asyncio.get_event_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await loop.run_in_executor(None, extract)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Tidak bisa mengambil info: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Thumbnail — ambil yang terbaik
    thumbnails = info.get("thumbnails", [])
    thumb_url = info.get("thumbnail", "")
    if thumbnails:
        best = sorted(thumbnails, key=lambda t: t.get("width", 0) or 0, reverse=True)
        thumb_url = best[0].get("url", thumb_url)

    # Build quality list dari formats
    formats = info.get("formats", [])
    qualities = []
    seen = set()

    # Video formats
    video_fmts = [
        f for f in formats
        if f.get("vcodec") != "none" and f.get("acodec") != "none"
        and f.get("height")
    ]
    video_fmts.sort(key=lambda f: f.get("height", 0), reverse=True)

    for f in video_fmts:
        h = f.get("height")
        label = f"{h}p"
        if label in seen: continue
        seen.add(label)
        qualities.append({
            "label": label,
            "format_id": f.get("format_id"),
            "ext": f.get("ext", "mp4").upper(),
            "size": format_size(f.get("filesize") or f.get("filesize_approx")),
            "type": "video"
        })

    # Video-only + audio merge (format bestvideo+bestaudio)
    if not qualities:
        video_only = [f for f in formats if f.get("vcodec") != "none" and f.get("height")]
        video_only.sort(key=lambda f: f.get("height", 0), reverse=True)
        for f in video_only[:5]:
            h = f.get("height")
            label = f"{h}p"
            if label in seen: continue
            seen.add(label)
            qualities.append({
                "label": label,
                "format_id": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]",
                "ext": "MP4",
                "size": format_size(f.get("filesize") or f.get("filesize_approx")),
                "type": "video"
            })
    else:
        # Tambahkan format merge untuk video-only streams (YouTube 1080p+)
        video_only = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") == "none" and f.get("height")]
        video_only.sort(key=lambda f: f.get("height", 0), reverse=True)
        extra_seen = set(q["label"] for q in qualities)
        for f in video_only[:3]:
            h = f.get("height")
            label = f"{h}p"
            if label in extra_seen: continue
            extra_seen.add(label)
            qualities.insert(0, {
                "label": label,
                "format_id": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]",
                "ext": "MP4",
                "size": format_size(f.get("filesize") or f.get("filesize_approx")),
                "type": "video"
            })
        # Sort by resolution descending
        def res_key(q):
            try: return int(q["label"].replace("p",""))
            except: return 0
        qualities.sort(key=res_key, reverse=True)

    # Audio only
    audio_fmts = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if audio_fmts:
        best_audio = sorted(audio_fmts, key=lambda f: f.get("abr") or 0, reverse=True)[0]
        qualities.append({
            "label": "MP3",
            "format_id": best_audio.get("format_id"),
            "ext": "MP3",
            "size": format_size(best_audio.get("filesize") or best_audio.get("filesize_approx")),
            "type": "audio"
        })

    # Fallback jika tidak ada format
    if not qualities:
        qualities = [{"label": "Best", "format_id": "best", "ext": "MP4", "size": "—", "type": "video"}]

    # Multi images (Instagram carousel dll)
    entries = info.get("entries", [])
    multi = []
    if entries:
        for entry in entries[:12]:
            t = entry.get("thumbnail") or (entry.get("thumbnails") or [{}])[-1].get("url", "")
            multi.append({"thumb": t, "url": entry.get("webpage_url", ""), "title": entry.get("title", "")})

    platform = detect_platform(url)

    return {
        "platform": platform,
        "title": info.get("title", "Tanpa judul"),
        "thumbnail": thumb_url,
        "duration": format_duration(info.get("duration")),
        "uploader": info.get("uploader") or info.get("channel") or "—",
        "upload_date": info.get("upload_date", ""),
        "view_count": format_number(info.get("view_count")),
        "like_count": format_number(info.get("like_count")),
        "qualities": qualities,
        "multi": multi if multi else None,
        "original_url": url
    }

# ── GET /download ──
@app.get("/download")
async def download_media(
    url: str = Query(...),
    format_id: str = Query("best"),
    audio_only: bool = Query(False)
):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": "/usr/bin/ffmpeg",
    }

    if audio_only or format_id == "MP3":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "outtmpl": "/tmp/%(id)s.%(ext)s",
        })
    else:
        # Gunakan format_id jika spesifik, fallback ke best merge
        fmt = format_id if format_id not in ["best", ""] else "bestvideo+bestaudio/best"
        ydl_opts.update({
            "format": fmt,
            "merge_output_format": "mp4",
            "outtmpl": "/tmp/%(id)s.%(ext)s",
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        })

    try:
        loop = asyncio.get_event_loop()
        def do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info)
        filepath = await loop.run_in_executor(None, do_download)

        # Cari file hasil (ekstensi bisa berubah)
        base = os.path.splitext(filepath)[0]
        actual = filepath
        for ext in [".mp4", ".mp3", ".webm", ".mkv", ".m4a"]:
            candidate = base + ext
            if os.path.exists(candidate):
                actual = candidate
                break

        if not os.path.exists(actual):
            raise HTTPException(status_code=500, detail="File tidak ditemukan setelah download")

        filename = os.path.basename(actual)
        ext = os.path.splitext(actual)[1].lower()
        media_type = "audio/mpeg" if ext == ".mp3" else "video/mp4"

        def iterfile():
            with open(actual, "rb") as f:
                yield from f
            os.remove(actual)  # cleanup

        return StreamingResponse(
            iterfile(),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── GET /thumbnail ──
@app.get("/thumbnail")
async def download_thumbnail(url: str = Query(...)):
    ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    try:
        loop = asyncio.get_event_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await loop.run_in_executor(None, extract)
        thumbnails = info.get("thumbnails", [])
        thumb_url = info.get("thumbnail", "")
        if thumbnails:
            best = sorted(thumbnails, key=lambda t: t.get("width", 0) or 0, reverse=True)
            thumb_url = best[0].get("url", thumb_url)
        if not thumb_url:
            raise HTTPException(status_code=404, detail="Thumbnail tidak ditemukan")

        async with httpx.AsyncClient() as client:
            r = await client.get(thumb_url)
            content_type = r.headers.get("content-type", "image/jpeg")
            title = re.sub(r'[^\w\s-]', '', info.get("title", "thumbnail"))[:50]
            return StreamingResponse(
                iter([r.content]),
                media_type=content_type,
                headers={"Content-Disposition": f'attachment; filename="{title}.jpg"'}
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Health check ──
@app.get("/")
async def root():
    return {"status": "ok", "service": "MediaDrop API", "version": "1.0.0"}
