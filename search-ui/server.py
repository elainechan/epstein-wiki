#!/usr/bin/env python3
"""Serve the search UI and proxy:
  /api/*   → OpenSearch (localhost:9200)
  /embed   → Ollama nomic-embed-text (localhost:11434)
Traces every search + embed call to Langfuse when LANGFUSE_PUBLIC_KEY is set.
"""
import http.server, urllib.request, urllib.error, json, os, time

PORT        = int(os.environ.get("PORT", 8765))
OS_URL      = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = "nomic-embed-text"
DIR         = os.path.dirname(os.path.abspath(__file__))

# ── Langfuse (optional) ───────────────────────────────────────────────────────
try:
    from langfuse import Langfuse
    _lf = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
    )
    _TRACING = True
except Exception:
    _lf = None
    _TRACING = False


def _trace_search(body_bytes: bytes, response_bytes: bytes, latency_ms: float):
    if not _TRACING:
        return
    try:
        req_body = json.loads(body_bytes)
        resp     = json.loads(response_bytes)
        hits     = resp.get("hits", {})
        mode     = "semantic" if "knn" in str(req_body.get("query", "")) else \
                   "phrase"   if "match_phrase" in str(req_body.get("query", "")) else \
                   "hybrid"   if "should" in str(req_body.get("query", "")) else "fulltext"
        query_text = (
            req_body.get("query", {}).get("multi_match", {}).get("query") or
            req_body.get("query", {}).get("match_phrase", {}).get("text") or
            req_body.get("query", {}).get("bool", {}).get("must", [{}])[0]
                .get("multi_match", {}).get("query") or
            "(no query)"
        )
        result_ids = [h["_source"].get("resource_id") for h in hits.get("hits", [])[:10]
                      if "_source" in h]
        total = hits.get("total", {}).get("value", 0)

        trace = _lf.trace(name="search-ui", input={"query": query_text, "mode": mode})
        trace.span(
            name="opensearch-retrieve",
            input={"mode": mode, "size": req_body.get("size"), "from": req_body.get("from", 0)},
            output={"total": total, "returned": len(result_ids), "resource_ids": result_ids},
            metadata={"latency_ms": round(latency_ms)},
        )
        _lf.flush()
    except Exception:
        pass


def _trace_embed(body_bytes: bytes, response_bytes: bytes, latency_ms: float):
    if not _TRACING:
        return
    try:
        req  = json.loads(body_bytes)
        resp = json.loads(response_bytes)
        dims = len(resp.get("embedding", []))
        trace = _lf.trace(name="search-ui-embed", input={"prompt": req.get("prompt", "")})
        trace.span(
            name="ollama-embed",
            input={"model": req.get("model"), "prompt": req.get("prompt", "")},
            output={"dimensions": dims},
            metadata={"latency_ms": round(latency_ms)},
        )
        _lf.flush()
    except Exception:
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} → {args[1]}", flush=True)

    def do_GET(self):
        if self.path.split("?")[0] == "/":
            self._serve_file("index.html", "text/html")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b""

        if self.path == "/embed":
            resp = self._proxy(OLLAMA_URL + "/api/embeddings", body)
            if resp:
                _trace_embed.__wrapped__ = True  # avoid double-tracing
        elif self.path.startswith("/api/") and "_search" in self.path:
            t0   = time.monotonic()
            resp = self._proxy(OS_URL + self.path[4:], body, return_data=True)
            if resp:
                _trace_search(body, resp, (time.monotonic() - t0) * 1000)
        elif self.path.startswith("/api/"):
            self._proxy(OS_URL + self.path[4:], body)
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _proxy(self, url, body, return_data=False):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()

            # trace embed here (has access to data)
            if "/api/embeddings" in url:
                _trace_embed(body, data, (time.monotonic() - t0) * 1000)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return data if return_data else None
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        return None

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_file(self, filename, content_type):
        path = os.path.join(DIR, filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()


if __name__ == "__main__":
    os.chdir(DIR)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    print(f"Search UI  → http://localhost:{PORT}/")
    print(f"OpenSearch → {OS_URL}")
    print(f"Ollama     → {OLLAMA_URL}  (model: {EMBED_MODEL})")
    print(f"Langfuse   → {'enabled' if _TRACING else 'disabled (set LANGFUSE_PUBLIC_KEY)'}")
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
