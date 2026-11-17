"""A killable music downloader, independent of the Telegram event loop."""
import asyncio
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

from media import Attachment, MediaError

LOG = logging.getLogger(__name__)


class MusicEngine:
    def __init__(self):
        self.process = None
        self.logs_task = None
        self.lock = asyncio.Lock()

    async def _logs(self, process):
        while line := await process.stderr.readline():
            safe = re.sub(r"https?://\S+|\d+:[A-Za-z0-9_-]{20,}", "<hidden>",
                          line.decode(errors="replace").strip())
            LOG.info("Музыка / %s", safe[:350])

    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        env = {key: value for key, value in os.environ.items()
               if key not in {"TELEGRAM_BOT_TOKEN", "YOUTUBE_COOKIES_BASE64"}}
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name("music_worker.py")),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
        self.logs_task = asyncio.create_task(self._logs(self.process))
        ready = await asyncio.wait_for(self.process.stdout.readline(), timeout=40)
        if ready.strip() != b'{"ready": true}':
            raise MediaError("Процесс поиска музыки не запустился")

    async def start(self):
        async with self.lock:
            try:
                await self._start()
            except BaseException:
                await self.abort()
                raise

    async def abort(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), timeout=3)
        if self.logs_task:
            self.logs_task.cancel()
            await asyncio.gather(self.logs_task, return_exceptions=True)
            self.logs_task = None

    async def request(self, action, source, directory, settings):
        async with self.lock:
            try:
                await self._start()
                data = {"action": action, "query": source, "directory": str(directory),
                        "cookies": str(settings.youtube_cookies) if settings.youtube_cookies else None}
                self.process.stdin.write((json.dumps(data) + "\n").encode())
                await self.process.stdin.drain()
                response = await self.process.stdout.readline()
                if not response:
                    raise MediaError("Процесс поиска музыки остановился")
                result = json.loads(response)
                if "error" in result:
                    raise MediaError(result["error"])
                item = result["attachment"]
                if item.get("path"):
                    item["path"] = Path(item["path"])
                return Attachment(**item)
            except MediaError:
                await self.abort()
                raise
            except BaseException:
                # Killing the whole process group also stops FFmpeg/Deno and
                # hung provider threads before the temporary directory is removed.
                await self.abort()
                raise
