"""Per-run loopback gateway: authenticate metadata, lazily verify payloads."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import shutil
import threading
from urllib.parse import unquote, urlsplit

from .repository import validate_index


@contextmanager
def gateway(snapshots, cache):
    for snapshot in snapshots:
        validate_index(snapshot, cache)
    token = secrets.token_urlsafe(24)
    by_publisher = {s.publisher: s for s in snapshots}
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = unquote(urlsplit(self.path).path).lstrip("/").split("/", 2)
            if len(parts) != 3 or parts[0] != token or parts[1] not in by_publisher:
                self.send_error(404)
                return
            snapshot, name = by_publisher[parts[1]], parts[2]
            if name not in snapshot.manifest["files"]:
                self.send_error(404)
                return
            try:
                path = snapshot.file(name, cache)
                self.send_response(200)
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                errors.append(str(exc))
                self.send_error(502, "Repository verification or download failed")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield [f"http://127.0.0.1:{server.server_port}/{token}/{s.publisher}" for s in snapshots]
        if errors:
            raise ValueError("; ".join(errors))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
