import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import extract
from youtube_session import session_copy, _jar


def content(value, extra=""):
    return ("# Netscape HTTP Cookie File\n"
            f".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\t{value}\n" + extra)


class YoutubeSessionTests(unittest.TestCase):
    def test_response_updates_are_private_and_staged_file_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.txt"
            source.write_text(content("before"))
            with session_copy(source) as staged:
                self.assertNotEqual(Path(staged), source)
                self.assertEqual(Path(staged).stat().st_mode & 0o777, 0o600)
                Path(staged).write_text(content("after"))
            self.assertEqual(next(iter(_jar(source))).value, "after")
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(staged).exists())

    def test_failed_download_does_not_replace_a_working_session(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.txt"
            original = content("before")
            source.write_text(original)
            with self.assertRaises(RuntimeError):
                with session_copy(source) as staged:
                    Path(staged).write_text(content("broken"))
                    raise RuntimeError("download failed")
            self.assertEqual(source.read_text(), original)
            self.assertFalse(Path(staged).exists())

    def test_concurrent_session_upload_wins_over_an_old_download(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.txt"
            source.write_text(content("before"))
            with session_copy(source) as staged:
                Path(staged).write_text(content("old-download"))
                source.write_text(content("new-login"))
            self.assertEqual(source.read_text(), content("new-login"))

    def test_expired_session_warning_prevents_saving_even_after_success(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.txt"
            original = content("before")
            source.write_text(original)
            with session_copy(source, allow_save=lambda: False) as staged:
                Path(staged).write_text(content("bad"))
            self.assertEqual(source.read_text(), original)

    def test_only_youtube_cookies_are_saved_and_session_expiration_zero_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.txt"
            source.write_text(content("before"))
            with session_copy(source) as staged:
                Path(staged).write_text(content("after",
                    ".example.com\tTRUE\t/\tFALSE\t2147483647\tFOREIGN\tvalue\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t0\tSESSION\tvalue\n"))
            jar = _jar(source)
            self.assertEqual({c.name for c in jar}, {"SID", "SESSION"})
            self.assertFalse(any(c.is_expired() for c in jar))

    def test_cookie_commit_happens_after_ytdlp_saves_response_cookies(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "cookies.txt"
            source.write_text(content("before"))
            with patch("yt_dlp.YoutubeDL") as backend:
                backend.return_value.__enter__.return_value.extract_info.return_value = {"title": "Video"}
                def save(*args):
                    Path(backend.call_args.args[0]["cookiefile"]).write_text(content("response"))
                backend.return_value.__exit__.side_effect = save
                extract("https://youtube.com/watch?v=test", {"cookiefile": str(source)})
            self.assertEqual(next(iter(_jar(source))).value, "response")
