import asyncio
import json
import hashlib
import hmac
import http.cookiejar
import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import ExitStack
from contextvars import ContextVar
from collections import OrderedDict
from dataclasses import dataclass, replace, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory, NamedTemporaryFile
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from telegram import Bot, Chat, InputFile, InputMediaPhoto, InputMediaVideo, Message, MessageEntity
from telegram import ReplyParameters, Update
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from links import Link, links_in, music_query, parse_link
from media import (Attachment, MediaError, Post, download_post, download_track,
                   download_track_local, error_detail, materialize_remote, recover_post, load_post, photo_audio)
from settings import Settings
from runtime import WebhookRuntime
from music_engine import MusicEngine

LOG = logging.getLogger(__name__)
BUILD = "2026.11.19.32"
MUSIC_TIMEOUT = 45
MUSIC_STATE = ContextVar("music_state", default=None)


class ProbeLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        if "OWNER_USER_ID" in record.getMessage():
            return
        line = re.sub(r"https?://\S+|\d+:[A-Za-z0-9_-]{20,}", "<hidden>", record.getMessage())
        self.lines.append(line[:400])
        self.lines[:] = self.lines[-40:]


class HealthHandler(BaseHTTPRequestHandler):
    runtime: WebhookRuntime | None = None
    application: Application | None = None
    media_bot = None
    loop = None
    probe_future = None
    probe_result = "not_run"
    probe_logs = []
    probe_lock = threading.Lock()

    def do_POST(self) -> None:
        if self.path == "/telegram":
            self.telegram_update()
            return
        worker = self.media_bot
        if self.path not in ("/selftest", "/youtube-cookies") or worker is None:
            self.send_error(404)
            return
        if not self.probe_authorized():
            self.send_error(403)
            return
        if self.path == "/youtube-cookies":
            cookie_path = None
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 131072:
                    raise ValueError()
                data = self.rfile.read(size).decode()
                with NamedTemporaryFile(mode="w", prefix="telegram-youtube-", suffix=".txt", delete=False) as handle:
                    handle.write(data)
                    cookie_path = Path(handle.name)
                jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
                jar.load(ignore_discard=True, ignore_expires=False)
                if not jar or any(c.domain.lstrip(".") not in {"youtube.com", "www.youtube.com", "music.youtube.com"}
                                  for c in jar):
                    raise ValueError()
                worker.settings = replace(worker.settings, youtube_cookies=cookie_path)
                self.send_response(204)
                self.end_headers()
                return
            except Exception:
                if cookie_path:
                    cookie_path.unlink(missing_ok=True)
                self.send_error(400)
                return
        if not self.application or not self.application.running or not worker.settings.owner_user_id:
            self.send_error(503)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 4096:
                raise ValueError()
            payload = json.loads(self.rfile.read(size)) if size else {}
            link = parse_link(payload.get("url", "https://vt.tiktok.com/ZSpGEBawN/"))
            query = payload.get("track")
            if query is not None:
                if not isinstance(query, str) or not 0 < len(query) <= 200:
                    raise ValueError()
                link = None
            elif not link:
                raise ValueError()
            target_chat_id = payload.get("chat_id", worker.settings.owner_user_id)
            if type(target_chat_id) is not int or (
                    target_chat_id != worker.settings.owner_user_id and target_chat_id not in worker.settings.chat_ids):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            self.send_error(400)
            return
        with self.probe_lock:
            if HealthHandler.probe_future and not HealthHandler.probe_future.done():
                self.send_error(409)
                return
            HealthHandler.probe_result = "running"
            HealthHandler.probe_logs = []
            HealthHandler.probe_future = asyncio.run_coroutine_threadsafe(worker.selftest(link, query, target_chat_id), self.loop)
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"started\n")

    def telegram_update(self) -> None:
        runtime = self.runtime
        if runtime is None:
            self.send_error(404)
            return
        supplied = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(runtime.secret.encode(), supplied.encode()):
            self.send_error(403)
            return
        if not runtime.ready or runtime.loop is None:
            self.send_error(503)
            return
        future = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1048576:
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            future = asyncio.run_coroutine_threadsafe(runtime.accept(payload), runtime.loop)
            future.result(timeout=5)
        except (ValueError, TypeError, KeyError):
            self.send_error(400)
            return
        except (FutureTimeout, RuntimeError, asyncio.QueueFull):
            if future:
                future.cancel()
            self.send_error(503)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def probe_authorized(self) -> bool:
        if self.media_bot is None:
            return False
        stamp = self.headers.get("X-Bot-Probe-Time", "")
        signature = self.headers.get("X-Bot-Probe-Signature", "")
        expected = hmac.new(self.media_bot.settings.token.encode(), stamp.encode(), hashlib.sha256).hexdigest()
        valid_time = stamp.isdecimal() and len(stamp) <= 12 and abs(time.time() - int(stamp)) <= 60
        return valid_time and hmac.compare_digest(expected, signature)

    def do_GET(self) -> None:
        if self.path == "/selftest":
            if not self.probe_authorized():
                self.send_error(403)
                return
            payload = json.dumps({"result": HealthHandler.probe_result, "logs": HealthHandler.probe_logs,
                                  "allowed_chat_ids": sorted(self.media_bot.settings.chat_ids),
                                  "music_jobs": [{"stage": item["stage"],
                                                  "elapsed": round(time.monotonic() - item["started"], 1)}
                                                 for item in list(self.media_bot.active_music.values())]},
                                 ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if self.path == "/status":
            state = {"build": BUILD,
                                  "selftest": HealthHandler.probe_result,
                                  "application_running": bool(self.application and self.application.running)}
            state.update(self.runtime.snapshot() if self.runtime else {"mode": "polling"})
            payload = json.dumps(state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if self.path not in ("/", "/health", "/keepalive"):
            self.send_error(404)
            return
        if self.runtime and not self.runtime.ready:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, format: str, *args: object) -> None:
        return


def start_health_server() -> ThreadingHTTPServer | None:
    port = os.environ.get("PORT")
    if not port:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), HealthHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    LOG.info("HTTP health-check слушает порт %s", port)
    return server


@dataclass(frozen=True)
class Job:
    message: Message
    link: Link | None = None
    track: str | None = None
    queued_at: float = field(default_factory=time.monotonic)


class MediaBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=12)
        self.music_queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=4)
        self.worker: asyncio.Task | None = None
        self.music_worker: asyncio.Task | None = None
        self.trigger = settings.trigger
        self.username = ""
        self.media_cache = OrderedDict()
        self.music_engine = MusicEngine()
        self.active_music = {}

    async def start(self, app: Application) -> None:
        from downloader import warmup
        await asyncio.to_thread(warmup)
        HealthHandler.media_bot = self
        HealthHandler.loop = asyncio.get_running_loop()
        self.username = app.bot.username
        self.trigger = self.trigger or f"@{self.username}"
        self.worker = asyncio.create_task(self.work(app.bot))
        self.music_worker = asyncio.create_task(self.work(app.bot, self.music_queue))
        LOG.info("@%s запущен. Privacy Mode: %s", self.username,
                 "выключен" if app.bot.bot.can_read_all_group_messages else "включён")
        if not self.settings.chat_ids:
            LOG.info("Для настройки группы отправь в неё /chatid@%s.", self.username)
        if self.settings.owner_user_id is None:
            LOG.info("Для настройки доступа в личке отправь боту /start и возьми ID из журнала.")

    async def stop(self, _: Application) -> None:
        probe = HealthHandler.probe_future
        if probe and not probe.done():
            probe.cancel()
        workers = [worker for worker in (self.worker, self.music_worker) if worker]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await self.music_engine.abort()

    async def receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message or (message.from_user and message.from_user.is_bot and not message.sender_chat):
            return
        content = message.text or message.caption or ""
        command = content.split(maxsplit=1)[0].lower() if content else ""
        id_commands = {"/chatid", f"/chatid@{self.username.lower()}"}
        start_commands = {"/start", f"/start@{self.username.lower()}"}

        if message.chat.type == Chat.PRIVATE:
            user_id = message.from_user.id if message.from_user else None
            if self.settings.owner_user_id is None and command in start_commands and user_id:
                LOG.info("Telegram ID для OWNER_USER_ID: %s", user_id)
            if not user_id or user_id != self.settings.owner_user_id:
                return
            if command in start_commands:
                await self.say(context.bot, message, f"Пришли ссылку или напиши {self.trigger} Название трека.")
                return
        elif message.chat.type in (Chat.GROUP, Chat.SUPERGROUP):
            allowed = message.chat_id in self.settings.chat_ids
            if command in id_commands and (allowed or not self.settings.chat_ids):
                await self.say(context.bot, message, f"ID группы: {message.chat_id}")
                return
            if not allowed:
                return
        else:
            return

        entities = (message.parse_entities([MessageEntity.URL, MessageEntity.TEXT_LINK]) if message.text
                    else message.parse_caption_entities([MessageEntity.URL, MessageEntity.TEXT_LINK]))
        entity_links = [entity.url or label for entity, label in entities.items()]
        links = links_in(content, entity_links)
        query = music_query(content, self.trigger)
        jobs = [Job(message, link=link) for link in links]
        if query:
            jobs.append(Job(message, track=query))
        if query == "" and not jobs:
            await self.say(context.bot, message, f"Напиши название после {self.trigger}.")
        for job in jobs:
            queue = self.music_queue if job.track else self.queue
            if queue.full():
                await self.say(context.bot, message, "Очередь занята. Пришли запрос чуть позже.")
                break
            ahead = queue.qsize() + (1 if job.track and self.active_music else 0)
            queue.put_nowait(job)
            if job.track and ahead:
                await self.say(context.bot, message, f"🎵 Запрос принят. Перед ним в очереди: {ahead}.")

    async def receive_tracked(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.receive(update, context)
        if HealthHandler.runtime:
            HealthHandler.runtime.updates_processed += 1

    async def work(self, bot: Bot, queue: asyncio.Queue[Job] | None = None) -> None:
        queue = self.queue if queue is None else queue
        while True:
            job = await queue.get()
            try:
                await self.process(bot, job)
            except TelegramError as exc:
                LOG.warning("Telegram / доставка: %s", type(exc).__name__)
            except Exception as exc:
                LOG.error("Очередь / ошибка: %s", type(exc).__name__)
            finally:
                queue.task_done()

    async def selftest(self, link: Link | None = None, query: str | None = None,
                       target_chat_id: int | None = None) -> None:
        trace = ProbeLog()
        HealthHandler.probe_logs = trace.lines
        loggers = (LOG, logging.getLogger("media"), logging.getLogger("downloader"), logging.getLogger("music_engine"))
        for logger in loggers:
            logger.addHandler(trace)
        try:
            bot = HealthHandler.application.bot
            target = target_chat_id if target_chat_id is not None else self.settings.owner_user_id
            if target != self.settings.owner_user_id and target not in self.settings.chat_ids:
                raise ValueError("Unapproved test destination")
            message = await bot.send_message(chat_id=target,
                                            text="Проверка с сервера Render: " + ("поиск музыки" if query else link.platform))
            success = await self.process(bot, Job(message, link=link, track=query))
            HealthHandler.probe_result = "delivered" if success else "failed"
        except Exception as exc:
            LOG.warning("Проверка Render / %s", error_detail(exc))
            HealthHandler.probe_result = "failed"
        finally:
            for logger in loggers:
                logger.removeHandler(trace)
            HealthHandler.probe_logs = trace.lines

    async def process(self, bot: Bot, job: Job) -> bool:
        if not job.track:
            return await self._process(bot, job)
        budget = min(MUSIC_TIMEOUT, 60 - (time.monotonic() - job.queued_at))
        if budget <= 0:
            try:
                async with asyncio.timeout(5):
                    await self.say(bot, job.message, "Запрос слишком долго ждал в очереди. Отправь его ещё раз.")
            except (TimeoutError, TelegramError):
                pass
            return False
        state = {"status": None, "stage": "поиск", "started": time.monotonic()}
        identity = id(asyncio.current_task())
        self.active_music[identity] = state
        context = MUSIC_STATE.set(state)
        try:
            async with asyncio.timeout(budget):
                return await self._process(bot, job)
        except TimeoutError:
            LOG.warning("Музыка / общий лимит %s с, этап: %s", MUSIC_TIMEOUT, state["stage"])
            try:
                async with asyncio.timeout(5):
                    await self.finish_status(bot, job.message, state["status"],
                        f"Источник музыки не ответил за {round(budget)} секунд. Попробуй указать исполнителя и название точнее.")
            except (TimeoutError, TelegramError):
                pass
            return False
        finally:
            self.active_music.pop(identity, None)
            MUSIC_STATE.reset(context)

    async def _process(self, bot: Bot, job: Job) -> bool:
        cache_key = job.link.url if job.link else "track:" + (job.track or "").strip().casefold()
        cached = self.media_cache.get(cache_key)
        if cached:
            try:
                await self.send_single(bot, job.message, cached)
                self.media_cache.move_to_end(cache_key)
                LOG.info("Telegram / повторная отправка из кэша")
                return True
            except TelegramError:
                self.media_cache.pop(cache_key, None)
        with TemporaryDirectory(prefix="telegram-media-") as temporary:
            directory = Path(temporary)
            started = asyncio.get_running_loop().time()
            status = None
            delivered = None
            if job.link:
                name = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube",
                        "twitter": "X / Twitter"}[job.link.platform]
                label = name
                failure = f"Не удалось получить медиа из {name}. Возможно, пост удалён, закрыт или сайт ограничил доступ."
                loader = load_post
                source = job.link
            else:
                label = "Поиск трека"
                failure = "Не удалось найти доступный трек. Попробуй указать исполнителя и название."
                loader = download_track
                source = job.track
            try:
                status = await self.send(lambda: bot.send_message(
                    **self.destination(job.message), text="🎵 Ищу трек…" if job.track else "⏳ Получаю медиа…"))
            except TelegramError:
                pass
            music = MUSIC_STATE.get()
            if music is not None:
                music["status"] = status
            try:
                result = await self.load(loader, source, directory)
            except (MediaError, OSError, ValueError) as exc:
                LOG.info("%s / %s", type(source).__name__, error_detail(exc))
                if job.link and job.link.platform == "youtube" and str(exc).startswith(("YouTube не разрешил", "YouTube:")):
                    failure = str(exc)
                await self.finish_status(bot, job.message, status, failure)
                return False
            except Exception as exc:
                LOG.warning("%s / ошибка источника: %s", type(source).__name__, type(exc).__name__)
                await self.finish_status(bot, job.message, status, failure)
                return False
            LOG.info("%s / источник подготовлен за %.1f с", label, asyncio.get_running_loop().time() - started)
            try:
                if music is not None:
                    music["stage"] = "отправка"
                await self.edit_status(status, "🎵 Отправляю трек…" if job.track else "⏳ Передаю медиа в Telegram…")
                if isinstance(result, Attachment):
                    delivered = await self.send_single(bot, job.message, result, directory)
                elif (len(result.visual) == 1 and not result.audio and not result.audio_source
                      and not result.incomplete):
                    delivered = await self.send_single(bot, job.message, result.visual[0], directory)
                else:
                    await self.send_post(bot, job.message, result, directory)
            except Exception as exc:
                remote = (isinstance(result, Attachment) and result.remote_url)
                simple_post = (isinstance(result, Post) and result.visual and not result.audio
                               and len(result.visual) <= 10
                               and any(item.remote_url for item in result.visual))
                try:
                    if job.track and remote:
                        LOG.info("%s / резервная загрузка", label)
                        await self.edit_status(status, "⏳ Прямая ссылка не подошла, загружаю резервным способом…")
                        local = await self.load(download_track_local, job.track, directory)
                        delivered = await self.send_single(bot, job.message, local, directory)
                    elif job.link and simple_post:
                        LOG.info("%s / резервная обработка", label)
                        await self.edit_status(status, "⏳ Прямая ссылка не подошла, обрабатываю видео…")
                        local = await self.load(recover_post, job.link, directory / "recovery")
                        if len(local.visual) == 1 and not local.audio and not local.incomplete:
                            delivered = await self.send_single(bot, job.message, local.visual[0], directory)
                        else:
                            await self.send_post(bot, job.message, local, directory)
                    else:
                        raise exc
                except Exception as fallback_exc:
                    LOG.warning("%s / отправка: %s; резервный путь: %s", label,
                                error_detail(exc), error_detail(fallback_exc))
                    await self.finish_status(bot, job.message, status, failure)
                    return False
            LOG.info("%s / готово за %.1f с", label, asyncio.get_running_loop().time() - started)
            if delivered:
                item = None
                if delivered.video:
                    video = delivered.video
                    item = Attachment("video", None, width=video.width, height=video.height,
                                      duration=video.duration, file_id=video.file_id)
                elif delivered.audio:
                    audio = delivered.audio
                    item = Attachment("audio", None, title=audio.title or "Аудио", artist=audio.performer or "",
                                      file_id=audio.file_id)
                if item:
                    self.media_cache[cache_key] = item
                    self.media_cache.move_to_end(cache_key)
                    while len(self.media_cache) > 128:
                        self.media_cache.popitem(last=False)
            if status:
                try:
                    await status.delete()
                except TelegramError:
                    pass
            return True

    async def edit_status(self, status: Message | None, text: str) -> None:
        if status:
            try:
                await status.edit_text(text)
            except TelegramError:
                pass

    async def finish_status(self, bot: Bot, message: Message, status: Message | None, text: str) -> None:
        if status:
            try:
                await status.edit_text(text)
                return
            except TelegramError:
                pass
        try:
            await self.say(bot, message, text)
        except TelegramError:
            pass

    async def load(self, loader, source, directory):
        if loader is download_track or loader is download_track_local:
            state = MUSIC_STATE.get()
            if state is not None:
                state["stage"] = "поиск и скачивание"
            return await self.music_engine.request("search" if loader is download_track else "local",
                                                   source, directory, self.settings)
        # Keep the temporary files in place while a download finishes during shutdown.
        task = asyncio.create_task(asyncio.to_thread(loader, source, directory, self.settings))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def destination(message: Message) -> dict:
        return {
            "chat_id": message.chat_id,
            "message_thread_id": message.message_thread_id if message.is_topic_message else None,
            "reply_parameters": ReplyParameters(message.message_id, allow_sending_without_reply=True),
        }

    @staticmethod
    async def send(call: Callable[[], Awaitable]):
        try:
            return await call()
        except RetryAfter as exc:
            delay = exc.retry_after
            await asyncio.sleep((delay.total_seconds() if isinstance(delay, timedelta) else delay) + 0.25)
            return await call()

    async def say(self, bot: Bot, message: Message, content: str) -> None:
        await self.send(lambda: bot.send_message(**self.destination(message), text=content))

    async def send_single(self, bot: Bot, message: Message, attachment: Attachment,
                          directory: Path | None = None) -> Message:
        if attachment.remote_url or attachment.file_id:
            media = attachment.file_id or attachment.remote_url
            destination = self.destination(message)
            try:
                if attachment.kind == "video":
                    sent = await self.send(lambda: bot.send_video(**destination, video=media,
                                         supports_streaming=True, read_timeout=60))
                elif attachment.kind == "photo":
                    sent = await self.send(lambda: bot.send_photo(**destination, photo=media,
                                         read_timeout=60))
                else:
                    sent = await self.send(lambda: bot.send_audio(**destination, audio=media,
                                         title=attachment.title, performer=attachment.artist or None,
                                         read_timeout=60))
                LOG.info("Telegram / доставлен %s по прямой ссылке", attachment.kind)
                return sent
            except (BadRequest, NetworkError) as exc:
                if directory is None or not attachment.remote_url:
                    raise
                LOG.info("%s / прямая отправка отклонена: %s; загружаю файл", attachment.platform,
                         type(exc).__name__)
                attachment = await asyncio.to_thread(materialize_remote, attachment, directory)
        async def upload():
            if attachment.path is None:
                raise MediaError("Файл медиа отсутствует")
            with attachment.path.open("rb") as handle:
                file = InputFile(handle, filename=attachment.path.name, read_file_handle=False)
                destination = self.destination(message)
                if attachment.kind == "video":
                    return await bot.send_video(**destination, video=file, supports_streaming=True,
                                         width=attachment.width, height=attachment.height,
                                         duration=attachment.duration, write_timeout=120)
                elif attachment.kind == "photo":
                    return await bot.send_photo(**destination, photo=file, write_timeout=90)
                else:
                    return await bot.send_audio(**destination, audio=file, title=attachment.title,
                                         performer=attachment.artist or None, write_timeout=120)
        sent = await self.send(upload)
        LOG.info("Telegram / доставлен %s загрузкой файла", attachment.kind)
        return sent

    async def send_post(self, bot: Bot, message: Message, post: Post, directory: Path) -> None:
        incomplete = post.incomplete
        audio_delivered = False
        for start in range(0, len(post.visual), 10):
            group = post.visual[start:start + 10]
            if len(group) == 1:
                await self.send_single(bot, message, group[0], directory)
            else:
                complete = await self.send_album(bot, message, group, directory)
                incomplete = incomplete or not complete
        for attachment in post.audio:
            try:
                await self.send_single(bot, message, attachment, directory)
                audio_delivered = True
            except Exception as exc:
                LOG.warning("Звук фотопоста / %s", error_detail(exc))
                if not post.audio_source:
                    incomplete = True
        if post.audio_source and not audio_delivered:
            try:
                audio = await self.load(photo_audio, post.audio_source, directory / "photo-audio")
                await self.send_single(bot, message, audio, directory)
                LOG.info("TikTok / звук фотопоста доставлен резервным путём")
            except Exception as exc:
                LOG.warning("TikTok / звук фотопоста не получен: %s", error_detail(exc))
                await self.say(bot, message, "Фотографии отправлены, но источник не выдал доступную аудиодорожку.")
        if incomplete:
            await self.say(bot, message, "Часть медиа в этом посте недоступна или превышает лимит.")

    async def send_album(self, bot: Bot, message: Message, group: list[Attachment],
                         directory: Path | None = None) -> bool:
        complete = True
        if all(item.remote_url for item in group):
            media = []
            for item in group:
                if item.kind == "video":
                    media.append(InputMediaVideo(item.remote_url, supports_streaming=True,
                                                 width=item.width, height=item.height,
                                                 duration=item.duration))
                else:
                    media.append(InputMediaPhoto(item.remote_url))
            try:
                await self.send(lambda: bot.send_media_group(**self.destination(message), media=media,
                                                             read_timeout=60))
                LOG.info("Telegram / доставлен альбом: %s файлов", len(group))
                return True
            except (BadRequest, NetworkError) as exc:
                if directory is None:
                    raise
                LOG.info("Альбом / прямая отправка отклонена: %s; загружаю файлы", type(exc).__name__)
                local = []
                for index, item in enumerate(group):
                    try:
                        local.append(await asyncio.to_thread(materialize_remote, item, directory, index))
                    except Exception as file_exc:
                        LOG.warning("Альбом / файл %s: %s", index + 1, error_detail(file_exc))
                        complete = False
                if not local:
                    raise MediaError("Не удалось загрузить файлы альбома")
                group = local
                if len(group) == 1:
                    await self.send_single(bot, message, group[0], directory)
                    return complete
        async def upload():
            with ExitStack() as stack:
                album = []
                for item in group:
                    if item.path is None:
                        raise MediaError("Файл медиа отсутствует")
                    handle = stack.enter_context(item.path.open("rb"))
                    file = InputFile(handle, filename=item.path.name, attach=True, read_file_handle=False)
                    if item.kind == "video":
                        album.append(InputMediaVideo(file, supports_streaming=True, width=item.width,
                                                     height=item.height, duration=item.duration))
                    else:
                        album.append(InputMediaPhoto(file))
                await bot.send_media_group(**self.destination(message), media=album, write_timeout=150)
        await self.send(upload)
        LOG.info("Telegram / доставлен альбом: %s файлов загрузкой", len(group))
        return complete


async def error_handler(_: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        LOG.error("У этого токена уже запущен другой процесс бота. Оставь один экземпляр.")
    else:
        LOG.warning("Telegram / обновление: %s", type(context.error).__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for module in ("httpx", "httpcore", "telegram"):
        logging.getLogger(module).setLevel(logging.CRITICAL)
    try:
        settings = Settings.load()
        for executable in ("ffmpeg", "ffprobe", "gallery-dl", "yt-dlp"):
            if not shutil.which(executable):
                raise ValueError(f"Команда {executable} не найдена. Проверь установку зависимостей.")
        if not shutil.which("deno"):
            LOG.warning("Deno не найден. YouTube может не работать. В Docker Deno уже включён.")
        bot = MediaBot(settings)
        public_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
        if public_url:
            parts = urlsplit(public_url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise ValueError("RENDER_EXTERNAL_URL должен быть публичным HTTPS-адресом сервиса.")
        builder = (ApplicationBuilder().token(settings.token).connect_timeout(20).read_timeout(30)
                   .update_queue(asyncio.Queue(maxsize=100))
                   .post_init(bot.start).post_stop(bot.stop))
        if public_url:
            builder = builder.updater(None)
        app = builder.build()
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE, bot.receive_tracked))
        app.add_error_handler(error_handler)
        HealthHandler.application = app
        if public_url:
            HealthHandler.runtime = WebhookRuntime(app, bot, public_url)
        LOG.info("Сборка %s / запускаю Telegram", BUILD)
        health_server = start_health_server()
        try:
            if HealthHandler.runtime:
                asyncio.run(HealthHandler.runtime.run())
            else:
                app.run_polling(allowed_updates=["message"], timeout=25, bootstrap_retries=-1)
        finally:
            if health_server:
                health_server.shutdown()
                health_server.server_close()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except InvalidToken:
        raise SystemExit("Telegram отклонил токен. Проверь TELEGRAM_BOT_TOKEN в .env.") from None


if __name__ == "__main__":
    main()
