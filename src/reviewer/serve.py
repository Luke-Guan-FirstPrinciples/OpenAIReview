"""Local HTTP server for the review visualization."""

import json
import os
import re
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

from . import __version__
from . import feedback_db
from .rate_limits import throttle


VIZ_DIR = Path(__file__).parent / "viz"
MAX_FEEDBACK_BYTES = 16 * 1024
RELATED_WORK_CACHE: dict[tuple[str, str], dict] = {}
S2_RATE_LIMIT_KEY = "semantic_scholar"
S2_RATE_LIMIT_ENV = "SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS"


def _s2_headers() -> dict[str, str]:
    headers = {"User-Agent": "openaireview/related-work"}
    api_key = os.environ.get("S2_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _extract_reference_text(paragraphs: list[dict]) -> str:
    texts = [str(p.get("text", "")) for p in paragraphs]
    for i, text in enumerate(texts):
        if re.search(r"^\s*(?:#{1,6}\s*)?(?:\d+\.?\s*)?(references|bibliography)\s*$", text, re.I | re.M):
            return "\n\n".join(texts[i:])
    return "\n\n".join(texts[-20:])


def _reference_candidates(reference_text: str, limit: int = 24) -> list[str]:
    chunks = re.split(r"\n\s*\n|(?=\n\s*(?:\[\d+\]|\d+\.|\[\w+\d{2,}\]))", reference_text)
    refs: list[str] = []
    for chunk in chunks:
        cleaned = re.sub(r"\s+", " ", chunk).strip()
        cleaned = re.sub(r"^#+\s*(references|bibliography)\s*", "", cleaned, flags=re.I).strip()
        if len(cleaned) < 35 or cleaned.lower() in {"references", "bibliography"}:
            continue
        refs.append(cleaned[:500])
        if len(refs) >= limit:
            break
    return refs


def _s2_search_reference(reference: str) -> dict | None:
    params = urlencode({
        "query": reference[:220],
        "limit": 1,
        "fields": "paperId,title,year,authors,citationCount,url,abstract,externalIds",
    })
    req = Request(
        f"https://api.semanticscholar.org/graph/v1/paper/search?{params}",
        headers=_s2_headers(),
    )
    throttle(S2_RATE_LIMIT_KEY, min_interval_env=S2_RATE_LIMIT_ENV, default_seconds=1.0)
    with urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") or []
    return data[0] if data else None


def _s2_recommend_from_ids(paper_ids: list[str], limit: int = 20) -> list[dict]:
    if not paper_ids:
        return []
    fields = "paperId,title,year,authors,citationCount,url,abstract,venue,externalIds"
    req = Request(
        f"https://api.semanticscholar.org/recommendations/v1/papers?{urlencode({'fields': fields, 'limit': limit})}",
        data=json.dumps({
            "positivePaperIds": paper_ids[:20],
            "negativePaperIds": [],
        }).encode("utf-8"),
        headers={**_s2_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    throttle(S2_RATE_LIMIT_KEY, min_interval_env=S2_RATE_LIMIT_ENV, default_seconds=1.0)
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("recommendedPapers") or payload.get("papers") or []


def _connected_papers_api_key() -> str:
    return (
        os.environ.get("CONNECTED_PAPERS_API_KEY")
        or os.environ.get("ConnectedPapers_API_KEY")
        or ""
    )


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _plain_connected_paper(paper: object) -> dict:
    if isinstance(paper, dict):
        paper_id = paper.get("id") or paper.get("paper_id") or paper.get("paperId") or ""
        authors = paper.get("authors") or []
        return {
            "paperId": paper_id,
            "title": paper.get("title", ""),
            "year": paper.get("year"),
            "authors": authors,
            "url": f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else "",
        }
    paper_id = (
        getattr(paper, "id", "")
        or getattr(paper, "paper_id", "")
        or getattr(paper, "paperId", "")
        or ""
    )
    return {
        "paperId": paper_id,
        "title": getattr(paper, "title", ""),
        "year": getattr(paper, "year", None),
        "authors": getattr(paper, "authors", []) or [],
        "url": f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else "",
    }


def _connected_papers(seed_ids: list[str], limit: int = 12) -> dict:
    if not seed_ids:
        return {"available": False, "reason": "No Semantic Scholar seed IDs found."}
    try:
        from connectedpapers import ConnectedPapersClient  # type: ignore
    except Exception as exc:
        return {"available": False, "reason": f"connectedpapers-py is not installed: {exc}"}
    api_key = _connected_papers_api_key()
    if not api_key:
        return {
            "available": False,
            "reason": "Set CONNECTED_PAPERS_API_KEY or ConnectedPapers_API_KEY to enable graph enrichment.",
        }
    try:
        graph_result = ConnectedPapersClient(access_token=api_key).get_graph_sync(seed_ids[0])
        graph = _field(graph_result, "graph_json")
        if graph is None:
            status = _field(graph_result, "status", "unknown")
            return {"available": False, "reason": f"Connected Papers returned no graph: {status}"}
        def plain(items):
            return [_plain_connected_paper(p) for p in list(items or [])[:limit]]
        return {
            "available": True,
            "seedPaperId": seed_ids[0],
            "status": str(_field(graph_result, "status", "")),
            "remaining_requests": _field(graph_result, "remaining_requests"),
            "common_references": plain(_field(graph, "common_references", [])),
            "common_citations": plain(_field(graph, "common_citations", [])),
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)}


def _build_related_work_payload(paper_data: dict) -> dict:
    reference_text = _extract_reference_text(paper_data.get("paragraphs", []))
    refs = _reference_candidates(reference_text)
    matched: list[dict] = []
    errors: list[str] = []
    for ref in refs:
        try:
            paper = _s2_search_reference(ref)
            if paper and paper.get("paperId") and paper["paperId"] not in {p.get("paperId") for p in matched}:
                matched.append(paper)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"{ref[:80]}: {exc}")
    seed_ids = [p["paperId"] for p in matched if p.get("paperId")]
    recommendations: list[dict] = []
    if seed_ids:
        try:
            recommendations = _s2_recommend_from_ids(seed_ids)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"Semantic Scholar recommendations: {exc}")
    return {
        "source": "Semantic Scholar Recommendations API",
        "reference_count": len(refs),
        "matched_references": matched,
        "recommended_papers": recommendations,
        "connected_papers": _connected_papers(seed_ids),
        "errors": errors,
    }


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
        elif self.path.startswith("/api/related-work/"):
            slug = unquote(self.path[len("/api/related-work/"):])
            self._serve_related_work(slug)
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

    def _serve_related_work(self, slug: str):
        json_path = self.results_dir / f"{slug}.json"
        if not json_path.exists():
            self.send_error(404, f"No results for: {slug}")
            return
        cache_key = (str(json_path.resolve()), str(json_path.stat().st_mtime_ns))
        if cache_key in RELATED_WORK_CACHE:
            self._send_json(RELATED_WORK_CACHE[cache_key])
            return
        try:
            data = json.loads(json_path.read_text())
            payload = _build_related_work_payload(data)
        except json.JSONDecodeError:
            self.send_error(500, f"Invalid JSON: {slug}.json")
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=502)
            return
        RELATED_WORK_CACHE[cache_key] = payload
        self._send_json(payload)

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
