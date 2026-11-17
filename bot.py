import asyncio
import logging
import os
import shutil
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Awaitable, Callable

from telegram import Bot, Chat, InputFile, InputMediaPhoto, InputMediaVideo, Message, MessageEntity
from telegram import ReplyParameters, Update
from telegram.error import Conflict, InvalidToken, RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from links import Link, links_in, music_query
from media import Attachment, MediaError, Post, download_post, download_track
from settings import Settings

LOG = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
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
        self.worker: asyncio.Task | None = None
        self.trigger = settings.trigger
        self.username = ""

    async def start(self, app: Application) -> None:
        self.username = app.bot.username
        self.trigger = self.trigger or f"@{self.username}"
        self.worker = asyncio.create_task(self.work(app.bot))
        LOG.info("@%s запущен. Privacy Mode: %s", self.username,
                 "выключен" if app.bot.bot.can_read_all_group_messages else "включён")
        if not self.settings.chat_ids:
            LOG.info("Для настройки группы отправь в неё /chatid@%s.", self.username)
        if self.settings.owner_user_id is None:
            LOG.info("Для настройки доступа в личке отправь боту /start и возьми ID из журнала.")

    async def stop(self, _: Application) -> None:
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

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
            if self.queue.full():
                await self.say(context.bot, message, "Очередь занята. Пришли запрос чуть позже.")
                break
            self.queue.put_nowait(job)

    async def work(self, bot: Bot) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self.process(bot, job)
            except TelegramError as exc:
                LOG.warning("Telegram / доставка: %s", type(exc).__name__)
            except Exception as exc:
                LOG.error("Очередь / ошибка: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def process(self, bot: Bot, job: Job) -> None:
        with TemporaryDirectory(prefix="telegram-media-") as temporary:
            directory = Path(temporary)
            if job.link:
                name = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube",
                        "twitter": "X / Twitter"}[job.link.platform]
                failure = f"Не удалось получить медиа из {name}. Возможно, пост удалён, закрыт или сайт ограничил доступ."
                loader = download_post
                source = job.link
            else:
                failure = "Не удалось найти доступный трек. Попробуй указать исполнителя и название."
                loader = download_track
                source = job.track
            try:
                result = await self.load(loader, source, directory)
            except (MediaError, OSError, ValueError) as exc:
                LOG.info("%s / %s", type(source).__name__, str(exc) if isinstance(exc, MediaError) else type(exc).__name__)
                await self.say(bot, job.message, failure)
                return
            except Exception as exc:
                LOG.warning("%s / ошибка источника: %s", type(source).__name__, type(exc).__name__)
                await self.say(bot, job.message, failure)
                return
            if isinstance(result, Attachment):
                await self.send_single(bot, job.message, result)
            else:
                await self.send_post(bot, job.message, result)

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

    async def send_single(self, bot: Bot, message: Message, attachment: Attachment) -> None:
        async def upload():
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

    async def send_post(self, bot: Bot, message: Message, post: Post) -> None:
        for start in range(0, len(post.visual), 10):
            group = post.visual[start:start + 10]
            if len(group) == 1:
                await self.send_single(bot, message, group[0])
            else:
                await self.send_album(bot, message, group)
        for attachment in post.audio:
            await self.send_single(bot, message, attachment)
        if post.incomplete:
            await self.say(bot, message, "Часть медиа в этом посте недоступна или превышает лимит.")

    async def send_album(self, bot: Bot, message: Message, group: list[Attachment]) -> None:
        async def upload():
            with ExitStack() as stack:
                album = []
                for item in group:
                    handle = stack.enter_context(item.path.open("rb"))
                    file = InputFile(handle, filename=item.path.name, attach=True, read_file_handle=False)
                    if item.kind == "video":
                        album.append(InputMediaVideo(file, supports_streaming=True, width=item.width,
                                                     height=item.height, duration=item.duration))
                    else:
                        album.append(InputMediaPhoto(file))
                await bot.send_media_group(**self.destination(message), media=album, write_timeout=150)
        await self.send(upload)


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
