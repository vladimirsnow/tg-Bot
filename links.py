import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit


@dataclass(frozen=True)
class Link:
    platform: str
    url: str
    post_id: str = ""
    is_photo: bool = False


URL_PATTERN = re.compile(
    r"(?<![\w@./-])(?:https?://[^\s<>]+|(?:[a-z0-9-]+\.)*"
    r"(?:instagram\.com|instagr\.am|tiktok\.com|youtube\.com|youtu\.be|x\.com|twitter\.com|"
    r"fixupx\.com|fxtwitter\.com)/[^\s<>]+)", re.IGNORECASE,
)


def parse_link(raw: str) -> Link | None:
    raw = raw.rstrip(".,!?;:)]}»\"'…")
    if len(raw) > 2048:
        return None
    try:
        parts = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = parts.hostname or ""
        if (parts.scheme not in {"https", "http"} or parts.username is not None
                or parts.password is not None or parts.port is not None):
            return None
    except ValueError:
        return None
    path = parts.path.rstrip("/")

    if host in {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}:
        if match := re.fullmatch(r"/(?:[A-Za-z0-9_.]+/)?(reels?|p|tv)/([A-Za-z0-9_-]{3,40})", path):
            kind, post_id = match[1].removesuffix("s"), match[2]
            return Link("instagram", f"https://www.instagram.com/{kind}/{post_id}/", post_id)
        if re.fullmatch(r"/share/(?:(?:reel|p)/)?[A-Za-z0-9_-]{3,64}", path):
            return Link("instagram", f"https://www.instagram.com{path}/")
    elif host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}:
        if host in {"vm.tiktok.com", "vt.tiktok.com"}:
            if re.fullmatch(r"/[A-Za-z0-9_-]{3,40}", path):
                return Link("tiktok", f"https://{host}{path}/")
        elif re.fullmatch(r"/t/[A-Za-z0-9_-]{3,40}", path):
            return Link("tiktok", f"https://www.tiktok.com{path}/")
        elif match := re.fullmatch(r"/(?:@[\w.-]+|share)/(video|photo)/(\d{5,25})", path, re.ASCII):
            return Link("tiktok", f"https://www.tiktok.com{path}", match[2], match[1] == "photo")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = path.lstrip("/")
        elif path.startswith("/shorts/"):
            video_id = path.removeprefix("/shorts/")
        elif path == "/watch":
            video_id = parse_qs(parts.query).get("v", [""])[0]
        else:
            return None
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return Link("youtube", f"https://www.youtube.com/watch?v={video_id}", video_id)
    elif host in {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com",
                  "mobile.twitter.com", "fxtwitter.com", "fixupx.com"}:
        if match := re.fullmatch(r"/(?:[A-Za-z0-9_]+/status|i/(?:web/)?status)/"
                                 r"(\d{1,25})(?:/(?:photo|video)/\d{1,2})?", path):
            return Link("twitter", f"https://x.com/i/status/{match[1]}", match[1])
    return None


def links_in(text: str, entity_links: list[str], limit: int = 3) -> list[Link]:
    seen = set()
    result = []
    for raw in [*entity_links, *(match[0] for match in URL_PATTERN.finditer(text))]:
        link = parse_link(raw)
        if link and link.url not in seen:
            result.append(link)
            seen.add(link.url)
            if len(result) == limit:
                break
    return result


def music_query(text: str, trigger: str) -> str | None:
    pattern = rf"(?<![\w@]){re.escape(trigger)}(?![\w@])(?:\s*[:：,]\s*|\s+|$)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return text[match.end():].strip()[:200]
    return None
