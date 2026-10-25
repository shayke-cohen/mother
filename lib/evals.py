"""mother eval — A/B test a routine change on the same inputs before it becomes the default.

First case: the brief's gather mode, `inline` (MOTHER reads every source) against `readers` (one
`mother:reader` sub-agent per source). Both run back to back as headless dry runs: the same workspace, the
same "as of" time, and MOTHER_DRY_RUN set so the guard denies every outbound call. They are scored on the
same evidence. The manager then picks one blind, as "Brief X" or "Brief Y".

Commands (JSON out):
  brief [--first inline|readers] [--model M] [--timeout MIN] [--skip-permissions]
                         run one pair -> {pair, X, Y, scores, cost}; order alternates unless --first
  verdict PAIR x|y|same [--note TEXT]
                         record the manager's blind pick (appends .mother/eval/results.jsonl)
  report [--min-pairs N] the totals and the ship rule -> {decision: ship|hold|not enough data, reasons}

Scores per variant:
  recall         share of the pair's cited items (both briefs, deduped by link) this brief carries
  cited          share of its list lines that carry a link; `uncited` counts the others
  false_quiet    sources the other brief cited items from, while this one neither cited nor flagged them
  cost           total_cost_usd, tokens (subagents included when the CLI reports modelUsage), wall time
Dead links aren't checked: that needs every connector, and a dry run reads them only through Claude.

Pairs live in <workspace>/.mother/eval/<date>/<time>/ (machine-local, gitignored).
Python 3.9 stdlib only.
"""
import datetime as _dt
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import items  # noqa: E402

VARIANTS = ("inline", "readers")
SHIP = {"min_pairs": 10, "min_preference": 0.6, "max_cost_ratio": 3.0}

PROMPT = """Run the mother:brief skill as a DRY RUN for an A/B eval.
- Gather mode: {gather}
- As of: {as_of} (treat this as now)
- Write the finished brief, markdown only, to: {path}
Write nothing else and post nothing: no triage file, no drafts, no audio, no DM. Outbound calls are denied
in this run. When the file is written, reply with the single word DONE."""


# ---------------------------------------------------------------- basics

def now():
    v = os.environ.get("MOTHER_NOW")
    return _dt.datetime.fromtimestamp(float(v)) if v else _dt.datetime.now()


def fail(msg, code=1):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
        fail("missing value for %s" % name)
    return default


def workspace():
    ws = os.environ.get("MOTHER_WORKSPACE")
    if not ws:
        state = Path(os.environ.get("MOTHER_STATE_DIR") or Path.home() / ".mother")
        try:
            ws = (state / "workspace").read_text().strip()
        except OSError:
            fail("no workspace: set MOTHER_WORKSPACE or run `mother init`")
    p = Path(ws).expanduser()
    if not (p / "config.json").is_file():
        fail("%s is not a MOTHER workspace (no config.json)" % p)
    return p


def eval_dir(ws):
    return ws / ".mother" / "eval"


# ---------------------------------------------------------------- scoring

LIST_LINE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S")


def couldnt_sources(text):
    out = set()
    for line in (text or "").splitlines():
        low = line.lower()
        if "couldn't check" in low or "couldnt check" in low or "could not check" in low:
            out.update(s for s in items.SOURCES if re.search(r"\b%s\b" % s, low))
    return out


def score(text):
    """Per-brief facts that don't need the other brief."""
    lines = [l for l in (text or "").splitlines() if LIST_LINE.match(l) and "⚠️" not in l
             and "couldn't check" not in l.lower()]
    cited = [l for l in lines if items.links_in(l)]
    links = items.links_in(text)
    return {"links": links, "lines": len(lines), "uncited": len(lines) - len(cited),
            "cited": round(len(cited) / len(lines), 3) if lines else 1.0,
            "couldnt": sorted(couldnt_sources(text)),
            "sources": sorted({s for s in (items.source_of(l) for l in links) if s})}


def score_pair(texts):
    """texts: {variant: markdown} -> {variant: scores}, with recall and false_quiet against the other."""
    s = {v: score(t) for v, t in texts.items()}
    union = set()
    for v in s:
        union.update(s[v]["links"])
    for v in s:
        other = [o for o in s if o != v]
        s[v]["recall"] = round(len(set(s[v]["links"]) & union) / len(union), 3) if union else 1.0
        seen = set(s[v]["sources"]) | set(s[v]["couldnt"])
        s[v]["false_quiet"] = sorted({src for o in other for src in s[o]["sources"]} - seen)
        s[v]["n_links"] = len(s[v].pop("links"))
    return s


# ---------------------------------------------------------------- running a pair

def tokens_of(res):
    mu = res.get("modelUsage")
    src = list(mu.values()) if isinstance(mu, dict) and mu else [res.get("usage") or {}]
    n = 0
    for u in src:
        if isinstance(u, dict):
            for k, v in u.items():
                if "token" in k.lower() and isinstance(v, (int, float)) and not isinstance(v, bool):
                    n += int(v)
    return n


def run_variant(ws, gather, as_of, path, model, timeout, skip_permissions):
    exe = os.environ.get("MOTHER_CLAUDE_BIN", "claude")
    cmd = [exe, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if skip_permissions:
        cmd += ["--dangerously-skip-permissions"]
    env = dict(os.environ, MOTHER_DRY_RUN="1", MOTHER_NO_SYNC="1", MOTHER_WORKSPACE=str(ws))
    env.pop("MOTHER_CHANNEL_ROLE", None)
    prompt = PROMPT.format(gather=gather, as_of=as_of, path=path)
    started = now()
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=str(ws), env=env,
                           timeout=timeout * 60)
        out, err = p.stdout, (p.stderr.strip() if p.returncode else "")
    except subprocess.TimeoutExpired:
        out, err = "", "timed out after %d min" % timeout
    except OSError as e:
        out, err = "", "could not start claude: %s" % e
    try:
        res = json.loads(out) if out.strip() else {}
    except ValueError:
        res = {}
    text = path.read_text() if path.is_file() else ""
    if not text and res.get("result") and str(res["result"]).strip() != "DONE":
        text = str(res["result"])
        path.write_text(text)
    rec = {"path": str(path), "cost_usd": res.get("total_cost_usd"), "tokens": tokens_of(res),
           "duration_ms": res.get("duration_ms") or int((now() - started).total_seconds() * 1000),
           "turns": res.get("num_turns"), "error": err[-300:] or (res.get("subtype") if res.get("is_error") else None)}
    if not text:
        rec["error"] = rec["error"] or "no brief written"
    return text, rec


def all_pairs(ws):
    d = eval_dir(ws)
    out = []
    for p in sorted(d.glob("*/*/pair.json")) if d.is_dir() else []:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


def cmd_brief(ws, args):
    first = opt(args, "--first")
    if first and first not in VARIANTS:
        fail("--first must be inline or readers")
    if not first:  # alternate, so neither variant always reads the fresher data
        first = VARIANTS[len(all_pairs(ws)) % 2]
    order = [first] + [v for v in VARIANTS if v != first]
    t = now()
    pid = t.strftime("%Y-%m-%d-%H%M%S")
    d = eval_dir(ws) / t.strftime("%Y-%m-%d") / t.strftime("%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    as_of = t.astimezone().isoformat(timespec="minutes")
    timeout = int(opt(args, "--timeout", "20"))
    texts, runs = {}, {}
    for v in order:
        texts[v], runs[v] = run_variant(ws, v, as_of, d / ("%s.md" % v), opt(args, "--model"), timeout,
                                        "--skip-permissions" in args)
    scores = score_pair(texts)
    for v in VARIANTS:
        runs[v].update(scores[v])
    labels = dict(zip(("X", "Y"), random.sample(VARIANTS, 2)))
    pair = {"pair": pid, "as_of": as_of, "order": order, "labels": labels, "variants": runs, "verdict": None}
    (d / "pair.json").write_text(json.dumps(pair, indent=1, ensure_ascii=False))
    print(json.dumps({"pair": pid, "X": runs[labels["X"]]["path"], "Y": runs[labels["Y"]]["path"],
                      "errors": {v: runs[v]["error"] for v in VARIANTS if runs[v]["error"]},
                      "pairs_so_far": len(all_pairs(ws))}, indent=1))


def find_pair(ws, pid):
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})-(\d{6})", pid or "")
    p = eval_dir(ws) / m.group(1) / m.group(2) / "pair.json" if m else None
    if not p or not p.is_file():
        fail("no eval pair %s" % pid)
    return p


def cmd_verdict(ws, args):
    if len(args) < 2 or args[1].lower() not in ("x", "y", "same"):
        fail("usage: mother eval verdict PAIR x|y|same [--note TEXT]")
    path = find_pair(ws, args[0])
    pair = json.loads(path.read_text())
    pick = args[1].lower()
    winner = "same" if pick == "same" else pair["labels"][pick.upper()]
    pair["verdict"] = {"pick": pick, "winner": winner, "note": opt(args, "--note", ""),
                       "at": now().astimezone().isoformat(timespec="seconds")}
    path.write_text(json.dumps(pair, indent=1, ensure_ascii=False))
    rec = {"pair": pair["pair"], "winner": winner, "note": pair["verdict"]["note"], "at": pair["verdict"]["at"]}
    for v in VARIANTS:
        r = pair["variants"].get(v) or {}
        rec[v] = {k: r.get(k) for k in ("recall", "cited", "uncited", "false_quiet", "cost_usd", "tokens",
                                        "duration_ms", "error")}
    with open(eval_dir(ws) / "results.jsonl", "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({"pair": pair["pair"], "winner": winner}))


# ---------------------------------------------------------------- report and the ship rule

def report(pairs, min_pairs=SHIP["min_pairs"]):
    ok = [p for p in pairs if not any((p["variants"].get(v) or {}).get("error") for v in VARIANTS)]
    tot = {v: {"recall": 0.0, "uncited": 0, "false_quiet": 0, "cost_usd": 0.0, "tokens": 0, "duration_ms": 0}
           for v in VARIANTS}
    for p in ok:
        for v in VARIANTS:
            r = p["variants"][v]
            tot[v]["recall"] += r.get("recall") or 0
            tot[v]["uncited"] += r.get("uncited") or 0
            tot[v]["false_quiet"] += len(r.get("false_quiet") or [])
            tot[v]["cost_usd"] += r.get("cost_usd") or 0
            tot[v]["tokens"] += r.get("tokens") or 0
            tot[v]["duration_ms"] += r.get("duration_ms") or 0
    n = len(ok)
    for v in VARIANTS:
        tot[v]["recall"] = round(tot[v]["recall"] / n, 3) if n else None
        tot[v]["cost_usd"] = round(tot[v]["cost_usd"], 4)
    judged = [p["verdict"]["winner"] for p in ok if p.get("verdict")]
    picks = {w: judged.count(w) for w in VARIANTS + ("same",)}
    pref = round((picks["readers"] + picks["same"]) / len(judged), 3) if judged else None
    base_cost = tot["inline"]["cost_usd"] or tot["inline"]["tokens"]
    new_cost = tot["readers"]["cost_usd"] if tot["inline"]["cost_usd"] else tot["readers"]["tokens"]
    ratio = round(new_cost / base_cost, 2) if base_cost else None
    out = {"pairs": len(pairs), "usable": n, "failed": len(pairs) - n, "judged": len(judged), "picks": picks,
           "readers_or_same": pref, "cost_ratio": ratio, "totals": tot}
    if len(judged) < min_pairs:
        out.update(decision="not enough data",
                   reasons=["%d judged pairs, need %d" % (len(judged), min_pairs)])
        return out
    r, i = tot["readers"], tot["inline"]
    checks = [
        (r["recall"] >= i["recall"], "recall %s vs inline %s" % (r["recall"], i["recall"])),
        (r["uncited"] <= i["uncited"], "uncited lines %d vs inline %d" % (r["uncited"], i["uncited"])),
        (r["false_quiet"] <= i["false_quiet"], "false quiet %d vs inline %d" % (r["false_quiet"], i["false_quiet"])),
        (pref is not None and pref >= SHIP["min_preference"],
         "manager picked readers or same in %s of pairs (need %s)" % (pref, SHIP["min_preference"])),
        (ratio is not None and ratio <= SHIP["max_cost_ratio"],
         "cost %sx inline (max %sx)" % (ratio, SHIP["max_cost_ratio"])),
    ]
    out["decision"] = "ship" if all(c[0] for c in checks) else "hold"
    out["reasons"] = [("ok: " if c[0] else "FAIL: ") + c[1] for c in checks]
    return out


def cmd_report(ws, args):
    print(json.dumps(report(all_pairs(ws), int(opt(args, "--min-pairs", SHIP["min_pairs"]))), indent=1))


COMMANDS = {"brief": cmd_brief, "verdict": cmd_verdict, "report": cmd_report}


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    cmd = COMMANDS.get(argv[0])
    if not cmd:
        fail("unknown eval command '%s' — see `mother eval help`" % argv[0])
    cmd(workspace(), argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
