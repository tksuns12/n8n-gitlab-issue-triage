"""Mock of the three outside APIs the workflow calls, for tests without keys.

- Decision model: POST /v1/systemone  (Jev / Mercury Decide schema)
- Text LLM:       POST /v1/chat/completions  (OpenAI-compatible)
- GitLab REST:    PUT  /api/v4/projects/:id/issues/:iid
                  POST /api/v4/projects/:id/issues/:iid/notes
- Test helpers:   GET /_calls, POST /_reset

Decisions are keyword rules so tests are deterministic. A title containing
"[decision-down]" makes the decision model answer 529 (overloaded).
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DECISION_KEY = "Bearer test-decision-key"
LLM_KEY = "Bearer test-llm-key"
GITLAB_TOKEN = "test-gitlab-token"

CALLS = []

BUG_WORDS = ("error", "crash", "broken", "fails", "500", "exception", "오류", "안 됨")
FEATURE_WORDS = ("add ", "support", "allow", "would be nice", "추가", "기능")
MAINT_WORDS = ("refactor", "upgrade", "bump", "cleanup", "deprecat", "정리")
SECURITY_WORDS = ("token", "leak", "xss", "password", "vulnerab", "유출")


def decide(state):
    text = f"{state.get('title', '')} {state.get('description', '')}".lower()

    def has(words):
        return any(w in text for w in words)

    if has(BUG_WORDS):
        choice, conf = "bug", 0.91
    elif has(MAINT_WORDS):
        choice, conf = "maintenance", 0.88
    elif has(FEATURE_WORDS):
        choice, conf = "feature", 0.86
    else:
        choice, conf = "feature", 0.41
    others = [o for o in ("bug", "feature", "maintenance") if o != choice]
    rest = round((1 - conf) / 2, 3)
    probs = {choice: conf, others[0]: rest, others[1]: rest}

    short = len(state.get("description", "")) < 60
    return {
        "type": {"type": "choice", "choice": choice, "probabilities": probs, "confidence": conf},
        "severity": {
            "type": "score",
            "score": 1.2,
            "legend": {"0": "minor", "1": "some users", "2": "core broken", "3": "outage"},
            "probabilities": {"0": 0.15, "1": 0.55, "2": 0.25, "3": 0.05},
            "confidence": 0.74,
        },
        "security": {"type": "noul", "noul": 0.93 if has(SECURITY_WORDS) else 0.04},
        "needs_info": {"type": "noul", "noul": 0.82 if short else 0.12},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {"_raw": raw.decode(errors="replace")}

    def _record(self, body):
        CALLS.append({"method": self.command, "path": self.path, "body": body})

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path}", flush=True)

    def do_GET(self):
        if self.path == "/_calls":
            return self._send(200, CALLS)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = self._body()
        if self.path == "/_reset":
            CALLS.clear()
            return self._send(200, {"ok": True})

        if self.path == "/v1/systemone":
            self._record(body)
            if self.headers.get("Authorization") != DECISION_KEY:
                return self._send(401, {"error": "invalid api key"})
            state = body.get("state") or {}
            if "[decision-down]" in str(state.get("title", "")):
                return self._send(529, {"error": "overloaded"})
            return self._send(200, {
                "model": "decision-mock-1",
                "answers": decide(state),
                "usage": {"input_tokens": 400, "output_tokens": 0},
            })

        if self.path == "/v1/chat/completions":
            self._record(body)
            if self.headers.get("Authorization") != LLM_KEY:
                return self._send(401, {"error": "invalid api key"})
            return self._send(200, {
                "choices": [{"message": {"role": "assistant", "content":
                    "확인을 위해 몇 가지 알려 주세요.\n- 재현 순서\n- 기대한 결과와 실제 결과\n- 사용 중인 버전"}}],
            })

        if re.fullmatch(r"/api/v4/projects/\d+/issues/\d+/notes", self.path):
            self._record(body)
            if self.headers.get("PRIVATE-TOKEN") != GITLAB_TOKEN:
                return self._send(401, {"message": "401 Unauthorized"})
            return self._send(201, {"id": len(CALLS), "body": body.get("body")})

        self._send(404, {"error": "not found"})

    def do_PUT(self):
        body = self._body()
        if re.fullmatch(r"/api/v4/projects/\d+/issues/\d+", self.path):
            self._record(body)
            if self.headers.get("PRIVATE-TOKEN") != GITLAB_TOKEN:
                return self._send(401, {"message": "401 Unauthorized"})
            labels = [label for label in str(body.get("add_labels", "")).split(",") if label]
            return self._send(200, {"iid": int(self.path.rsplit("/", 1)[1]), "labels": labels})
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
