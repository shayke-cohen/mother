#!/usr/bin/env python3
"""Render the weekly sprint plan page from one week's JSON.

Usage: python3 build.py <plan.json> <out.html>

A static page: no script, so it renders anywhere (Stash, a file preview, a phone). The design lives in
template.html; only the JSON changes per week. Validates the top-level keys so a half-written data file
fails loudly instead of rendering an empty section.
"""
import html
import json
import sys
from collections import Counter
from pathlib import Path

REQUIRED = ["meta", "context", "feedback", "last", "next", "decisions"]
CHANGES = ("in", "done", "add", "assign", "create")

# change → (pill text, tone). Blue = already there, green = done, orange = this plan adds, red = this plan creates.
CHANGE_PILL = {"in": ("in sprint", "accent"), "done": ("done", "good"), "add": ("add to sprint", "warn"),
               "assign": ("add + assign", "warn"), "create": ("create", "bad")}
STATE_PILL = {"done": ("done", "good"), "carry": ("→ next sprint", "accent"), "open": ("not done", "warn"),
              "split": ("split or close", "bad"), "decide": ("decide", "bad"), "move": ("off our sprint", "bad")}

e = html.escape


def pill(text, tone):
    return f'<span class="pill p-{tone}">{e(str(text))}</span>' if text else ""


def deltas(card):
    """One line per earlier value: +12% red when worse, green when better, blue when flat or neutral."""
    try:
        now = float(card["value"])
    except (TypeError, ValueError):
        return ""
    hib = card.get("higherIsBetter")
    out = ""
    for p in card.get("prev", []):
        was = float(p["value"])
        d = now - was
        txt = ("+" if d > 0 else "−" if d < 0 else "±") + (f"{abs(round(d / was * 100))}%" if was else str(abs(d)))
        tone = "accent" if d == 0 or hib is None else ("good" if (d > 0) == hib else "bad")
        lab = p.get("label", "")
        on = "on " if lab[:1].isdigit() else ""
        out += f'<div class="dl">{pill(txt, tone)} vs {"~" if p.get("estimate") else ""}{e(str(p["value"]))} {on}{e(lab)}</div>'
    return out


def cards(rows):
    return '<div class="stats">' + "".join(
        f'<div class="stat"><b>{e(str(c["value"]))}</b><div class="lbl">{e(c["label"])}</div>{deltas(c)}'
        f'<span>{e(c.get("note", ""))}</span></div>' for c in rows) + "</div>"


def render(d):
    m = d["meta"]
    browse = m.get("tracker_url", "")
    board = m.get("board_url")
    label = m.get("label", "proposed")

    def key(k):
        if not k:
            return '<span class="key new">NEW</span>'
        return f'<a class="key" href="{e(browse + k)}" target="_blank" rel="noopener">{e(k)}</a>' if browse else f'<span class="key">{e(k)}</span>'

    def link(url, text):
        return f'<a href="{e(url)}" target="_blank" rel="noopener">{e(text)}</a>'

    nx = d["next"]
    items = [i for g in nx["goals"] for i in g["items"]]
    bad = [i.get("key") or i["summary"] for i in items if i.get("change") not in CHANGES]
    if bad:
        sys.exit(f"build.py: unknown change on {', '.join(bad)} (use one of {', '.join(CHANGES)})")
    other = nx.get("other", [])
    load_now = Counter(i["owner"] for i in items if i["change"] == "in") + Counter(o["owner"] for o in other)
    load_after = Counter(i["owner"] for i in items if i["change"] != "done") + Counter(o["owner"] for o in other)
    n_add = sum(1 for i in items if i["change"] in ("add", "assign"))
    n_create = sum(1 for i in items if i["change"] == "create")
    n_assign = sum(1 for i in items if i["change"] == "assign")
    in_sprint = nx.get("inSprint", sum(1 for i in items if i["change"] in ("in", "done")) + len(other))
    load_html = " · ".join(f"{e(p)} <b>{load_now[p]}</b>" + (f" → <b>{n}</b>" if n != load_now[p] else "")
                           for p, n in load_after.most_common())

    goals_html = ""
    for gi, g in enumerate(nx["goals"], 1):
        rows = "".join(
            f'<tr><td>{key(i.get("key"))}</td><td>{e(i["summary"])}</td><td class="own">{e(i["owner"])}'
            + (f'<div class="dl">from {e(i["from"])}</div>' if i.get("from") else "")
            + f'</td><td>{pill(*CHANGE_PILL[i["change"]])}</td></tr>' for i in g["items"])
        goals_html += (f'<section class="goal"><h3><span class="gn">{gi}</span>{e(g["title"])}</h3>'
                       f'<p class="dt">Done when: {e(g["done"])}</p><table>{rows}</table></section>')
    if other:
        rows = "".join(f'<tr><td>{key(o["key"])}</td><td>{e(o["summary"])}</td><td class="own">{e(o["owner"])}</td>'
                       f'<td>{e(o.get("status", ""))}</td></tr>' for o in other)
        goals_html += (f'<section class="goal"><h3>Also in {e(m["sprint"])}, outside the goals</h3>'
                       f'<p class="dt">Keep, hand over or move out — see Decisions.</p><table>{rows}</table></section>')

    L = d["last"]
    last_html = ""
    if L.get("leadtime"):
        last_html += f'<h3 class="sec">Ticket lead time — created → done</h3>{cards(L["leadtime"]["cards"])}'
        last_html += f'<p class="note">{e(L["leadtime"].get("note", ""))}</p>'
    for s in L.get("streams", []):
        rows = "".join(f'<tr><td>{key(i.get("key"))}</td><td>{e(i["text"])}</td><td>{pill(*STATE_PILL[i["state"]])}</td></tr>'
                       for i in s["items"])
        last_html += f'<section class="goal"><h3>{e(s["title"])}</h3><table>{rows}</table></section>'

    C = d["context"]
    ctx_html = "".join(f'<h3 class="sec">{e(g["title"])}</h3>{cards(g["cards"])}' for g in C.get("groups", []))
    if C.get("breakdown"):
        ctx_html += '<div class="grid3">' + "".join(
            f'<div class="goal t-{b.get("tone", "accent")}"><h3>{e(b["title"])}</h3><p class="dt">{e(b["text"])}</p></div>'
            for b in C["breakdown"]) + "</div>"

    F = d["feedback"]
    fb_html = '<div class="grid2">' + "".join(
        f'<div class="goal t-{t.get("tone", "accent")}"><h3>{e(t["title"])} {pill(t.get("count"), t.get("tone", "accent"))}</h3>'
        f'<p class="dt">{e(t["detail"])}</p><p class="dt"><b>{"→ " + e(t["goal"]) if t.get("goal") else "Not in this sprint"}</b></p></div>'
        for t in F.get("themes", [])) + "</div>"

    proposed_url = m.get("proposed_url") or (
        f'{browse.rsplit("/browse/", 1)[0]}/issues/?jql=project%20%3D%20{m["project"]}%20AND%20labels%20%3D%20{label}'
        if browse and m.get("project") else "")
    review_links = " · ".join(x for x in [link(board, f'Open {m["sprint"]} on the board ↗') if board else "",
                                          link(proposed_url, f'The {label} issues ↗') if proposed_url else ""] if x)
    goal_text = m.get("goal") or "; ".join(g["title"] for g in nx["goals"])
    nav = (link(board, "Board ↗") if board else "") + "".join(
        f'<a href="#{a}">{t}</a>' for a, t in [("context", "Apps"), ("feedback", "Customers"), ("last", "Last sprint"),
                                                ("next", "Next sprint"), ("decide", "Decisions"), ("jira", "Jira changes")])
    body = f"""<nav>{nav}</nav>
<div class="kicker">{e(m["team"])} · {e(m["sprint"])} · {e(m["start"])} → {e(m["end"])}</div>
<h1>{e(m["title"])}</h1>
<p class="sub">One place for the sprint: each goal with its issues and owners. New and moved issues go into the tracker labelled <b>{e(label)}</b>; the team changes owners or drops issues there, then removes the label.</p>
{f'<div class="banner">{e(m["banner"])}</div>' if m.get("banner") else ""}

<div class="kicker" id="context">Context</div><h2>Where things are</h2>
<p class="sub">{e(C.get("sub", ""))}</p>
<div class="key-legend">{pill("+12%", "bad")} got worse &nbsp; {pill("−8%", "good")} got better &nbsp; {pill("±0%", "accent")} no change, or a number that is neither good nor bad. &nbsp; The % is the change from the earlier value; "~" means that earlier value is an estimate.</div>
{ctx_html}

<div class="kicker" id="feedback">Context</div><h2>What customers told us this week</h2>
<p class="sub">{e(F.get("sub", ""))}</p>
{fb_html}

<div class="kicker" id="last">1 · Last sprint</div><h2>{e(L["title"])}</h2>
<p class="sub">{e(L.get("sub", ""))}</p>
{last_html}

<div class="kicker" id="next">2 · Next sprint</div><h2>Goals and issues</h2>
{f'<p class="sub">{review_links}</p>' if review_links else ""}
<p class="sub">{e(nx.get("sub", ""))}</p>
<div class="stats"><div class="stat"><b>{in_sprint}</b><span>issues already in {e(m["sprint"])}</span></div><div class="stat"><b>{n_add}</b><span>existing issues to add</span></div>
<div class="stat"><b>{n_create}</b><span>new issues to create</span></div><div class="stat"><b>{len(nx["goals"])}</b><span>goals</span></div></div>
<div class="load">Open issues per person, now → with this plan: {load_html}</div>
{goals_html}
{f'<p class="note">{e(nx["notNow"])}</p>' if nx.get("notNow") else ""}

<div class="kicker" id="decide">3 · Decisions</div><h2>Open questions</h2>
<ol>{"".join(f"<li>{e(x)}</li>" for x in d["decisions"])}</ol>

<div class="kicker" id="jira">4 · On approval</div><h2>What changes in the tracker</h2>
<ol>
<li>Write the goal into {e(m["sprint"])}: <i>{e(goal_text)}</i>.</li>
<li>Create {n_create} issues (red above), assigned as shown.</li>
<li>Add those plus {n_add} existing issues (orange) to {e(m["sprint"])}, set the owners shown ({n_assign} change owner), and label them <code>{e(label)}</code>. Issues already in the sprint are not touched.</li>
<li>Post a review request to the team channel with this page's link.</li>
</ol>
<p class="note">Sources: {e(m.get("sources", ""))} Generated {e(m.get("generated", ""))}.</p>"""
    template = (Path(__file__).parent / "template.html").read_text()
    if "<!--BODY-->" not in template:
        sys.exit("build.py: template has no <!--BODY--> slot")
    return template.replace("<!--TITLE-->", e(f'{m["title"]} — {m["team"]}')).replace("<!--BODY-->", body)


def main(data_path: str, out_path: str) -> None:
    data = json.loads(Path(data_path).read_text())
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        sys.exit(f"build.py: data is missing {', '.join(missing)}")
    out = render(data)
    Path(out_path).write_text(out)
    print(f"wrote {out_path} ({len(out):,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
