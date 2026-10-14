#!/usr/bin/env python3
"""Compares decision models and a generative LLM on the issue-type question.

Every model gets the same instructions and criteria as the workflow's
"Decision model: classify" node. Decision models (Jev, Mercury Decide, ...) answer
through the /v1/systemone schema with a choice and a confidence. The LLM answers
through JSON-schema structured output. Keys come from .env and are never printed.

    python3 eval/run_eval.py --limit 10                          # quick check
    python3 eval/run_eval.py --models inception/mercury-decide:free,llm
    python3 eval/run_eval.py --resume eval/results/<file>.jsonl  # continue after a daily limit
    python3 eval/run_eval.py --summarize eval/results/<file>.jsonl --llm-price 0.30 2.50
    python3 eval/run_eval.py --llm-base-url https://openrouter.ai/api/v1 \
        --llm-model google/gemini-2.5-flash --llm-key-var OPENROUTER_API_KEY   # LLM through OpenRouter
"""

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
TYPES = ["bug", "feature", "maintenance"]
CATALOG = "https://openrouter.ai/api/v1/models?output_modalities=all"

# Keep in sync with the "Decision model: classify" node in workflows/issue-triage.json.
INSTRUCTIONS = "What kind of work does this GitLab issue ask for? Judge from `title` and `description`."
CRITERIA = {
    "bug": "Something that works or is documented to work is broken: errors, crashes, wrong results, regressions.",
    "feature": "A new capability, or a change to existing behaviour that users would notice.",
    "maintenance": "Upkeep with no user-facing change: refactoring, dependency upgrades, tech debt, tooling, CI.",
}


def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.split(" #", 1)[0].strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    # Common names work too.
    env.setdefault("DECISION_API_KEY", env.get("OPENROUTER_API_KEY", ""))
    env.setdefault("LLM_API_KEY", env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY", ""))
    return env


def post(url, body, headers, tries=4):
    data = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.load(res), (time.perf_counter() - started) * 1000
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (500, 502, 503, 529) and attempt < tries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def ask_decision_model(env, model, issue):
    res, ms = post(f"{env.get('DECISION_BASE_URL', 'https://openrouter.ai/api')}/v1/systemone", {
        "model": model,
        "state": {"title": issue["title"], "description": issue["description"]},
        "questions": {"type": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}},
    }, {"Authorization": f"Bearer {env['DECISION_API_KEY']}"})
    answer = res["answers"]["type"]
    usage = res.get("usage", {})
    pred = answer.get("choice")
    return {"pred": pred if pred in TYPES else None, "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities"), "ms": round(ms),
            "input_tokens": usage.get("input_tokens"), "cost": usage.get("cost"),
            "served_by": res.get("model")}


def ask_llm(env, issue):
    criteria = "\n".join(f"- {k}: {v}" for k, v in CRITERIA.items())
    base = env.get("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    extra = {"usage": {"include": True}} if "openrouter.ai" in base else {}
    res, ms = post(f"{base}/chat/completions", {
        **extra,
        "model": env.get("LLM_MODEL", "gemini-2.5-flash"),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": f"{INSTRUCTIONS}\nAnswer with one of:\n{criteria}"},
            {"role": "user", "content": json.dumps({"title": issue["title"], "description": issue["description"]},
                                                   ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "issue_type", "strict": True,
            "schema": {"type": "object", "additionalProperties": False, "required": ["type"],
                       "properties": {"type": {"type": "string", "enum": TYPES}}},
        }},
    }, {"Authorization": f"Bearer {env['LLM_API_KEY']}"})
    content = res["choices"][0]["message"]["content"] or ""
    try:
        pred = json.loads(content)["type"]
    except (json.JSONDecodeError, KeyError, TypeError):
        pred = None
    usage = res.get("usage", {})
    return {"pred": pred if pred in TYPES else None, "raw": None if pred in TYPES else content[:200],
            "ms": round(ms), "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"), "cost": usage.get("cost"),
            "served_by": res.get("model")}


def catalog_prices(models):
    """USD per token from OpenRouter's public catalog, for decision models without usage.cost."""
    try:
        with urllib.request.urlopen(CATALOG, timeout=30) as res:
            data = json.load(res)["data"]
    except Exception:  # noqa: BLE001 - cost line is optional
        return {}
    return {m["id"]: float(m["pricing"].get("prompt") or 0) for m in data if m["id"] in models}


def pct(n, d):
    return f"{100 * n / d:.1f}%" if d else "-"


def is_llm(model):
    return model == "llm" or model.startswith("llm:")


def summarize(records, llm_price):
    # A resumed run can hold an error and a later success for the same pair; the success wins.
    latest = {}
    for r in records:
        key = (r["iid"], r["model"])
        if key not in latest or "error" in latest[key]:
            latest[key] = r
    by_model = defaultdict(list)
    for r in latest.values():
        by_model[r["model"]].append(r)
    prices = catalog_prices([m for m in by_model if not is_llm(m)])
    lines = [f"# 평가 결과 ({datetime.now():%Y-%m-%d})", "",
             "정답: `gitlab-org/gitlab` 공개 이슈에 GitLab 팀이 붙인 `type::` 라벨. 질문과 기준은 워크플로와 같습니다.", ""]
    preds = {}
    for model, rows in by_model.items():
        done = [r for r in rows if "error" not in r]
        errors = len(rows) - len(done)
        name = model
        if not done:
            lines += [f"## {name}", "", f"- 호출 실패 {errors}건, 성공 0건", ""]
            continue
        preds[model] = {r["iid"]: r["pred"] for r in done}
        correct = sum(r["pred"] == r["type"] for r in done)
        invalid = sum(r["pred"] is None for r in done)
        ms = sorted(r["ms"] for r in done)
        lines += [f"## {name}", "",
                  f"- 정확도: {correct}/{len(done)} = {pct(correct, len(done))} "
                  f"(형식 오류 {invalid}건은 오답으로 셈, 호출 실패 {errors}건은 제외)",
                  f"- 지연: 중앙값 {statistics.median(ms):.0f}ms, p95 {ms[int(0.95 * (len(ms) - 1))]:.0f}ms"]
        lines.append("- 유형별 재현율: " + ", ".join(
            f"{t} {sum(r['pred'] == t for r in done if r['type'] == t)}/{sum(r['type'] == t for r in done)}"
            for t in TYPES))
        tokens_in = sum(r.get("input_tokens") or 0 for r in done)
        if is_llm(model):
            tokens_out = sum(r.get("output_tokens") or 0 for r in done)
            billed = [r["cost"] for r in done if r.get("cost") is not None]
            if len(billed) == len(done):
                lines.append(f"- 비용: 1,000건당 ${1000 * sum(billed) / len(done):.4f} "
                             f"(OpenRouter 청구액, 입력 {tokens_in:,} · 출력 {tokens_out:,} 토큰)")
            elif llm_price:
                cost = tokens_in * llm_price[0] / 1e6 + tokens_out * llm_price[1] / 1e6
                lines.append(f"- 비용: 1,000건당 ${1000 * cost / len(done):.4f} "
                             f"(입력 ${llm_price[0]}/M · 출력 ${llm_price[1]}/M 기준)")
            else:
                lines.append(f"- 토큰: 입력 {tokens_in:,} · 출력 {tokens_out:,}")
        else:
            reported = [r["cost"] for r in done if r.get("cost") is not None]
            cost = sum(reported) if len(reported) == len(done) else tokens_in * prices.get(model, 0)
            lines.append(f"- 비용: 1,000건당 ${1000 * cost / len(done):.4f} (입력 {tokens_in:,} 토큰)")
            lines += ["", "| 확신도 기준 | 자동 처리 비율 | 그 안의 정확도 |", "|---|---|---|"]
            for th in (0.5, 0.6, 0.7, 0.8, 0.9):
                sure = [r for r in done if (r.get("confidence") or 0) >= th]
                ok = sum(r["pred"] == r["type"] for r in sure)
                lines.append(f"| ≥ {th} | {pct(len(sure), len(done))} ({len(sure)}건) | {pct(ok, len(sure))} |")
        lines.append("")
    models = list(preds)
    for i, a in enumerate(models):
        for b in models[i + 1:]:
            common = [iid for iid in preds[a] if iid in preds[b] and preds[a][iid] and preds[b][iid]]
            agree = sum(preds[a][iid] == preds[b][iid] for iid in common)
            lines.append(f"- {a} 와 {b} 의 답이 같은 비율: {pct(agree, len(common))} ({len(common)}건)")
    return "\n".join(lines).rstrip() + "\n"


def read_records(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--env", default=str(HERE.parent / ".env"))
    p.add_argument("--models", help="comma-separated decision model IDs and/or 'llm' (default: DECISION_MODEL,llm)")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int)
    p.add_argument("--resume", help="results file to append to; finished (issue, model) pairs are skipped")
    p.add_argument("--summarize", help="only rebuild RESULTS.md from a results file")
    p.add_argument("--decision-rpm", type=float, default=18, help="OpenRouter free models allow 20/min")
    p.add_argument("--llm-rpm", type=float, default=8)
    p.add_argument("--paid-rpm", type=float, default=120, help="pace for paid decision models")
    p.add_argument("--llm-price", type=float, nargs=2, metavar=("IN", "OUT"), help="USD per 1M tokens")
    p.add_argument("--llm-base-url", help="overrides LLM_BASE_URL")
    p.add_argument("--llm-model", help="overrides LLM_MODEL")
    p.add_argument("--llm-key-var", help="name of the .env variable holding the LLM key, e.g. OPENROUTER_API_KEY")
    args = p.parse_args()

    env = load_env(args.env)
    if args.llm_base_url:
        env["LLM_BASE_URL"] = args.llm_base_url
    if args.llm_model:
        env["LLM_MODEL"] = args.llm_model
    if args.llm_key_var:
        env["LLM_API_KEY"] = env[args.llm_key_var]
    llm_label = f"llm:{env.get('LLM_MODEL', 'gemini-2.5-flash')}"
    if args.summarize:
        summary = summarize(read_records(args.summarize), args.llm_price)
        (HERE / "RESULTS.md").write_text(summary, encoding="utf-8")
        print(summary)
        return

    models = (args.models or f"{env.get('DECISION_MODEL', 'inception/mercury-decide:free')},llm").split(",")
    issues = [json.loads(line) for line in open(HERE / ".cache" / "issues.jsonl", encoding="utf-8")]
    issues = issues[args.offset:][: args.limit] if args.limit else issues[args.offset:]

    out_path = Path(args.resume) if args.resume else HERE / "results" / f"{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    done = set()
    if out_path.exists():
        done = {(r["iid"], r["model"]) for r in read_records(out_path) if "error" not in r}

    def gap(model):
        if is_llm(model):
            return 60 / args.llm_rpm
        return 60 / (args.decision_rpm if model.endswith(":free") else args.paid_rpm)

    last = defaultdict(float)
    stop = False
    with open(out_path, "a", encoding="utf-8") as out:
        for i, issue in enumerate(issues, 1):
            marks = []
            for model in models:
                label = llm_label if is_llm(model) else model
                if (issue["iid"], label) in done:
                    continue
                time.sleep(max(0.0, gap(label) - (time.time() - last[label])))
                last[label] = time.time()
                try:
                    result = ask_llm(env, issue) if is_llm(model) else ask_decision_model(env, model, issue)
                except Exception as e:  # noqa: BLE001 - recorded, not fatal
                    result = {"error": str(e)}
                    if "HTTP 429" in str(e) or "HTTP 402" in str(e):
                        stop = True
                out.write(json.dumps({"iid": issue["iid"], "type": issue["type"], "model": label, **result},
                                     ensure_ascii=False) + "\n")
                out.flush()
                marks.append(f"{label.split('/')[-1]}={result.get('pred', 'ERR')}")
                if stop:
                    break
            print(f"[{i}/{len(issues)}] #{issue['iid']} {issue['type']:<11} {'  '.join(marks) or 'done'}", flush=True)
            if stop:
                print(f"\nrate or credit limit hit; continue later with --resume {out_path}")
                break

    summary = summarize(read_records(out_path), args.llm_price)
    (HERE / "RESULTS.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"raw results: {out_path}")


if __name__ == "__main__":
    main()
