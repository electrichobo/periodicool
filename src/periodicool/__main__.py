"""Run with `python -m periodicool`."""

import uvicorn

from periodicool.config import load_settings
from periodicool.main import create_app


def main() -> None:
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
