"""Webhook delivery and supervision for a free Render web service."""
import asyncio
import hashlib
import hmac
import logging
import signal
import time
from collections import OrderedDict

import httpx
from telegram import Update
from telegram.error import InvalidToken, TelegramError

LOG = logging.getLogger(__name__)


class WebhookRuntime:
    interval = 300

    def __init__(self, application, worker, public_url):
        self.application = application
        self.worker = worker
        self.public_url = public_url.rstrip("/")
        self.webhook_url = self.public_url + "/telegram"
        self.secret = hmac.new(worker.settings.token.encode(), b"telegram-webhook", hashlib.sha256).hexdigest()
        self.started_at = time.time()
        self.ready = False
        self.loop = None
        self.keepalive_ok = 0
        self.keepalive_failed = 0
        self.last_keepalive = None
        self.updates_received = 0
        self.updates_processed = 0
        self.duplicates = 0
        self.seen = OrderedDict()
        self.stopping = asyncio.Event()

    def snapshot(self):
        return {"mode": "webhook", "webhook_ready": self.ready,
                "uptime_seconds": round(time.time() - self.started_at),
                "keepalive_ok": self.keepalive_ok, "keepalive_failed": self.keepalive_failed,
                "last_keepalive": self.last_keepalive,
                "updates_received": self.updates_received, "updates_processed": self.updates_processed,
                "duplicate_updates": self.duplicates}

    async def accept(self, payload):
        if not self.ready or not self.application.running:
            raise RuntimeError("Application is starting")
        if not isinstance(payload, dict) or type(payload.get("update_id")) is not int:
            raise ValueError("Invalid update")
        identity = payload["update_id"]
        if identity in self.seen:
            self.duplicates += 1
            return
        update = Update.de_json(payload, self.application.bot)
        # No awaits between checking and recording the ID: concurrent deliveries
        # cannot put the same update into the queue twice.
        self.application.update_queue.put_nowait(update)
        self.seen[identity] = True
        while len(self.seen) > 2048:
            self.seen.popitem(last=False)
        self.updates_received += 1

    async def register(self):
        await self.application.bot.set_webhook(
            url=self.webhook_url, secret_token=self.secret, allowed_updates=["message"],
            max_connections=4, drop_pending_updates=False)
        LOG.info("Telegram webhook настроен; сообщения поступают по HTTP")

    async def retry(self, operation):
        delay = 2
        while not self.stopping.is_set():
            try:
                await operation()
                return True
            except InvalidToken:
                raise
            except TelegramError as exc:
                LOG.warning("Telegram / запуск: %s; повтор через %s с", type(exc).__name__, delay)
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(30, delay * 2)
        return False

    async def maintain_once(self, client):
        try:
            response = await client.get(self.public_url + "/keepalive")
            response.raise_for_status()
            if response.text.strip() != "ok":
                raise ValueError("Unexpected keepalive response")
            self.keepalive_ok += 1
            self.last_keepalive = time.time()
            LOG.info("Самопроверка / входящий HTTP-запрос выполнен")
        except (httpx.HTTPError, ValueError) as exc:
            self.keepalive_failed += 1
            LOG.warning("Самопроверка / HTTP: %s", type(exc).__name__)
        try:
            info = await self.application.bot.get_webhook_info()
            if info.url != self.webhook_url:
                await self.register()
        except TelegramError as exc:
            LOG.warning("Самопроверка / Telegram: %s", type(exc).__name__)
        for field, queue in (("worker", self.worker.queue), ("music_worker", self.worker.music_queue)):
            task = getattr(self.worker, field)
            if task is None or task.done():
                if task and not task.cancelled():
                    task.exception()  # Retrieve the exception without logging request data.
                setattr(self.worker, field, asyncio.create_task(self.worker.work(self.application.bot, queue)))
                LOG.warning("Самопроверка / восстановлен обработчик %s", field)

    async def watchdog(self):
        async with httpx.AsyncClient(timeout=20) as client:
            while not self.stopping.is_set():
                await self.maintain_once(client)
                delay = 30 if self.keepalive_failed and (
                    self.last_keepalive is None or time.time() - self.last_keepalive > self.interval) else self.interval
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def run(self):
        self.loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(signum, self.stopping.set)
        monitor = None
        worker_started = False
        try:
            if not await self.retry(self.application.initialize):
                return
            await self.worker.start(self.application)
            worker_started = True
            await self.application.start()
            if not await self.retry(self.register):
                return
            self.ready = True
            monitor = asyncio.create_task(self.watchdog(), name="runtime-watchdog")
            await self.stopping.wait()
        finally:
            self.ready = False
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if self.application.running:
                await self.application.stop()
            if worker_started:
                await self.worker.stop(self.application)
            await self.application.shutdown()
            # Keep the webhook registered: Telegram retries while Render restarts.
