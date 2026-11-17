import asyncio
import json
import hashlib
import hmac
import logging
import os
import re
import shutil
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Awaitable, Callable

from telegram import Bot, Chat, InputFile, InputMediaPhoto, InputMediaVideo, Message, MessageEntity
from telegram import ReplyParameters, Update
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from links import Link, links_in, music_query, parse_link
from media import (Attachment, MediaError, Post, download_post, download_track,
                   download_track_local, error_detail, materialize_remote, recover_post, load_post, photo_audio)
from settings import Settings

LOG = logging.getLogger(__name__)
BUILD = "2026.11.17.14"


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
    application: Application | None = None
    media_bot = None
    loop = None
    probe_future = None
    probe_result = "not_run"
    probe_logs = []
    probe_lock = threading.Lock()

    def do_POST(self) -> None:
        worker = self.media_bot
        if self.path != "/selftest" or worker is None:
            self.send_error(404)
            return
        if not self.probe_authorized():
            self.send_error(403)
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
        except (ValueError, TypeError, AttributeError):
            self.send_error(400)
            return
        with self.probe_lock:
            if HealthHandler.probe_future and not HealthHandler.probe_future.done():
                self.send_error(409)
                return
            HealthHandler.probe_result = "running"
            HealthHandler.probe_logs = []
            HealthHandler.probe_future = asyncio.run_coroutine_threadsafe(worker.selftest(link, query), self.loop)
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"started\n")

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
            payload = json.dumps({"result": HealthHandler.probe_result, "logs": HealthHandler.probe_logs},
                                 ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if self.path == "/status":
            payload = json.dumps({"build": BUILD,
                                  "selftest": HealthHandler.probe_result,
                                  "application_running": bool(self.application and self.application.running)})
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if self.path not in ("/", "/health"):
            self.send_error(404)
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


class MediaBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=12)
        self.music_queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=4)
        self.worker: asyncio.Task | None = None
        self.music_worker: asyncio.Task | None = None
        self.trigger = settings.trigger
        self.username = ""

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
            queue.put_nowait(job)

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

    async def selftest(self, link: Link | None = None, query: str | None = None) -> None:
        trace = ProbeLog()
        HealthHandler.probe_logs = trace.lines
        loggers = (LOG, logging.getLogger("media"), logging.getLogger("downloader"))
        for logger in loggers:
            logger.addHandler(trace)
        try:
            bot = HealthHandler.application.bot
            message = await bot.send_message(chat_id=self.settings.owner_user_id,
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
        with TemporaryDirectory(prefix="telegram-media-") as temporary:
            directory = Path(temporary)
            started = asyncio.get_running_loop().time()
            status = None
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
                    **self.destination(job.message), text="⏳ Получаю медиа…"))
            except TelegramError:
                pass
            try:
                result = await self.load(loader, source, directory)
            except (MediaError, OSError, ValueError) as exc:
                LOG.info("%s / %s", type(source).__name__, error_detail(exc))
                await self.finish_status(bot, job.message, status, failure)
                return False
            except Exception as exc:
                LOG.warning("%s / ошибка источника: %s", type(source).__name__, type(exc).__name__)
                await self.finish_status(bot, job.message, status, failure)
                return False
            LOG.info("%s / источник подготовлен за %.1f с", label, asyncio.get_running_loop().time() - started)
            try:
                await self.edit_status(status, "⏳ Передаю медиа в Telegram…")
                if isinstance(result, Attachment):
                    await self.send_single(bot, job.message, result, directory)
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
                        await self.send_single(bot, job.message, local, directory)
                    elif job.link and simple_post:
                        LOG.info("%s / резервная обработка", label)
                        await self.edit_status(status, "⏳ Прямая ссылка не подошла, обрабатываю видео…")
                        local = await self.load(recover_post, job.link, directory / "recovery")
                        await self.send_post(bot, job.message, local, directory)
                    else:
                        raise exc
                except Exception as fallback_exc:
                    LOG.warning("%s / отправка: %s; резервный путь: %s", label,
                                error_detail(exc), error_detail(fallback_exc))
                    await self.finish_status(bot, job.message, status, failure)
                    return False
            LOG.info("%s / готово за %.1f с", label, asyncio.get_running_loop().time() - started)
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
                          directory: Path | None = None) -> None:
        if attachment.remote_url:
            destination = self.destination(message)
            try:
                if attachment.kind == "video":
                    await self.send(lambda: bot.send_video(**destination, video=attachment.remote_url,
                                         supports_streaming=True, read_timeout=60))
                elif attachment.kind == "photo":
                    await self.send(lambda: bot.send_photo(**destination, photo=attachment.remote_url,
                                         read_timeout=60))
                else:
                    await self.send(lambda: bot.send_audio(**destination, audio=attachment.remote_url,
                                         title=attachment.title, performer=attachment.artist or None,
                                         read_timeout=60))
                LOG.info("Telegram / доставлен %s по прямой ссылке", attachment.kind)
                return
            except (BadRequest, NetworkError) as exc:
                if directory is None:
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
                    await bot.send_video(**destination, video=file, supports_streaming=True,
                                         width=attachment.width, height=attachment.height,
                                         duration=attachment.duration, write_timeout=120)
                elif attachment.kind == "photo":
                    await bot.send_photo(**destination, photo=file, write_timeout=90)
                else:
                    await bot.send_audio(**destination, audio=file, title=attachment.title,
                                         performer=attachment.artist or None, write_timeout=120)
        await self.send(upload)
        LOG.info("Telegram / доставлен %s загрузкой файла", attachment.kind)

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
        app = (ApplicationBuilder().token(settings.token).connect_timeout(20).read_timeout(30)
               .post_init(bot.start).post_stop(bot.stop).build())
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE, bot.receive))
        app.add_error_handler(error_handler)
        HealthHandler.application = app
        LOG.info("Сборка %s / запускаю Telegram", BUILD)
        health_server = start_health_server()
        try:
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
