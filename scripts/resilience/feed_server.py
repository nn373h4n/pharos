"""The fake feed host of the resilience check (M5-08): static files, and one slow one.

Used only by scripts/resilience-check.sh, in the throwaway stack, in place of the plain file server
of the stack tests. Everything under /feeds is served as it is; `/slow.xml` is sent in small
pieces with a pause between them, so that a fetch of it is open for several seconds, long enough
to stop the worker in the middle of it.
"""

import http.server
import time

DIRECTORY = "/feeds"
PIECES = 12
PAUSE_SECONDS = 0.5


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=DIRECTORY, **kwargs)  # type: ignore[arg-type]

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/slow.xml":
            super().do_GET()
            return
        with open(f"{DIRECTORY}/slow.xml", "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        size = max(1, len(body) // PIECES)
        for start in range(0, len(body), size):
            self.wfile.write(body[start : start + size])
            self.wfile.flush()
            time.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    # All interfaces of the container: the worker reaches it over the internal network of the stack
    # tests, which has no route out. This file is never part of an installation.
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
