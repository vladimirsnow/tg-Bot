# YouTube Cookies для Android

Локальное приложение для получения `YOUTUBE_COOKIES_BASE64` и файла `render-youtube.env`. Android 8.0+; APK для arm64 и 32-битных ARM опубликованы в [релизе](https://github.com/vladimirsnow/tg-Bot/releases/tag/cookie-helper-v1.0.0).

1. Установи APK подходящей архитектуры. Для большинства современных телефонов нужен arm64. Если Android спрашивает разрешение на установку, дай его только приложению, из которого открываешь APK.
2. Нажми **Новая сессия YouTube**, войди на настоящей HTTPS-странице Google и дождись YouTube.
3. Нажми **Получить cookies**. Приложение откроет `youtube.com/robots.txt`, заберёт cookies только YouTube и закроет страницу.
4. Нажми **Копировать для Render**. В Render → Environment замени значение `YOUTUBE_COOKIES_BASE64` и нажми **Save, rebuild, and deploy**. Вместо копирования можно сохранить `.env` и импортировать его.

Для повторного копирования приложение не открывает YouTube. Для новой авторизации есть отдельная кнопка. Экспортированную сессию не используй для просмотра видео: это уменьшает ротацию cookies, но Google всё равно может отозвать сессию.

Google может ограничить вход во встроенном браузере на конкретном устройстве. Тогда установи Firefox для Android и расширение [cookies.txt](https://addons.mozilla.org/firefox/addon/cookies-txt/), экспортируй только YouTube в Netscape TXT и выбери **Импорт cookies.txt** в приложении. На Android приложение не может читать cookies из уже установленного Chrome/Firefox; root не требуется и не используется.

Пароль вводится только на странице Google. Приложение не содержит токена Telegram и не отправляет cookies в Telegram, Render API или на сервер разработчика. Браузер общается с Google/YouTube для входа. Готовый экспорт зашифрован через Android Keystore, не выводится в журнал и исключён из системного резервного копирования. Сохранённый пользователем `.env` — отдельный файл с данными сессии; не публикуй его.

## Сборка

Нужны JDK 17+, Android SDK Platform 37.0, Build Tools 36.0.0. Gradle Wrapper 9.3.1 и Android Gradle Plugin 9.1.1 зафиксированы в проекте. Укажи `sdk.dir` в локальном `local.properties` или `ANDROID_HOME`.

```sh
./gradlew :app:testDebugUnitTest :app:connectedDebugAndroidTest :app:lintRelease :app:assembleRelease
```

Для теста устройства нужен Android Emulator или подключённый тестовый телефон. Подпись установочных сборок выполняется отдельно из корня репозитория:

```sh
python tools/cookie-helper/sign_apks.py --sdk "$ANDROID_HOME" --keystore /private/release.jks --password-file /private/password.txt --output artifacts
```

Ключ подписи и пароль не входят в репозиторий. Для последующих обновлений используй тот же ключ. Собранный APK требует ручной установки; публикации в Google Play и платных сервисов нет.

## Проверки

JVM-тесты проверяют Netscape/Base64, HTTP-only, сроки, фильтрацию доменов и повреждённые файлы. Android-тесты на вымышленных cookies проверяют настоящий путь Gecko cookie API → native messaging → зашифрованное хранилище, импорт TXT, повторное открытие приложения и генерацию `.env`. Тестовые команды расширения присутствуют только в debug assets и не включаются в release APK.

Вход в личный Google-аккаунт на физическом телефоне автоматически не тестируется. При отказе Google предусмотрен импорт из Firefox. GeckoView распространяется по MPL 2.0; лицензия и ссылка на исходники Firefox находятся в `assets/licenses`.
