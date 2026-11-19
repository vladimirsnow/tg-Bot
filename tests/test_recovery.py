import asyncio
import base64
import json
import tempfile
import threading
import unittest
import os
import httpx
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest
from telegram import Update
from dataclasses import replace

from bot import Job, MediaBot, HealthHandler
from links import parse_link
from media import Attachment, MediaError, Post, gallery, recover_post, load_post, download_post, Remote, ytdlp
from settings import Settings
from downloader import DownloadFailure, extract


SETTINGS = Settings("", "", frozenset(), 123, None, None)


class RecoveryTests(unittest.TestCase):
    def test_expired_youtube_session_is_not_reported_as_missing_video(self):
        def fail(url, download=False):
            backend.call_args.args[0]["logger"].warning("The provided YouTube account cookies are no longer valid.")
            raise ValueError("Requested format is not available")
        with patch("yt_dlp.YoutubeDL") as backend:
            backend.return_value.__enter__.return_value.extract_info.side_effect = fail
            with self.assertRaises(DownloadFailure) as failure:
                extract("https://youtube.com/watch?v=test")
            self.assertEqual(failure.exception.reason, "youtube_cookies_expired")

    def test_js_process_failure_does_not_ask_for_another_account(self):
        with tempfile.TemporaryDirectory() as temp, patch("downloader.extract", side_effect=DownloadFailure("javascript_process_killed")):
            with self.assertRaisesRegex(MediaError, "принудительно остановлен"):
                ytdlp("https://youtube.com/watch?v=test", Path(temp), None)

    def test_project_groups_are_merged_with_environment_groups(self):
        values = {"TELEGRAM_BOT_TOKEN": "123:" + "a" * 24, "ALLOWED_CHAT_IDS": "-1005"}
        with patch("settings.load_dotenv"), patch.dict(os.environ, values, clear=True):
            settings = Settings.load()
            self.assertIn(-1005, settings.chat_ids)
            self.assertIn(-1002368553296, settings.chat_ids)

    def test_youtube_environment_cookies_are_private_and_validated(self):
        content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tTEST\tvalue\n"
        values = {"TELEGRAM_BOT_TOKEN": "123:" + "a" * 24,
                  "YOUTUBE_COOKIES_BASE64": base64.b64encode(content.encode()).decode()}
        with patch("settings.load_dotenv"), patch.dict(os.environ, values, clear=True):
            settings = Settings.load()
            try:
                self.assertEqual(settings.youtube_cookies.stat().st_mode & 0o777, 0o600)
                self.assertEqual(settings.youtube_cookies.read_text(), content)
            finally:
                settings.youtube_cookies.unlink()

    def test_tiktok_prefers_h264_even_for_tall_portrait_videos(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "download.mp4"
            source.write_bytes(b"video")
            with patch("downloader.extract", return_value={}) as backend, \
                 patch("media.as_video", return_value=Attachment("video", source)):
                ytdlp("https://vt.tiktok.com/ZSpGEBawN/", root, None)
                selected = backend.call_args.args[1]["format"]
                self.assertTrue(selected.startswith("b[vcodec=h264]"))
                self.assertNotIn("height<=", selected)

    def test_blocked_youtube_does_not_repeat_identical_requests(self):
        link = parse_link("https://youtube.com/shorts/v2M26i19fvI")
        with patch("media.download_post", side_effect=MediaError("HTTP Error 429")), \
             patch("media.recover_post") as recovery:
            with self.assertRaisesRegex(MediaError, "YouTube не разрешил"):
                load_post(link, Path("/tmp"), SETTINGS)
            recovery.assert_not_called()

    def test_remote_probe_rejects_unsigned_requests(self):
        with patch.object(HealthHandler, "media_bot", SimpleNamespace(settings=SETTINGS)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = httpx.post(f"http://127.0.0.1:{server.server_port}/selftest")
                self.assertEqual(response.status_code, 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_extraction_failure_also_starts_recovery(self):
        link = parse_link("https://vt.tiktok.com/ZSpGEBawN/")
        expected = Post([Attachment("video", Path("/tmp/video.mp4"))])
        with tempfile.TemporaryDirectory() as temporary:
            with patch("media.download_post", side_effect=MediaError("CDN 403")), \
                 patch("media.recover_post", return_value=expected) as recovery:
                self.assertIs(load_post(link, Path(temporary), SETTINGS), expected)
                self.assertEqual(recovery.call_args.args[1], Path(temporary) / "extraction-recovery")

    def test_tiktok_downloader_runs_when_tikwm_times_out(self):
        link = parse_link("https://vt.tiktok.com/ZSpGEBawN/")
        expected = Attachment("video", Path("/tmp/video.mp4"))
        with tempfile.TemporaryDirectory() as temporary:
            with patch("media.tikwm", side_effect=httpx.ReadTimeout("API timeout")), \
                 patch("media.gallery", return_value=[Remote("video", "https://v16.tiktok.com/clip.mp4")]), \
                 patch("media.ytdlp", return_value=expected) as native:
                self.assertEqual(download_post(link, Path(temporary), SETTINGS).visual, [expected])
                self.assertEqual(native.call_args.kwargs, {"timeout": 45, "native_info": None})

    def test_photo_and_music_from_same_source_do_not_wait_for_another_provider(self):
        link = parse_link("https://www.tiktok.com/@example/photo/7708309252607151361")
        items = [Remote("photo", "https://p16.tiktokcdn.com/photo.jpg"),
                 Remote("audio", "https://sf16.tiktokcdn.com/song.mp3", "Song", "Artist")]
        with tempfile.TemporaryDirectory() as temporary:
            with patch("media.gallery", return_value=items), patch("media.tikwm") as slow_api, \
                 patch("media.ytdlp") as video_downloader:
                post = download_post(link, Path(temporary), SETTINGS)
                self.assertEqual(len(post.visual), 1)
                self.assertEqual(len(post.audio), 1)
                self.assertEqual(post.audio_source, link)
                slow_api.assert_not_called()
                video_downloader.assert_not_called()

    def test_shorts_needs_only_one_extraction(self):
        link = parse_link("https://youtube.com/shorts/v2M26i19fvI")
        item = Attachment("video", Path("/tmp/short.mp4"))
        with tempfile.TemporaryDirectory() as temporary, patch("media.ytdlp", return_value=item) as native:
            self.assertEqual(download_post(link, Path(temporary), SETTINGS).visual, [item])
            native.assert_called_once()

    def test_recovery_uses_fresh_directory_and_native_downloader(self):
        link = parse_link("https://vt.tiktok.com/ZSpGEBawN/")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "provider-0").mkdir()
            expected = Attachment("video", root / "video.mp4")
            with patch("media.ytdlp", return_value=expected) as native:
                for _ in range(2):
                    post = recover_post(link, root, SETTINGS)
                    self.assertEqual(post.visual, [expected])
                self.assertEqual(native.call_args.args[1], root / "native")

    def test_failed_native_downloader_tries_local_providers(self):
        link = parse_link("https://vt.tiktok.com/ZSpGEBawN/")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = Post([Attachment("video", root / "fallback.mp4")])
            with patch("media.ytdlp", side_effect=MediaError("HTTP 403")), \
                 patch("media.download_post", return_value=expected) as providers:
                self.assertIs(recover_post(link, root, SETTINGS), expected)
                self.assertEqual(providers.call_args.kwargs, {"fast": False})
                self.assertEqual(providers.call_args.args[1], root / "providers")

    def test_instagram_mp4_without_video_url_metadata_is_video(self):
        link = parse_link("https://www.instagram.com/reel/Example123/")
        result = [[3, "https://video.cdninstagram.com/clip.mp4?token=x", {}]]
        with patch("media.run", return_value=json.dumps(result)):
            self.assertEqual(gallery(link, None)[0].kind, "video")


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_group_accepts_links_from_regular_members(self):
        group = -1002368553296
        worker = MediaBot(replace(SETTINGS, chat_ids=frozenset({group})))
        context = SimpleNamespace(bot=AsyncMock())

        def update(chat_id, chat_type):
            return Update.de_json({"update_id": 1, "message": {"message_id": 2, "date": 1794900000,
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": 999, "first_name": "Member", "is_bot": False},
                "text": "https://vt.tiktok.com/ZSpGEBawN/"}}, None)

        await worker.receive(update(-100999, "supergroup"), context)
        self.assertTrue(worker.queue.empty())
        await worker.receive(update(999, "private"), context)
        self.assertTrue(worker.queue.empty())
        await worker.receive(update(group, "supergroup"), context)
        self.assertEqual(worker.queue.qsize(), 1)
        self.assertEqual(worker.queue.get_nowait().message.chat_id, group)

    async def test_cached_video_does_not_download_again(self):
        worker = MediaBot(SETTINGS)
        link = parse_link("https://youtube.com/shorts/v2M26i19fvI")
        cached = Attachment("video", None, file_id="telegram-file-id")
        worker.media_cache[link.url] = cached
        worker.send_single = AsyncMock()
        worker.load = AsyncMock()
        message = SimpleNamespace()
        api = SimpleNamespace()
        self.assertTrue(await worker.process(api, Job(message, link=link)))
        worker.send_single.assert_awaited_once_with(api, message, cached)
        worker.load.assert_not_awaited()

    async def test_one_bad_album_file_does_not_hide_other_photos(self):
        worker = MediaBot(SETTINGS)
        api = SimpleNamespace(send_media_group=AsyncMock(side_effect=BadRequest("CDN failed")))
        message = SimpleNamespace(chat_id=1, message_id=2, is_topic_message=False)
        worker.send_single = AsyncMock()
        remote = [Attachment("photo", None, remote_url="https://www.tikwm.com/a.jpg"),
                  Attachment("photo", None, remote_url="https://www.tikwm.com/b.jpg")]
        local = Attachment("photo", Path("/tmp/photo.jpg"))
        with patch("bot.materialize_remote", side_effect=[MediaError("HTTP 403"), local]):
            complete = await worker.send_album(api, message, remote, Path("/tmp"))
        self.assertFalse(complete)
        worker.send_single.assert_awaited_once_with(api, message, local, Path("/tmp"))

    async def test_failed_sound_does_not_discard_delivered_photos(self):
        worker = MediaBot(SETTINGS)
        post = Post([Attachment("photo", None)], [Attachment("audio", None)])
        worker.send_single = AsyncMock(side_effect=[None, MediaError("HTTP 403")])
        worker.say = AsyncMock()
        await worker.send_post(SimpleNamespace(), SimpleNamespace(), post, Path("/tmp"))
        self.assertEqual(worker.send_single.await_count, 2)
        worker.say.assert_awaited_once()

    async def test_music_job_cannot_block_video_worker(self):
        worker = MediaBot(SETTINGS)
        video = Job(SimpleNamespace(), link=parse_link("https://vt.tiktok.com/ZSpGEBawN/"))
        music = Job(SimpleNamespace(), track="Artist - Song")
        worker.queue.put_nowait(video)
        worker.music_queue.put_nowait(music)
        worker.process = AsyncMock()
        task = asyncio.create_task(worker.work(SimpleNamespace()))
        try:
            await asyncio.wait_for(worker.queue.join(), timeout=1)
            worker.process.assert_awaited_once()
            self.assertIs(worker.process.call_args.args[1], video)
            self.assertEqual(worker.music_queue.qsize(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
