"""Reuse yt-dlp imports across requests instead of restarting Python per job."""
import logging
import os
import re
import time
import tempfile
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from youtube_session import session_copy

LOG = logging.getLogger(__name__)


class DownloadFailure(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def warmup():
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
    from yt_dlp.extractor.youtube import YoutubeIE, YoutubeSearchIE
    from yt_dlp.extractor.soundcloud import SoundcloudSearchIE
    if os.getenv("MEDIA_MUSIC_WORKER") != "1":
        from yt_dlp.extractor.tiktok import TikTokIE
        from gallery_dl.extractor.tiktok import TiktokExtractor
    return yt_dlp, ImpersonateTarget


class QuietLogger:
    def __init__(self, deadline=float("inf"), cancel_event=None):
        self.started = time.monotonic()
        self.deadline = deadline
        self.cancel_event = cancel_event
        self.failure_reason = None

    def guard(self):
        if time.monotonic() > self.deadline or (self.cancel_event and self.cancel_event.is_set()):
            raise TimeoutError("Download deadline exceeded")

    def debug(self, message):
        self.guard()
        if "Downloading" in message and any(part in message for part in ("webpage", "player", "API JSON", "m3u8")):
            safe = re.sub(r"https?://\S+", "<hidden>", message)
            LOG.info("Загрузчик / %.1f с: %s", time.monotonic() - self.started, safe[:220])

    def warning(self, message):
        self.guard()
        if "cookies are no longer valid" in message:
            self.failure_reason = "youtube_cookies_expired"
        elif "returncode: -9" in message and not self.failure_reason:
            self.failure_reason = "javascript_process_killed"
        safe = re.sub(r"https?://\S+|\d+:[A-Za-z0-9_-]{20,}", "<hidden>", message)
        LOG.info("Загрузчик / %s", safe[:350])

    def error(self, message):
        self.guard()


def extract(url, options=None, download=False, timeout=60, tiktok_data=None, cancel_event=None,
            playlist=False, playlist_limit=3):
    deadline = time.monotonic() + timeout
    logger = QuietLogger(deadline, cancel_event)
    logger.guard()
    yt_dlp, _ = warmup()

    def progress(data):
        logger.guard()
        if data.get("status") == "finished":
            LOG.info("Загрузчик / файл скачан за %.1f с", time.monotonic() - deadline + timeout)

    cache = Path(tempfile.gettempdir()) / "telegram-media-extractor-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    params = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": logger, "cachedir": str(cache), "noplaylist": True,
        "socket_timeout": 6, "retries": 0, "fragment_retries": 0,
        "source_address": "0.0.0.0",
        "extractor_retries": 0, "compat_opts": {"no-certifi"},
        "concurrent_fragment_downloads": 4,
        "js_runtimes": {"deno": {}}, "progress_hooks": [progress],
    }
    params.update(options or {})
    if params.get("cookiefile") and "extractor_args" not in params:
        params["extractor_args"] = {"youtube": {"player_client": ["web"]}}
    if (url.startswith(("https://www.youtube.com/", "https://youtube.com/", "ytsearch"))
            and not params.get("cookiefile") and "extractor_args" not in params):
        params["extractor_args"] = {"youtube": {
            "player_client": ["visionos"],
            "innertube_host": ["youtubei.googleapis.com"],
        }}
    with ExitStack() as stack:
        is_youtube = url.startswith("ytsearch") or urlsplit(url).hostname in {
            "youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}
        if is_youtube and params.get("cookiefile"):
            params["cookiefile"] = stack.enter_context(session_copy(
                params["cookiefile"], lambda: logger.failure_reason != "youtube_cookies_expired"))
        # Register this after session_copy so yt-dlp saves response cookies first.
        ydl = stack.enter_context(yt_dlp.YoutubeDL(params))
        try:
            if tiktok_data:
                from yt_dlp.extractor.tiktok import TikTokIE
                for cookie in tiktok_data.get("__tiktok_cookies", ()):
                    ydl.cookiejar.set_cookie(cookie)
                author = (tiktok_data.get("author") or {}).get("uniqueId") or tiktok_data.get("user")
                canonical = f"https://www.tiktok.com/@{author}/video/{tiktok_data['id']}" if author else url
                parsed = TikTokIE(ydl)._parse_aweme_video_web(tiktok_data, canonical, str(tiktok_data["id"]))
                info = ydl.process_ie_result(parsed, download=download)
            else:
                info = ydl.extract_info(url, download=download)
        except Exception:
            if logger.failure_reason:
                raise DownloadFailure(logger.failure_reason) from None
            raise
        logger.guard()
        if not info:
            raise ValueError("No media returned")
        if "entries" in info:
            if playlist:
                from itertools import islice
                info["entries"] = list(islice((entry for entry in info["entries"] if entry), playlist_limit))
            else:
                info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise ValueError("Search returned no results")
        return info
