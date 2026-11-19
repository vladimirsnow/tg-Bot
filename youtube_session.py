"""Keep successful YouTube cookie updates without overwriting concurrent sessions."""
import fcntl
import http.cookiejar
import logging
import os
import warnings
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

LOG = logging.getLogger(__name__)
DOMAINS = {"youtube.com", "www.youtube.com", "music.youtube.com"}
AUTH_NAMES = {"SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}


def _jar(path):
    jar = http.cookiejar.MozillaCookieJar(str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        jar.load(ignore_discard=True, ignore_expires=True)
    result = http.cookiejar.MozillaCookieJar()
    for cookie in jar:
        # yt-dlp writes session cookies with expiration 0 instead of an empty field.
        if cookie.expires == 0:
            cookie.expires, cookie.discard = None, True
        if cookie.domain.lstrip(".") in DOMAINS and not cookie.is_expired():
            result.set_cookie(cookie)
    return result


def _values(jar):
    return {(c.domain, c.path, c.name): (c.value, c.expires, c.secure, c.discard, c._rest)
            for c in jar}


def _save(source, baseline, staged):
    temporary = None
    try:
        original, updated = _jar(source), _jar(staged)
        if not updated or _values(original) == _values(updated):
            return
        if (any(c.name in AUTH_NAMES for c in original)
                and not any(c.name in AUTH_NAMES for c in updated)):
            return
        fd = os.open(str(source) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # A new upload or another download wins over an older snapshot.
            if source.read_bytes() != baseline:
                return
            with NamedTemporaryFile(prefix=".youtube-cookies-", suffix=".txt", dir=source.parent, delete=False) as handle:
                temporary = Path(handle.name)
            updated.save(str(temporary), ignore_discard=True, ignore_expires=False)
            temporary.chmod(0o600)
            os.replace(temporary, source)
            temporary = None
        LOG.info("YouTube / обновления cookies сохранены")
    except (OSError, ValueError, http.cookiejar.LoadError) as exc:
        # Read-only user cookie files must not make a successful download fail.
        LOG.info("YouTube / сохранение cookies: %s", type(exc).__name__)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def session_copy(source, allow_save=lambda: True):
    source = Path(source).resolve()
    baseline = source.read_bytes()
    with NamedTemporaryFile(prefix="telegram-youtube-session-", suffix=".txt", delete=False) as handle:
        staged = Path(handle.name)
        handle.write(baseline)
    try:
        yield str(staged)
        if allow_save():
            _save(source, baseline, staged)
    finally:
        staged.unlink(missing_ok=True)
