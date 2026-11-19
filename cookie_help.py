"""Static owner-only help; it never reads or accepts account cookies."""

RELEASE = "https://github.com/vladimirsnow/tg-Bot/releases/download/cookie-helper-v1.0.0"
RELEASE_PAGE = "https://github.com/vladimirsnow/tg-Bot/releases/tag/cookie-helper-v1.0.0"
APK_URL = RELEASE + "/YouTube-Cookies-arm64-v1.0.0.apk"
CONVERTER_URL = RELEASE + "/cookies-render.html"

COOKIE_HELP = f"""🍪 <b>Новые cookies YouTube для Render</b>

<b>На Android — приложение</b>
<a href="{APK_URL}">Скачать YouTube Cookies APK</a> · Android 8+, большинство современных телефонов.
<a href="{RELEASE_PAGE}">32-битный APK и подробности</a> — если основной APK не устанавливается.

1. Установи APK и нажми «Новая сессия YouTube».
2. Войди в свой аккаунт на странице Google. Затем нажми «Получить cookies».
3. Нажми «Копировать для Render». Это только значение переменной, без её названия.
4. В Render → твой сервис → Environment замени <code>YOUTUBE_COOKIES_BASE64</code> и нажми <b>Save, rebuild, and deploy</b>.

Приложение закроет страницу YouTube после экспорта. Для повторного копирования открывать её не нужно.

Если Google не разрешил вход внутри приложения: открой Firefox на телефоне, установи <a href="https://addons.mozilla.org/firefox/addon/cookies-txt/">cookies.txt</a>, войди в YouTube в отдельном сеансе и экспортируй cookies только текущего сайта. В APK нажми «Импорт cookies.txt» и выбери файл.

<b>На ПК — Firefox</b>
1. Установи <a href="https://addons.mozilla.org/firefox/addon/cookies-txt/">cookies.txt</a> и разреши расширению работу в приватных окнах.
2. В отдельном приватном окне войди на youtube.com. В этой же вкладке открой <code>https://www.youtube.com/robots.txt</code>.
3. Через расширение экспортируй cookies только YouTube в Netscape TXT и закрой приватное окно.
4. Скачай <a href="{CONVERTER_URL}">локальный конвертер</a>, открой его в браузере и выбери TXT. Он работает без отправки файла на сервер.
5. Нажми «Копировать значение» и вставь результат в <code>YOUTUBE_COOKIES_BASE64</code> в Render → Environment → <b>Save, rebuild, and deploy</b>.

<b>Важно</b>
Cookies дают доступ к сессии аккаунта: не отправляй их в Telegram и не загружай в онлайн-конвертеры. Отдельную экспортированную сессию больше не используй для просмотра YouTube. Google всё равно может её отозвать; бессрочный доступ не гарантируется.

Эта команда доступна только владельцу в личке. Бот не принимает файлы cookies через чат и не меняет Render Environment самостоятельно."""
