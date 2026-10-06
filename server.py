"""One-page local UI over agent.run. Stdlib only.  python server.py  ->  http://127.0.0.1:8070

ponytail: the user switcher logs in with the seeded test users from demo_users.json. A real deployment would use
the person's own Odoo login (or API key) instead; the agent code would not change.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import agent
from odoo_rpc import OdooError, Session

PAGE = Path(__file__).with_name("index.html")


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/users":
            return self.reply(200, [{"key": k, "name": u["name"], "role": u["role"]} for k, u in agent.USERS.items()])
        if url.path == "/api/source":  # re-read one cited field live, as the same user
            u = agent.USERS.get(q.get("user"))
            if not u:
                return self.reply(400, {"error": "unknown user"})
            try:
                rec = Session(u["login"], u["password"]).read(q["model"], [int(q["id"])], [q["field"]])
                return self.reply(200, {"value": rec[0][q["field"]] if rec else None, "as": u["login"]})
            except OdooError as e:
                return self.reply(200, {"error": f"{e.kind}: {e.message}", "as": u["login"]})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/ask":
            return self.reply(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        if body.get("user") not in agent.USERS or not str(body.get("question", "")).strip():
            return self.reply(400, {"error": "need a known user and a question"})
        try:
            ans = agent.run(body["user"], body["question"].strip()[:500], use_llm=bool(body.get("llm", True)))
            agent.save_run(ans)
            self.reply(200, ans)
        except Exception as e:  # surface Odoo/agent errors in the page instead of a dropped connection
            self.reply(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print("Orchid reading phase on http://127.0.0.1:8070")
    ThreadingHTTPServer(("127.0.0.1", 8070), Handler).serve_forever()
