"""Entry point: ``python -m openai_wyoming_proxy``."""

import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "INFO").lower(),
        access_log=os.getenv("ACCESS_LOG", "false").lower() in ("1", "true", "yes"),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
