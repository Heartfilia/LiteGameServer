#!/usr/bin/env python3
import uvicorn

from litegame_server.config import Settings


if __name__ == "__main__":
    settings = Settings.load()
    uvicorn.run("litegame_server.app:create_app", factory=True,
                host=settings.host, port=settings.port, proxy_headers=False)
