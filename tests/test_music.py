import asyncio
import sys
import httpx
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import Job, MediaBot
from music_engine import MusicEngine
from settings import Settings
from media import track_matches, ytdlp, search_audius, download_track, Attachment, MediaError
from threading import Event


SETTINGS = Settings("", "", frozenset(), 123, None, None)


class MusicTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def test_audius_rejects_paid_or_gated_audio(self):
        response = httpx.Response(200, request=httpx.Request("GET", "https://api.audius.co/search"),
            json={"data": [{"id": "abc", "title": "Song", "user": {"name": "Artist"},
                            "is_streamable": True, "is_stream_gated": True}]})
        with tempfile.TemporaryDirectory() as temp, patch("media.httpx.Client") as client, \
             patch("media.fetch") as fetch:
            client.return_value.__enter__.return_value.get.return_value = response
            with self.assertRaises(MediaError):
                search_audius("Artist Song", Path(temp), Event())
            fetch.assert_not_called()

    def test_audius_can_win_when_other_music_sources_fail(self):
        result = Attachment("audio", Path("/tmp/full-song.mp3"), "Song", "Artist")
        with tempfile.TemporaryDirectory() as temp, \
             patch("media.search_download", side_effect=MediaError("Unavailable")), \
             patch("media.search_audius", return_value=result):
            self.assertIs(download_track("Artist Song", Path(temp), SETTINGS), result)

    def test_preview_audio_is_excluded_from_all_format_fallbacks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "download.mp3").write_bytes(b"audio")
            with patch("downloader.extract", return_value={"title": "Song"}) as backend:
                ytdlp("https://soundcloud.com/example/song", root, None, audio=True)
                for selector in backend.call_args.args[1]["format"].split("/"):
                    self.assertIn("format_id!*=preview", selector)

    async def test_expired_queue_request_does_not_start_another_search(self):
        worker = MediaBot(SETTINGS)
        worker._process = AsyncMock()
        worker.say = AsyncMock()
        job = Job(SimpleNamespace(), track="song", queued_at=time.monotonic() - 61)
        self.assertFalse(await worker.process(SimpleNamespace(), job))
        worker._process.assert_not_awaited()
        worker.say.assert_awaited_once()

    def test_similar_song_title_does_not_replace_requested_song(self):
        self.assertTrue(track_matches("The end my chemical romance", "The End.", "My Chemical Romance"))
        self.assertFalse(track_matches("The end my chemical romance", "To the End", "My Chemical Romance"))
        self.assertTrue(track_matches("династия", "ДИНАСТИЯ", "VILLIAN & madk1d"))

    async def test_stuck_downloader_process_is_killed_on_timeout(self):
        engine = MusicEngine()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(300)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        engine.process = process
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(engine.request("search", "song", Path(temp), SETTINGS), timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(engine.process)
        self.assertIsNotNone(process.returncode)

    async def test_whole_music_job_times_out_and_clears_progress(self):
        worker = MediaBot(SETTINGS)

        async def stuck(*args):
            await asyncio.Event().wait()

        worker._process = stuck
        worker.finish_status = AsyncMock()
        with patch("bot.MUSIC_TIMEOUT", 0.05):
            result = await worker.process(SimpleNamespace(), Job(SimpleNamespace(), track="song"))
        self.assertFalse(result)
        worker.finish_status.assert_awaited_once()
        self.assertFalse(worker.active_music)

    async def test_process_can_restart_after_cancellation(self):
        engine = MusicEngine()
        try:
            await engine.start()
            first = engine.process.pid
            await engine.abort()
            await engine.start()
            self.assertNotEqual(first, engine.process.pid)
        finally:
            await engine.abort()
