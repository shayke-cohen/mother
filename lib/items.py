"""mother items — the evidence check for what a `mother:reader` sub-agent hands back (docs/DESIGN.md "Sub-agents").

A reader returns one JSON object: {source, coverage{status, reason, opened, unopened}, items[]}. Its claims
reach the brief only through this check, so two rules hold in code rather than in a prompt:

  evidence or silence     an item without a link (URL, ticket key, PR ref or workspace path) is dropped
  couldn't check != quiet a reader that failed, timed out or returned garbage becomes `couldnt_check`, never
                          an empty source

Commands (JSON out):
  check [--file F] [--max N]   stdin (or F) = a reader's reply, prose and ```json fences allowed
                               -> {source, coverage, items, dropped, truncated, warning}
                               exit 0 = usable (even if coverage is couldnt_check); 2 = malformed

Python 3.9 stdlib only.
"""
import json
import re
import sys

STATUSES = ("ok", "partial", "couldnt_check")
SOURCES = ("calendar", "mail", "chat", "tracker", "code")
MAX_ITEMS = 25
NOTE_MAX = 200

URL = re.compile(r"https?://[^\s<>()\]\"'`]+")
TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
PR_REF = re.compile(r"\b[\w.-]+/[\w.-]+#\d+\b")
PATH = re.compile(r"^(?:[\w.-]+/)+[\w.-]+\.(?:md|json|jsonl)$")


def is_link(v):
    """A citation MOTHER can follow: a URL, a ticket key, owner/repo#N, or a workspace file path."""
    if not isinstance(v, str):
        return False
    v = v.strip()
    if PATH.fullmatch(v):
        return ".." not in v.split("/")
    return bool(URL.fullmatch(v) or TICKET.fullmatch(v) or PR_REF.fullmatch(v))


def links_in(text):
    """Every citation in a piece of markdown, URLs first, in order of appearance, deduped."""
    seen, out = set(), []
    urls = [u.rstrip(".,;:") for u in URL.findall(text or "")]
    rest = URL.sub(" ", text or "")
    for v in urls + PR_REF.findall(rest) + TICKET.findall(PR_REF.sub(" ", rest)):
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def source_of(link):
    """Best-effort source of a citation, for telling which reader a brief line came from. None = unknown."""
    l = link.lower()
    if "calendar" in l:
        return "calendar"
    if "mail." in l or "/mail/" in l or "outlook." in l:
        return "mail"
    if "slack.com" in l or "teams.microsoft" in l:
        return "chat"
    if "/pull/" in l or "/merge_requests/" in l or PR_REF.fullmatch(link):
        return "code"
    if TICKET.fullmatch(link) or "atlassian.net" in l or "linear.app" in l or "monday.com" in l:
        return "tracker"
    return None


def extract(raw):
    """The reader's JSON object: the whole reply, else the last ```json fence, else the outermost {...}."""
    raw = (raw or "").strip()
    cands = [raw] + list(reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)))
    i, j = raw.find("{"), raw.rfind("}")
    if 0 <= i < j:
        cands.append(raw[i:j + 1])
    for c in cands:
        try:
            v = json.loads(c)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    raise ValueError("no JSON object in the reader's reply")


def couldnt(source, reason):
    return {"source": source or "unknown", "coverage": {"status": "couldnt_check", "reason": reason},
            "items": [], "dropped": 0, "truncated": 0,
            "warning": "⚠️ %s: couldn't check — %s" % (source or "a source", reason)}


def check(raw, max_items=MAX_ITEMS):
    """Returns (code, normalized). code 2 = malformed; the normalized result is then a couldnt_check."""
    try:
        r = extract(raw)
    except ValueError as e:
        return 2, couldnt(None, "reader output unreadable (%s)" % e)
    src = r.get("source")
    if not isinstance(src, str) or not src.strip():
        return 2, couldnt(None, "reader output has no source")
    src = src.strip().lower()
    cov = r.get("coverage")
    if not isinstance(cov, dict) or cov.get("status") not in STATUSES:
        return 2, couldnt(src, "reader output has no coverage status")
    items = r.get("items")
    if not isinstance(items, list):
        return 2, couldnt(src, "reader output has no items list")
    coverage = {"status": cov["status"], "reason": str(cov.get("reason") or "")[:NOTE_MAX]}
    for k in ("opened", "unopened"):
        if isinstance(cov.get(k), int) and not isinstance(cov.get(k), bool):
            coverage[k] = cov[k]
    kept, seen, dropped = [], set(), 0
    for it in items:
        if not isinstance(it, dict) or not is_link(it.get("link")) or not str(it.get("title") or "").strip():
            dropped += 1
            continue
        link = it["link"].strip()
        if link in seen:
            continue
        seen.add(link)
        kept.append({"kind": str(it.get("kind") or "item"), "title": str(it["title"]).strip()[:NOTE_MAX],
                     "who": str(it.get("who") or ""), "when": str(it.get("when") or ""), "link": link,
                     "state": str(it.get("state") or ""), "note": str(it.get("note") or "")[:NOTE_MAX]})
    truncated = max(0, len(kept) - max_items)
    kept = kept[:max_items]
    warning = None
    if coverage["status"] == "couldnt_check":
        warning = "⚠️ %s: couldn't check — %s" % (src, coverage["reason"] or "no reason given")
    elif coverage["status"] == "partial":
        warning = "⚠️ %s: partial — %s" % (src, coverage["reason"] or "no reason given")
    elif dropped and not kept:
        # Everything it found was uncited: that is not the same as a quiet source.
        coverage = {"status": "couldnt_check", "reason": "%d uncited items, none usable" % dropped}
        warning = "⚠️ %s: couldn't check — %s" % (src, coverage["reason"])
    return 0, {"source": src, "coverage": coverage, "items": kept, "dropped": dropped, "truncated": truncated,
               "warning": warning}


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    if argv[0] != "check":
        print(json.dumps({"error": "unknown items command '%s'" % argv[0]}), file=sys.stderr)
        sys.exit(1)
    args = argv[1:]
    f = args[args.index("--file") + 1] if "--file" in args and args.index("--file") + 1 < len(args) else None
    try:
        n = int(args[args.index("--max") + 1]) if "--max" in args else MAX_ITEMS
    except (ValueError, IndexError):
        n = MAX_ITEMS
    try:
        raw = open(f).read() if f else sys.stdin.read()
    except OSError as e:
        raw = ""
        code, res = 2, couldnt(None, "can't read %s (%s)" % (f, e.strerror))
    else:
        code, res = check(raw, n)
    print(json.dumps(res, indent=1, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    main(sys.argv[1:])
