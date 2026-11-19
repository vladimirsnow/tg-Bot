import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Update
from bot import MediaBot
from cookie_help import COOKIE_HELP
from settings import Settings


class CookieHelpTests(unittest.IsolatedAsyncioTestCase):
    def setup_worker(self, owner=123):
        worker = MediaBot(Settings("", "", frozenset({-1002368553296}), owner, None, None))
        worker.username = "TestBot"
        worker.trigger = "@TestBot"
        return worker

    def update(self, user, chat, kind, text):
        return Update.de_json({"update_id": 1, "message": {"message_id": 2, "date": 1794900000,
            "chat": {"id": chat, "type": kind}, "from": {"id": user, "is_bot": False, "first_name": "Test"},
            "text": text}}, None)

    async def test_owner_private_help_does_not_use_download_queues(self):
        worker = self.setup_worker()
        context = SimpleNamespace(bot=AsyncMock())
        for command in ("/cooki", "/cooki@TestBot"):
            await worker.receive(self.update(123, 123, "private", command), context)
        self.assertEqual(context.bot.send_message.await_count, 2)
        self.assertEqual(worker.owner_help_deliveries, 2)
        self.assertIn("YOUTUBE_COOKIES_BASE64", context.bot.send_message.call_args.kwargs["text"])
        self.assertEqual(context.bot.send_message.call_args.kwargs["chat_id"], 123)
        self.assertTrue(worker.queue.empty())
        self.assertTrue(worker.music_queue.empty())

    async def test_all_group_cookie_commands_are_silent_even_with_links_or_track_queries(self):
        worker = self.setup_worker()
        context = SimpleNamespace(bot=AsyncMock())
        for user in (123, 999):
            for kind in ("group", "supergroup"):
                await worker.receive(self.update(user, -1002368553296, kind,
                    "/cooki@TestBot https://youtube.com/shorts/v2M26i19fvI @TestBot song"), context)
        context.bot.send_message.assert_not_awaited()
        self.assertTrue(worker.queue.empty())
        self.assertTrue(worker.music_queue.empty())

    async def test_private_other_user_and_other_bot_address_are_silent(self):
        worker = self.setup_worker()
        context = SimpleNamespace(bot=AsyncMock())
        await worker.receive(self.update(999, 999, "private", "/cooki"), context)
        await worker.receive(self.update(123, 999, "private", "/cooki"), context)
        await worker.receive(self.update(123, 123, "private", "/cooki@AnotherBot"), context)
        context.bot.send_message.assert_not_awaited()

    async def test_help_does_not_consume_or_replace_existing_media_jobs(self):
        worker = self.setup_worker()
        video, music = object(), object()
        worker.queue.put_nowait(video)
        worker.music_queue.put_nowait(music)
        await worker.receive(self.update(123, 123, "private", "/cooki"), SimpleNamespace(bot=AsyncMock()))
        self.assertIs(worker.queue.get_nowait(), video)
        self.assertIs(worker.music_queue.get_nowait(), music)

    async def test_unconfigured_owner_does_not_expose_help(self):
        worker = self.setup_worker(owner=None)
        context = SimpleNamespace(bot=AsyncMock())
        await worker.receive(self.update(123, 123, "private", "/cooki"), context)
        context.bot.send_message.assert_not_awaited()

    def test_help_fits_a_telegram_message(self):
        self.assertLess(len(COOKIE_HELP), 4096)
