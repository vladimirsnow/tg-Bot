import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest

from bot import Job, MediaBot
from links import parse_link
from media import Attachment, MediaError, Post, gallery, recover_post
from settings import Settings


SETTINGS = Settings("", "", frozenset(), 123, None, None)


class RecoveryTests(unittest.TestCase):
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
