import json
import logging
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

from downloader import warmup
from media import download_track, download_track_local, error_detail
from settings import Settings


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    with redirect_stdout(sys.stderr):
        warmup()
    print('{"ready": true}', flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            settings = Settings("", "", frozenset(), None, None,
                                Path(request["cookies"]) if request.get("cookies") else None)
            operation = download_track if request["action"] == "search" else download_track_local
            with redirect_stdout(sys.stderr):
                attachment = operation(request["query"], Path(request["directory"]), settings)
            result = {"attachment": asdict(attachment)}
        except Exception as exc:
            result = {"error": error_detail(exc)}
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
