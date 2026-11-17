import asyncio
import json
import tempfile
import threading
import unittest
import httpx
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

from bot import Job, MediaBot, HealthHandler
from links import parse_link
from media import Attachment, MediaError, Post, gallery, recover_post, load_post, download_post, Remote
from settings import Settings


SETTINGS = Settings("", "", frozenset(), 123, None, None)


class RecoveryTests(unittest.TestCase):
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
                self.assertEqual(native.call_args.kwargs, {"timeout": 45})

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
