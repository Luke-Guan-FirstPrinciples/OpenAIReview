"""Local HTTP server for the review visualization."""

import json
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from . import __version__
from . import feedback_db


VIZ_DIR = Path(__file__).parent / "viz"
MAX_FEEDBACK_BYTES = 16 * 1024


class ReviewHandler(SimpleHTTPRequestHandler):
    """Custom handler that serves the viz UI and result data."""

    def __init__(self, *args, results_dir: str = "./review_results", **kwargs):
        self.results_dir = Path(results_dir)
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
        elif self.path == "/data/index.json":
            self._serve_data_index()
        elif self.path.startswith("/data/") and self.path.endswith(".json"):
            slug = self.path[len("/data/"):-len(".json")]
            self._serve_paper_data(slug)
        elif self.path == "/api/feedback/status":
            self._send_json({"configured": feedback_db.is_configured()})
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/feedback":
            self._handle_feedback()
        else:
            self.send_error(404, "Not Found")

    def _handle_feedback(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_FEEDBACK_BYTES:
            self.send_error(400, "Invalid Content-Length")
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400, "Invalid JSON")
            return
        if not isinstance(payload, dict):
            self.send_error(400, "Expected JSON object")
            return

        payload.setdefault("user_agent", self.headers.get("User-Agent"))

        if not feedback_db.is_configured():
            self._send_json(
                {"error": "feedback storage not configured"},
                status=503,
            )
            return

        try:
            row_id = feedback_db.insert_feedback(payload)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:
            sys.stderr.write(f"[serve] feedback insert failed: {exc}\n")
            self._send_json({"error": "internal error"}, status=500)
            return

        self._send_json({"id": row_id})

    def _serve_index(self):
        html_path = VIZ_DIR / "index.html"
        if not html_path.exists():
            self.send_error(500, "index.html not found in package")
            return
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("<!-- __VERSION__ -->", f"v{__version__}")
        content = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_data_index(self):
        """Dynamically build index.json from result files in results_dir."""
        papers = []
        if self.results_dir.is_dir():
            for f in sorted(self.results_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                    # Only include files that look like paper results
                    if "paragraphs" not in data or "methods" not in data:
                        continue
                    papers.append({
                        "slug": data.get("slug", f.stem),
                        "title": data.get("title", f.stem),
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
        index = {"papers": papers}
        self._send_json(index)

    def _serve_paper_data(self, slug: str):
        """Serve a paper's result JSON."""
        json_path = self.results_dir / f"{slug}.json"
        if not json_path.exists():
            self.send_error(404, f"No results for: {slug}")
            return
        try:
            data = json.loads(json_path.read_text())
            self._send_json(data)
        except json.JSONDecodeError:
            self.send_error(500, f"Invalid JSON: {slug}.json")

    def _send_json(self, data: dict, status: int = 200):
        content = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        # Quieter logging
        sys.stderr.write(f"[serve] {args[0]}\n")


def run_server(results_dir: str = "./review_results", port: int = 8081) -> None:
    """Start the visualization server."""
    results_path = Path(results_dir)
    if not results_path.is_dir():
        print(f"Warning: results directory does not exist: {results_path}")
        print("  Run 'openaireview review <file>' first to generate results.")

    handler = partial(ReviewHandler, results_dir=results_dir)
    server = HTTPServer(("0.0.0.0", port), handler)
    print(f"Serving review visualization at http://localhost:{port}")
    print(f"Results directory: {results_path.resolve()}")
    if feedback_db.is_configured():
        print("Feedback storage: configured (Postgres)")
    else:
        print("Feedback storage: disabled (set DB_HOST/DB_NAME/DB_USER/DB_PASSWORD and install 'openaireview[feedback]')")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()
