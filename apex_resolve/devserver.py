"""In-process threaded HTTP server for integration testing and interactive demonstrations."""
import socket
import threading
import time
from typing import Optional

import uvicorn


def free_port() -> int:
    """Find an available unallocated TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ThreadedServer:
    """Run a FastAPI / ASGI application in a background daemon thread."""

    def __init__(self, app, port: Optional[int] = None):
        self.port = port or free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
