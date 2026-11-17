FROM denoland/deno:latest AS deno
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DENO_NO_UPDATE_CHECK=1 DENO_NO_PROMPT=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home bot
COPY --from=deno /usr/bin/deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py links.py media.py settings.py downloader.py runtime.py allowed_chats.json ./
USER bot
CMD ["python", "bot.py"]
