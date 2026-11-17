import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps

from links import Link, parse_link
from settings import Settings

LOG = logging.getLogger(__name__)
MIB = 1024 * 1024
UPLOAD_LIMIT = 49 * MIB
SOURCE_LIMIT = 100 * MIB
MAX_PHOTOS = 35
SEARCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-search")
CDNS = {
    "instagram": ("cdninstagram.com", "fbcdn.net", "fbsbx.com"),
    "tiktok": ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
               "tiktokv.com", "tiktokv.us", "tiktokv.eu", "tiktokwm.com", "tikwm.com",
               "byteoversea.com", "ibytedtos.com", "muscdn.com", "snssdk.com"),
    "twitter": ("twimg.com",),
    "youtube": ("googlevideo.com", "googleusercontent.com"),
    "soundcloud": ("sndcdn.com", "soundcloud.com", "soundcloud.cloud"),
    "audius": ("audius.co", "r2.cloudflarestorage.com"),
}


class MediaError(Exception):
    pass


def error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, MediaError):
        return str(exc)[:360]
    return type(exc).__name__


@dataclass
class Attachment:
    kind: str
    path: Path | None
    title: str = ""
    artist: str = ""
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    remote_url: str | None = None
    platform: str | None = None
    referer: str = ""


@dataclass
class Post:
    visual: list[Attachment] = field(default_factory=list)
    audio: list[Attachment] = field(default_factory=list)
    incomplete: bool = False
    audio_source: Link | None = None


@dataclass(frozen=True)
class Remote:
    kind: str
    url: str
    title: str = ""
    artist: str = ""


def text(value) -> str:
    if isinstance(value, list):
        return ", ".join(item for item in value if isinstance(item, str))[:128]
    return value[:128] if isinstance(value, str) else ""


def run(args: list[str], timeout: int) -> str:
    env = {key: value for key, value in os.environ.items() if key != "TELEGRAM_BOT_TOKEN"}
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise MediaError(f"{args[0]}: {type(exc).__name__}") from None
    if result.returncode:
        error = result.stderr.lower()
        if any(item in error for item in ("sign in", "login", "cookies", "429", "403", "captcha")):
            raise MediaError(f"{args[0]}: источник ограничил доступ")
        hint = next((line for line in reversed(result.stderr.splitlines())
                     if line.lstrip().startswith("ERROR:")), "")
        hint = re.sub(r"https?://\S+|\d+:[A-Za-z0-9_-]{20,}", "<hidden>", hint)
        raise MediaError(f"{args[0]}: {hint[:240] or f'код {result.returncode}'}")
    return result.stdout


def cdn_url(url: str, platform: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (parts.scheme == "https" and parts.username is None and parts.password is None
                and parts.port in (443, None) and any(host == domain or host.endswith("." + domain)
                                                      for domain in CDNS[platform]))
    except ValueError:
        return False


def fetch(client: httpx.Client, url: str, platform: str, target: Path, limit: int) -> None:
    deadline = time.monotonic() + 45
    for _ in range(5):
        if not cdn_url(url, platform) or time.monotonic() > deadline:
            raise MediaError("Недоступный адрес медиа")
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            size_header = response.headers.get("content-length", "")
            if size_header.isdecimal() and int(size_header) > limit:
                raise MediaError("Исходный файл слишком большой")
            received = 0
            with target.open("wb") as target_file:
                for chunk in response.iter_bytes(128 * 1024):
                    received += len(chunk)
                    if received > limit or time.monotonic() > deadline:
                        raise MediaError("Превышен лимит скачивания")
                    target_file.write(chunk)
            if received == 0:
                raise MediaError("Пустой ответ источника")
            return
    raise MediaError("Слишком много перенаправлений")


def as_photo(source: Path, target: Path) -> Attachment:
    with Image.open(source) as image:
        image.draft("RGB", (2000, 2000))
        if image.width * image.height > 28_000_000:
            raise MediaError("Слишком большое разрешение фото")
        image = ImageOps.exif_transpose(image)
        image.thumbnail((2000, 2000))
        width, height = image.size
        if max(width, height) > 19 * min(width, height):
            padded = Image.new("RGB", (max(width, math.ceil(height / 19)),
                                       max(height, math.ceil(width / 19))), "black")
            padded.paste(image.convert("RGB"), ((padded.width - width) // 2, (padded.height - height) // 2))
            image = padded
        image.convert("RGB").save(target, "JPEG", quality=88)
    return Attachment("photo", target)


def as_audio(source: Path, target: Path, title: str = "", artist: str = "") -> Attachment:
    probe = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=codec_name", "-of", "json", str(source)], 10))
    streams = probe.get("streams") or []
    codec = streams[0].get("codec_name") if streams else None
    if codec in {"mp3", "aac"} and source.stat().st_size <= UPLOAD_LIMIT:
        target = target.with_suffix(".mp3" if codec == "mp3" else ".m4a")
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
             "-map", "0:a:0", "-vn", "-c:a", "copy", str(target)], 20)
        if 0 < target.stat().st_size <= UPLOAD_LIMIT:
            return Attachment("audio", target, title or "Аудио", artist)
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "1", "-i", str(source),
         "-map", "0:a:0", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", "160k",
         "-threads", "1", str(target)], 100)
    if not 0 < target.stat().st_size <= UPLOAD_LIMIT:
        raise MediaError("Аудио превышает лимит Telegram")
    return Attachment("audio", target, title or "Аудио", artist)


def as_video(source: Path, target: Path) -> Attachment:
    probe = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(source)], 15))
    streams = probe.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video or min(int(video.get("width", 0)), int(video.get("height", 0))) < 2:
        raise MediaError("Видеодорожка отсутствует")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    try:
        duration = float(probe.get("format", {}).get("duration") or video.get("duration", 0))
    except (TypeError, ValueError):
        duration = 0
    if not math.isfinite(duration) or duration < 0:
        duration = 0
    rotation = next((data["rotation"] for data in video.get("side_data_list", []) if "rotation" in data), 0)
    width, height = video["width"], video["height"]
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-filter_threads", "1", "-threads", "1",
               "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1"]
    if (source.stat().st_size <= UPLOAD_LIMIT and video.get("codec_name") == "h264"
            and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
            and (not audio or audio.get("codec_name") == "aac")):
        if rotation % 180:
            width, height = height, width
        run([*command, "-c", "copy", "-movflags", "+faststart", str(target)], 45)
        if 0 < target.stat().st_size <= UPLOAD_LIMIT:
            return Attachment("video", target, width=width, height=height, duration=round(duration))

    display_width, display_height = (video["height"], video["width"]) if rotation % 180 else (
        video["width"], video["height"])
    video_kbps = min(1800, int(UPLOAD_LIMIT * 0.92 * 8 / max(1, duration) / 1000 - 96))
    if video_kbps < 160:
        raise MediaError("Видео слишком длинное для лимита Telegram")
    max_edge = 720 if video_kbps >= 650 else 480
    factor = min(1.0, max_edge / max(display_width, display_height))
    width = max(2, int(display_width * factor) // 2 * 2)
    height = max(2, int(display_height * factor) // 2 * 2)
    run([*command, "-vf", f"scale={width}:{height}", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
         "-bufsize", f"{video_kbps * 2}k", "-c:a", "aac", "-b:a", "96k", "-threads", "1",
         "-movflags", "+faststart", str(target)], 175)
    if not 0 < target.stat().st_size <= UPLOAD_LIMIT:
        raise MediaError("Видео превышает лимит Telegram")
    return Attachment("video", target, width=width, height=height, duration=round(duration))


def tiktok_rows(url: str, depth: int = 0):
    from gallery_dl import extractor
    if depth > 3:
        raise MediaError("Слишком много перенаправлений TikTok")
    source = extractor.find(url)
    if source is None:
        raise MediaError("Не найден обработчик TikTok")
    source.log = logging.Logger("gallery-private", level=logging.CRITICAL)
    source.initialize()
    source._retries = 0
    source._timeout = 6
    try:
        for row in source.items():
            if row[0] == 6:
                child = parse_link(row[1])
                if child and child.platform == "tiktok":
                    yield from tiktok_rows(child.url, depth + 1)
            else:
                yield row
    finally:
        source.session.close()


def gallery(link: Link, cookies: Path | None) -> list[Remote]:
    args = ["gallery-dl", "--config-ignore", "--http-timeout", "12", "--retries", "0", "-J"]
    if link.platform == "instagram":
        args += ["-o", "extractor.instagram.audio=merged", "-o", "extractor.instagram.videos=merged"]
        if cookies:
            args += ["--cookies", str(cookies)]
    if link.platform == "tiktok":
        data = tiktok_rows(link.url)
    else:
        data = json.loads(run([*args, "--", link.url], 42))
    result = []
    for row in data:
        if len(row) != 3 or row[0] != 3:
            continue
        _, url, metadata = row
        if link.platform == "instagram":
            if metadata.get("audio_url") == url:
                kind = "audio"
            elif (metadata.get("video_url") == url or metadata.get("extension") in {"mp4", "webm"}
                  or urlsplit(url).path.lower().endswith((".mp4", ".webm"))):
                kind = "video"
            else:
                kind = "photo"
            result.append(Remote(kind, url, text(metadata.get("audio_title")),
                                 text(metadata.get("audio_artist") or metadata.get("audio_user"))))
        else:
            kind = {"image": "photo", "video": "video", "audio": "audio"}.get(metadata.get("type"))
            if kind:
                music = metadata.get("music") or {}
                result.append(Remote(kind, url, text(music.get("title")), text(music.get("authorName"))))
    return result


def photo_audio(link: Link, directory: Path, settings: Settings) -> Attachment:
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=6, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for provider in (lambda: gallery(link, None), lambda: tikwm(client, link)):
            try:
                for item in provider():
                    if item.kind == "audio" and cdn_url(item.url, "tiktok"):
                        remote = Attachment("audio", None, item.title, item.artist,
                                            remote_url=item.url, platform="tiktok", referer=link.url)
                        return materialize_remote(remote, directory)
            except Exception as exc:
                LOG.info("TikTok / резерв звука: %s", error_detail(exc))
    return ytdlp(link.url, directory, None, audio=True, timeout=30)


def instagram_embed(client: httpx.Client, link: Link) -> list[Remote]:
    response = client.get(link.url + "embed/")
    response.raise_for_status()
    match = re.search(r'"contextJSON"\s*:\s*', response.text)
    if not match:
        return []
    raw, _ = json.JSONDecoder().raw_decode(response.text, match.end())
    if not isinstance(raw, str):
        return []
    post = json.loads(raw).get("gql_data", {}).get("shortcode_media") or {}
    if post.get("shortcode") != link.post_id:
        return []
    edges = (post.get("edge_sidecar_to_children") or {}).get("edges") or []
    entries = [edge["node"] for edge in edges] if edges else [post]
    result = []
    for entry in entries:
        kind = "video" if entry.get("is_video") else "photo"
        url = entry.get("video_url" if kind == "video" else "display_url")
        if url:
            result.append(Remote(kind, url))
    return result


def tikwm(client: httpx.Client, link: Link) -> list[Remote]:
    response = client.get("https://www.tikwm.com/api/", params={"url": link.url}, timeout=5)
    response.raise_for_status()
    data = response.json().get("data") or {}
    if images := data.get("images"):
        result = [Remote("photo", urljoin("https://www.tikwm.com/", url))
                  for url in images if isinstance(url, str)]
        music = data.get("music_info") or {}
        if url := music.get("play") or data.get("music"):
            result.append(Remote("audio", urljoin("https://www.tikwm.com/", url),
                                 text(music.get("title")), text(music.get("author"))))
        return result
    if not link.is_photo and (url := data.get("play") or data.get("wmplay")):
        return [Remote("video", urljoin("https://www.tikwm.com/", url))]
    return []


def twitter(client: httpx.Client, link: Link) -> list[Remote]:
    response = client.get(f"https://api.fxtwitter.com/2/status/{link.post_id}",
                          headers={"User-Agent": "TelegramMediaBot/1.0", "Referer": ""})
    response.raise_for_status()
    tweet = response.json().get("status") or {}
    media = tweet.get("media") or {}
    items = media.get("all") or [*media.get("photos", []), *media.get("videos", [])]
    return [Remote("photo" if item.get("type") == "photo" else "video", item["url"])
            for item in items if item.get("url") and item.get("type") in {"photo", "video", "gif"}]


def ytdlp(url: str, directory: Path, cookies: Path | None, audio: bool = False,
          timeout: int = 90) -> Attachment:
    from downloader import extract, warmup
    _, target_type = warmup()
    options = {"outtmpl": str(directory / "download.%(ext)s"),
               "max_filesize": SOURCE_LIMIT, "windowsfilenames": True,
               "merge_output_format": "mp4"}
    link = parse_link(url)
    if link and link.platform in {"tiktok", "instagram", "youtube"}:
        options["impersonate"] = target_type.from_str("chrome")
    if cookies:
        cookie_copy = directory / "session.txt"
        shutil.copyfile(cookies, cookie_copy)
        cookie_copy.chmod(0o600)
        options["cookiefile"] = str(cookie_copy)
    options["format"] = ("bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best" if audio else
        "b[height<=480][ext=mp4][vcodec^=avc1][acodec^=mp4a]/"
        "bv*[height<=480][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
        "b[height<=480][ext=mp4]/bv*[height<=480][ext=mp4]+ba[ext=m4a]/"
        "bv*[height<=720][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/b[ext=mp4]/b")
    if not audio and link and link.platform == "tiktok":
        options["format"] = ("b[vcodec=h264][filesize<=?49M]/b[vcodec^=avc1][filesize<=?49M]/"
                             "b[vcodec=h264]/b[vcodec^=avc1]/b[ext=mp4]/b")
    def match(info, *, incomplete=False):
        duration = info.get("duration") or 0
        if info.get("is_live") or duration > (1200 if audio else 900):
            return "Media exceeds duration limit"
    options["match_filter"] = match
    try:
        info = extract(url, options, download=True, timeout=timeout)
    except Exception as exc:
        hint = re.sub(r"https?://\S+|\d+:[A-Za-z0-9_-]{20,}", "<hidden>", str(exc))
        raise MediaError(f"yt-dlp: {hint[:340]}") from None
    source = next((path for path in directory.glob("download.*") if path.is_file()
                   and path.suffix.lower() in {".mp4", ".m4a", ".mp3", ".webm", ".ogg", ".opus", ".mkv", ".aac"}), None)
    if not source or not 0 < source.stat().st_size <= SOURCE_LIMIT:
        raise MediaError("Медиа отсутствует или превышает лимит")
    if audio:
        title = text(info.get("track") or info.get("title"))
        artist = text(info.get("artist") or info.get("creator") or info.get("uploader"))
        if source.suffix.lower() in {".m4a", ".mp3"} and source.stat().st_size <= UPLOAD_LIMIT:
            return Attachment("audio", source, title, artist)
        return as_audio(source, directory / "track.mp3", title, artist)
    return as_video(source, directory / "video.mp4")


def download_items(client: httpx.Client, platform: str, items: list[Remote], directory: Path,
                   referer: str, fast: bool) -> Post:
    result = Post()
    seen = set()
    photos = 0
    has_photos = any(item.kind == "photo" for item in items)
    for index, item in enumerate(items):
        if item.url in seen or (item.kind == "audio" and not has_photos):
            continue
        seen.add(item.url)
        if item.kind == "photo":
            photos += 1
            if photos > MAX_PHOTOS:
                result.incomplete = True
                continue
        if item.kind not in {"photo", "video", "audio"} or not cdn_url(item.url, platform):
            result.incomplete = True
            continue
        if not fast:
            raw = directory / f"source-{index}"
            try:
                fetch(client, item.url, platform, raw,
                      15 * MIB if item.kind == "photo" else SOURCE_LIMIT)
                if item.kind == "photo":
                    attachment = as_photo(raw, directory / f"photo-{index}.jpg")
                elif item.kind == "video":
                    attachment = as_video(raw, directory / f"video-{index}.mp4")
                else:
                    attachment = as_audio(raw, directory / f"audio-{index}.mp3", item.title, item.artist)
                (result.audio if item.kind == "audio" else result.visual).append(attachment)
            except (MediaError, httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError) as exc:
                LOG.info("%s %s / загрузка: %s", platform, item.kind, error_detail(exc))
                result.incomplete = True
            finally:
                raw.unlink(missing_ok=True)
            continue
        attachment = Attachment(item.kind, None, title=item.title, artist=item.artist,
                                remote_url=item.url, platform=platform, referer=referer)
        (result.audio if item.kind == "audio" else result.visual).append(attachment)
    return result


def materialize_remote(attachment: Attachment, directory: Path, index: int = 0) -> Attachment:
    if not attachment.remote_url or not attachment.platform:
        raise MediaError("Удалённый файл недоступен")
    raw = directory / f"remote-source-{index}"
    extension = {"photo": "photo.jpg", "video": "video.mp4", "audio": "audio.mp3"}[attachment.kind]
    target = directory / f"remote-{index}-{extension}"
    headers = {"User-Agent": "Mozilla/5.0"}
    if attachment.referer:
        headers["Referer"] = attachment.referer
    with httpx.Client(timeout=12, headers=headers) as client:
        limit = 15 * MIB if attachment.kind == "photo" else SOURCE_LIMIT
        fetch(client, attachment.remote_url, attachment.platform, raw, limit)
    try:
        if attachment.kind == "photo":
            return as_photo(raw, target)
        if attachment.kind == "video":
            return as_video(raw, target)
        return as_audio(raw, target, attachment.title, attachment.artist)
    finally:
        raw.unlink(missing_ok=True)


def recover_post(link: Link, directory: Path, settings: Settings) -> Post:
    """Try a fresh extractor before repeating providers whose CDN URL failed."""
    directory.mkdir(parents=True, exist_ok=True)
    cookies = {"youtube": settings.youtube_cookies, "instagram": settings.instagram_cookies}.get(link.platform)
    if not link.is_photo:
        native = directory / "native"
        native.mkdir(exist_ok=True)
        try:
            return Post([ytdlp(link.url, native, cookies)])
        except (MediaError, OSError, ValueError) as exc:
            LOG.info("%s / прямой загрузчик: %s", link.platform, error_detail(exc))
    providers = directory / "providers"
    providers.mkdir(exist_ok=True)
    return download_post(link, providers, settings, fast=False)


def load_post(link: Link, directory: Path, settings: Settings) -> Post:
    """Recovery also covers extraction failures, before any media was sent."""
    try:
        return download_post(link, directory, settings)
    except (MediaError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("%s / первый путь: %s; запускаю резервный", link.platform, error_detail(exc))
        return recover_post(link, directory / "extraction-recovery", settings)


def download_post(link: Link, directory: Path, settings: Settings, fast: bool = True) -> Post:
    cookies = {"youtube": settings.youtube_cookies, "instagram": settings.instagram_cookies}.get(link.platform)
    photo_fallback = None
    if link.platform == "youtube":
        return Post([ytdlp(link.url, directory, cookies)])
    with httpx.Client(timeout=12, headers={"User-Agent": "Mozilla/5.0", "Referer": link.url}) as client:
        if link.platform == "instagram" and not link.post_id:
            for _ in range(4):
                with client.stream("GET", link.url, follow_redirects=False) as response:
                    redirect = response.headers.get("location")
                    target = parse_link(urljoin(link.url, redirect)) if redirect else None
                if not target or target.platform != "instagram" or target.url == link.url:
                    break
                link = target
                if link.post_id:
                    break
            if not link.post_id:
                raise MediaError("Не удалось раскрыть короткую ссылку Instagram")
        providers = {
            "instagram": [lambda: instagram_embed(client, link), lambda: gallery(link, cookies)],
            "tiktok": [lambda: gallery(link, None), lambda: tikwm(client, link)],
            "twitter": [lambda: twitter(client, link)],
            "youtube": [],
        }
        for index, provider in enumerate(providers[link.platform]):
            target = directory / f"provider-{index}"
            target.mkdir(exist_ok=True)
            try:
                provider_started = time.monotonic()
                items = provider()
                LOG.info("%s / источник %s вернул %s файлов за %.1f с", link.platform, index + 1,
                         len(items),
                         time.monotonic() - provider_started)
                post = download_items(client, link.platform, items, target, link.url, fast)
                if not post.visual:
                    continue
                photos = [item for item in post.visual if item.kind == "photo"]
                if link.platform == "tiktok" and photos:
                    post.audio_source = link
                if photo_fallback and not photos:
                    continue
                if fast and link.platform == "tiktok" and len(post.visual) == 1 and not photos:
                    native = directory / "tiktok-native"
                    native.mkdir(exist_ok=True)
                    try:
                        return Post([ytdlp(link.url, native, None, timeout=45)])
                    except (MediaError, OSError, ValueError) as exc:
                        LOG.info("TikTok / загрузка с заголовками: %s", error_detail(exc))
                if not link.is_photo or photos:
                    return post
            except (MediaError, httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as exc:
                LOG.info("%s / источник %s: %s", link.platform, index + 1, error_detail(exc))
    if photo_fallback:
        photo_fallback.incomplete = True
        return photo_fallback
    if not link.is_photo:
        return Post([ytdlp(link.url, directory, cookies)])
    raise MediaError("Источник не выдал фото")


def download_track(query: str, directory: Path, settings: Settings) -> Attachment:
    futures = {}
    for search, platform, media_format in (
        ("ytsearch1", "youtube", "bestaudio[ext=m4a]"),
        ("scsearch1", "soundcloud", "bestaudio[ext=mp3]/bestaudio"),
    ):
        future = SEARCH_POOL.submit(track_remote, query, search, platform, media_format,
                                    settings.youtube_cookies)
        futures[future] = platform
    deadline = time.monotonic() + 12
    pending = set(futures)
    try:
        while pending:
            done, pending = wait(pending, timeout=max(0, deadline - time.monotonic()),
                                 return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                try:
                    return future.result()
                except Exception as exc:
                    LOG.info("Поиск трека / быстрый %s: %s", futures[future], error_detail(exc))
    finally:
        for future in pending:
            future.cancel()
    return download_track_local(query, directory, settings)


def track_remote(query: str, search: str, platform: str, media_format: str,
                 cookies: Path | None) -> Attachment:
    from downloader import extract, warmup
    options = {"format": media_format}
    if platform == "youtube":
        options["impersonate"] = warmup()[1].from_str("chrome")
    if platform == "youtube" and cookies:
        options["cookiefile"] = str(cookies)
    try:
        info = extract(f"{search}:{query}", options, download=False, timeout=15)
    except Exception as exc:
        raise MediaError(f"Поиск {platform}: {type(exc).__name__}") from None
    media_url = info.get("url") or ""
    if not cdn_url(media_url, platform) or ".m3u8" in urlsplit(media_url).path.lower():
        raise MediaError("Источник не выдал прямой аудиофайл")
    return Attachment("audio", None, title=text(info.get("track") or info.get("title")) or query,
                      artist=text(info.get("artist") or info.get("uploader")),
                      remote_url=media_url, platform=platform,
                      referer=info.get("webpage_url") or "https://www.youtube.com/")


def download_track_local(query: str, directory: Path, settings: Settings) -> Attachment:
    for name, cookies in (("ytsearch1", settings.youtube_cookies), ("scsearch1", None)):
        source = directory / name
        source.mkdir(exist_ok=True)
        try:
            return ytdlp(f"{name}:{query}", source, cookies, audio=True)
        except (MediaError, OSError, ValueError) as exc:
            LOG.info("Поиск трека / %s: %s", name, type(exc).__name__)
    with httpx.Client(timeout=12, headers={"User-Agent": "TelegramMediaBot/1.0"}) as client:
        response = client.get("https://api.audius.co/v1/tracks/search",
                              params={"query": query, "limit": 8, "app_name": "telegram-media-bot"})
        response.raise_for_status()
        words = set(re.findall(r"\w+", query.casefold()))

        def rank(track: dict) -> tuple:
            title = set(re.findall(r"\w+", text(track.get("title")).casefold()))
            artist = set(re.findall(r"\w+", text((track.get("user") or {}).get("name")).casefold()))
            return (not words.issubset(title | artist), len(title - words), -len(words & artist))

        for track in sorted(response.json().get("data") or [], key=rank):
            track_id = str(track.get("id", ""))
            if not track.get("is_streamable") or not re.fullmatch(r"[A-Za-z0-9]+", track_id):
                continue
            raw = directory / "audius-source"
            try:
                fetch(client, f"https://api.audius.co/v1/tracks/{track_id}/stream?app_name=telegram-media-bot",
                      "audius", raw, UPLOAD_LIMIT)
                return as_audio(raw, directory / "audius.mp3", text(track.get("title")) or query,
                                text((track.get("user") or {}).get("name")))
            except (MediaError, httpx.HTTPError, OSError) as exc:
                LOG.info("Поиск трека / Audius: %s", type(exc).__name__)
    raise MediaError("Трек не найден")
