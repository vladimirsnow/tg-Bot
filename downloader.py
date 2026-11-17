"""Reuse yt-dlp imports across requests instead of restarting Python per job."""
import logging
import re
import time
import tempfile
from pathlib import Path

LOG = logging.getLogger(__name__)


def warmup():
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
    from yt_dlp.extractor.youtube import YoutubeIE, YoutubeSearchIE
    from yt_dlp.extractor.tiktok import TikTokIE
    from yt_dlp.extractor.soundcloud import SoundcloudSearchIE
    from gallery_dl.extractor.tiktok import TiktokExtractor
    return yt_dlp, ImpersonateTarget


class QuietLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        safe = re.sub(r"https?://\S+|\d+:[A-Za-z0-9_-]{20,}", "<hidden>", message)
        LOG.info("Загрузчик / %s", safe[:350])

    def error(self, message):
        pass


def extract(url, options=None, download=False, timeout=60, tiktok_data=None):
    yt_dlp, _ = warmup()
    deadline = time.monotonic() + timeout

    def progress(data):
        if time.monotonic() > deadline:
            raise TimeoutError("Download deadline exceeded")

    cache = Path(tempfile.gettempdir()) / "telegram-media-extractor-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    params = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "logger": QuietLogger(), "cachedir": str(cache), "noplaylist": True,
        "socket_timeout": 6, "retries": 0, "fragment_retries": 0,
        "source_address": "0.0.0.0",
        "extractor_retries": 0, "compat_opts": {"no-certifi"},
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
    with yt_dlp.YoutubeDL(params) as ydl:
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
        if not info:
            raise ValueError("No media returned")
        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise ValueError("Search returned no results")
        return info
