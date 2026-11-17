import asyncio
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from bot import HealthHandler
from runtime import WebhookRuntime


def fixture():
    bot = SimpleNamespace(set_webhook=AsyncMock(), get_webhook_info=AsyncMock())
    app = SimpleNamespace(bot=bot, running=True, update_queue=asyncio.Queue(maxsize=1))
    worker = SimpleNamespace(settings=SimpleNamespace(token="test-token"),
                             queue=asyncio.Queue(), music_queue=asyncio.Queue(),
                             worker=None, music_worker=None, work=AsyncMock())
    return WebhookRuntime(app, worker, "https://service.onrender.com")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_authentication_duplicates_and_queue_backpressure(self):
        runtime = fixture()
        runtime.ready = True
        runtime.loop = asyncio.get_running_loop()
        data = {"update_id": 1, "message": {"message_id": 2, "date": 1794900000,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "first_name": "Test", "is_bot": False}, "text": "/start"}}
        with patch.object(HealthHandler, "runtime", runtime):
            server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/telegram"
            try:
                async with httpx.AsyncClient() as client:
                    denied = await client.post(url, json=data)
                    self.assertEqual(denied.status_code, 403)
                    self.assertTrue(runtime.application.update_queue.empty())
                    headers = {"X-Telegram-Bot-Api-Secret-Token": runtime.secret}
                    self.assertEqual((await client.post(url, json=data, headers=headers)).status_code, 200)
                    self.assertEqual((await client.post(url, json=data, headers=headers)).status_code, 200)
                    self.assertEqual(runtime.application.update_queue.qsize(), 1)
                    self.assertEqual(runtime.updates_received, 1)
                    self.assertEqual(runtime.duplicates, 1)
                    # A full queue is retried by Telegram, not acknowledged/lost.
                    data["update_id"] = 3
                    self.assertEqual((await client.post(url, json=data, headers=headers)).status_code, 503)
                    self.assertNotIn(3, runtime.seen)
                    runtime.ready = False
                    self.assertEqual((await client.post(url, json=data, headers=headers)).status_code, 503)
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join()

    async def test_watchdog_repairs_registration_and_dead_workers(self):
        runtime = fixture()
        runtime.application.bot.get_webhook_info.return_value = SimpleNamespace(url="")
        client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
            200, text="ok", request=httpx.Request("GET", "https://service.onrender.com/keepalive"))))
        await runtime.maintain_once(client)
        try:
            client.get.assert_awaited_once_with("https://service.onrender.com/keepalive")
            self.assertEqual(runtime.keepalive_ok, 1)
            runtime.application.bot.set_webhook.assert_awaited_once()
            options = runtime.application.bot.set_webhook.call_args.kwargs
            self.assertFalse(options["drop_pending_updates"])
            self.assertEqual(options["secret_token"], runtime.secret)
            self.assertIsNotNone(runtime.worker.worker)
            self.assertIsNotNone(runtime.worker.music_worker)
        finally:
            for task in (runtime.worker.worker, runtime.worker.music_worker):
                task.cancel()
            await asyncio.gather(runtime.worker.worker, runtime.worker.music_worker, return_exceptions=True)

    async def test_failed_keepalive_does_not_prevent_telegram_recovery(self):
        runtime = fixture()
        runtime.application.bot.get_webhook_info.return_value = SimpleNamespace(url="")
        client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ConnectError("offline")))
        await runtime.maintain_once(client)
        self.assertEqual(runtime.keepalive_failed, 1)
        runtime.application.bot.set_webhook.assert_awaited_once()
        for task in (runtime.worker.worker, runtime.worker.music_worker):
            task.cancel()
        await asyncio.gather(runtime.worker.worker, runtime.worker.music_worker, return_exceptions=True)
