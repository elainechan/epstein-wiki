#!/usr/bin/env python3
"""Serve the search UI and proxy:
  /api/*   → OpenSearch (localhost:9200)
  /embed   → Ollama nomic-embed-text (localhost:11434)
"""
import http.server, urllib.request, urllib.error, json, os

PORT       = int(os.environ.get("PORT", 8765))
OS_URL     = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = "nomic-embed-text"
DIR        = os.path.dirname(os.path.abspath(__file__))


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
            self._proxy(OLLAMA_URL + "/api/embeddings", body)
        elif self.path.startswith("/api/"):
            self._proxy(OS_URL + self.path[4:], body)
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _proxy(self, url, body):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(data)
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
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
