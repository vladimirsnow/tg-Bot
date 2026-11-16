import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    token: str
    trigger: str
    chat_ids: frozenset[int]
    owner_user_id: int | None
    instagram_cookies: Path | None
    youtube_cookies: Path | None

    @classmethod
    def load(cls):
        load_dotenv(override=False)
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Укажи токен от BotFather в TELEGRAM_BOT_TOKEN.")
        trigger = os.getenv("BOT_TRIGGER", "").strip()
        if trigger and (len(trigger) > 64 or "\n" in trigger or "\r" in trigger):
            raise ValueError("BOT_TRIGGER должен быть одной строкой до 64 символов.")
        try:
            chat_ids = frozenset(int(value) for value in re.split(r"[,\s]+", os.getenv("ALLOWED_CHAT_IDS", ""))
                                 if value)
        except ValueError:
            raise ValueError("ALLOWED_CHAT_IDS должен содержать ID чатов через запятую.") from None
        if any(chat_id >= 0 for chat_id in chat_ids):
            raise ValueError("Укажи отрицательный ID группы в ALLOWED_CHAT_IDS.")
        owner = os.getenv("OWNER_USER_ID", "").strip()
        if owner and not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
            raise ValueError("OWNER_USER_ID должен содержать положительный ID пользователя.")

        def cookies(name: str) -> Path | None:
            if value := os.getenv(name, "").strip():
                path = Path(value).expanduser().resolve()
                if not path.is_file() or not os.access(path, os.R_OK):
                    raise ValueError(f"Файл из {name} недоступен для чтения.")
                return path
            return None

        return cls(token, trigger, chat_ids, int(owner) if owner else None, cookies("INSTAGRAM_COOKIES_FILE"),
                   cookies("YOUTUBE_COOKIES_FILE"))
