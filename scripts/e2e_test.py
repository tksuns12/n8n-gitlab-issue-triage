#!/usr/bin/env python3
"""End-to-end tests: real n8n + Postgres, mock GitLab, decision model and LLM. Run via scripts/test.sh."""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

N8N = "http://localhost:5678"
MOCK = "http://localhost:8099"
SECRET = "test-webhook-secret"
LONG = ("Steps: open the page, click the button, wait. Expected the list to load. "
        "Actual: nothing happens. Version 3.2.1 on Chrome 130.")

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f" :: {detail}"))
    if not ok:
        failures.append(name)


def request(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def mock_calls(iid=None):
    _, body = request("GET", f"{MOCK}/_calls")
    calls = json.loads(body)
    if iid is None:
        return calls
    return [c for c in calls if re.search(rf"/issues/{iid}(/|$)", c["path"])
            or (c["path"] in ("/v1/systemone", "/v1/chat/completions") and f"#{iid}#" in json.dumps(c["body"]))]


def send(iid, title, description=LONG, action="open", labels=(), key=None, token=SECRET, kind="issue"):
    key = key or f"test-{uuid.uuid4()}"
    payload = {
        "object_kind": kind,
        "user": {"username": "reporter"},
        "project": {"id": 42, "path_with_namespace": "demo/app"},
        "object_attributes": {
            "iid": iid, "action": action, "updated_at": "2026-10-14 10:00:00 UTC",
            # The marker lets mock_calls() pick out decision/LLM calls for this issue.
            "title": title, "description": f"{description}\n#{iid}#",
            "url": f"https://gitlab.example.com/demo/app/-/issues/{iid}",
        },
        "labels": [{"title": label} for label in labels],
    }
    status, _ = request("POST", f"{N8N}/webhook/gitlab/issues", json.dumps(payload).encode(), {
        "Content-Type": "application/json", "X-Gitlab-Event": "Issue Hook",
        "X-Gitlab-Token": token, "Idempotency-Key": key,
    })
    return status, key


def db(sql):
    out = subprocess.run(
        ["docker", "compose", "--env-file", ".env.test", "exec", "-T", "postgres",
         "psql", "-U", "n8n", "-d", "n8n", "-tA", "-c",
         f"select coalesce(json_agg(t), '[]'::json) from ({sql}) t"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def row(key):
    rows = db(f"select * from triage.issue_triage where event_key = '{key}'")
    return rows[0] if rows else None


def wait_status(key, statuses, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = row(key)
        if r and r["status"] in statuses:
            return r
        time.sleep(0.5)
    return row(key)


def review_url(iid):
    for c in mock_calls(iid):
        if c["body"].get("internal"):
            m = re.search(r"\((http[^)]+/form-waiting/[^)]+)\)", c["body"]["body"])
            if m:
                return m.group(1)
    return None


def submit_form(url, decision, type_label, severity, comment):
    boundary = uuid.uuid4().hex
    fields = {"field-0": decision, "field-1": type_label, "field-2": severity, "field-3": comment}
    body = "".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
                   for k, v in fields.items()) + f"--{boundary}--\r\n"
    return request("POST", url, body.encode(), {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def gitlab_writes(iid):
    calls = mock_calls(iid)
    puts = [c["body"].get("add_labels") for c in calls if c["method"] == "PUT"]
    public = [c["body"]["body"] for c in calls if c["path"].endswith("/notes") and not c["body"].get("internal")]
    internal = [c["body"]["body"] for c in calls if c["path"].endswith("/notes") and c["body"].get("internal")]
    return puts, public, internal


def test_rejects_wrong_token():
    print("wrong webhook secret")
    status, key = send(100, "CSV export fails with 500 error", token="wrong")
    time.sleep(1)
    check("responds 403", status == 403, status)
    check("nothing stored", row(key) is None)


def test_auto_apply_and_duplicate():
    print("clear bug → auto-applied, redelivery ignored")
    status, key = send(101, "CSV export fails with 500 error")
    check("responds 200", status == 200, status)
    r = wait_status(key, {"auto_applied"})
    check("status auto_applied", r and r["status"] == "auto_applied", r)
    puts, public, internal = gitlab_writes(101)
    check("labels added", puts == ["type::bug,severity::3"], puts)
    check("public note explains labels", len(public) == 1 and "type::bug" in public[0], public)
    check("no review request", internal == [], internal)

    decisions_before = len([c for c in mock_calls(101) if c["path"] == "/v1/systemone"])
    send(101, "CSV export fails with 500 error", key=key)
    time.sleep(3)
    decisions_after = len([c for c in mock_calls(101) if c["path"] == "/v1/systemone"])
    check("same Idempotency-Key is processed once", decisions_after == decisions_before == 1,
          (decisions_before, decisions_after))
    check("still one row", len(db(f"select id from triage.issue_triage where event_key = '{key}'")) == 1)


def test_needs_info_corrected_by_reviewer():
    print("vague issue → LLM draft → reviewer corrects labels and edits comment")
    _, key = send(102, "Search is weird", description="its slow sometimes")
    r = wait_status(key, {"waiting_review"})
    check("status waiting_review", r and r["status"] == "waiting_review", r)
    check("route needs_info", r and r["route"] == "needs_info", r)
    check("LLM drafted a question", any(c["path"] == "/v1/chat/completions" for c in mock_calls(102)))
    check("draft stored", r and "재현 순서" in (r["draft_comment"] or ""), r)
    _, public, internal = gitlab_writes(102)
    check("review request is an internal note", len(internal) == 1 and "@reviewer" in internal[0], internal)
    check("nothing public before review", public == [], public)

    url = review_url(102)
    check("review link is signed", url and "signature=" in url, url)
    status, html = request("GET", url)
    check("form opens with draft prefilled", status == 200 and "재현 순서" in html, status)
    status, _ = submit_form(url, "적용", "type::bug", "severity::3", "재현 순서와 버전을 알려 주세요.")
    check("form accepted", status == 200, status)
    r = wait_status(key, {"corrected", "approved"})
    check("status corrected", r and r["status"] == "corrected", r)
    check("final labels from reviewer", r and r["final_labels"] == ["type::bug", "severity::3"], r)
    puts, public, _ = gitlab_writes(102)
    check("reviewer labels applied", puts == ["type::bug,severity::3"], puts)
    check("edited comment posted", public == ["재현 순서와 버전을 알려 주세요."], public)


def test_security_rejected():
    print("security suspicion → no LLM, reviewer rejects")
    _, key = send(103, "API token leaked in job log")
    r = wait_status(key, {"waiting_review"})
    check("route security", r and r["route"] == "security", r)
    check("no LLM call", not any(c["path"] == "/v1/chat/completions" for c in mock_calls(103)))
    submit_form(review_url(103), "거절 (직접 분류)", "type::bug", "없음", "")
    r = wait_status(key, {"rejected"})
    check("status rejected", r and r["status"] == "rejected", r)
    puts, public, _ = gitlab_writes(103)
    check("handed to a person", puts == ["triage::needs-human"], puts)
    check("no public comment", public == [], public)


def test_low_confidence_approved_as_is():
    print("low confidence → reviewer approves suggestion unchanged")
    _, key = send(104, "Dashboard tiles")
    r = wait_status(key, {"waiting_review"})
    check("route review", r and r["route"] == "review", r)
    submit_form(review_url(104), "적용", "type::feature", "없음", "")
    r = wait_status(key, {"approved", "corrected"})
    check("status approved", r and r["status"] == "approved", r)
    puts, public, _ = gitlab_writes(104)
    check("suggested label applied", puts == ["type::feature"], puts)
    check("empty comment is not posted", public == [], public)


def test_decision_model_down_falls_back_to_review():
    print("decision model overloaded → retries, then a person classifies")
    _, key = send(105, "[decision-down] Add dark mode support")
    r = wait_status(key, {"waiting_review"}, timeout=60)
    check("route review", r and r["route"] == "review", r)
    check("reason recorded", r and r["route_reason"].startswith("결정 모델 호출 실패"), r)
    tries = len([c for c in mock_calls(105) if c["path"] == "/v1/systemone"])
    check("retried 3 times", tries == 3, tries)
    _, _, internal = gitlab_writes(105)
    check("note says no suggestion", internal and "제안 없음" in internal[0], internal)


def test_ignored_events():
    print("events that must not be triaged")
    _, k1 = send(106, "CSV export fails with 500 error", action="update")
    _, k2 = send(107, "CSV export fails with 500 error", labels=["type::bug"])
    _, k3 = send(108, "CSV export fails with 500 error", kind="note")
    time.sleep(3)
    check("update action ignored", row(k1) is None)
    check("already typed ignored", row(k2) is None)
    check("non-issue event ignored", row(k3) is None)
    check("no outside calls", all(mock_calls(i) == [] for i in (106, 107, 108)))


if __name__ == "__main__":
    request("POST", f"{MOCK}/_reset")
    for t in (test_rejects_wrong_token, test_auto_apply_and_duplicate, test_needs_info_corrected_by_reviewer,
              test_security_rejected, test_low_confidence_approved_as_is, test_decision_model_down_falls_back_to_review,
              test_ignored_events):
        t()
    print(db("select * from triage.review_outcomes order by route"))
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    sys.exit(1 if failures else 0)
