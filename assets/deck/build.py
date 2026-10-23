#!/usr/bin/env python3
"""Fill the weekly deck template with one week's JSON.

Usage: python3 build.py <data.json> <out.html>

The template's design never changes per week; only the JSON does. Validates the top-level keys so a
half-written data file fails loudly instead of rendering an empty slide.
"""
import json
import sys
from pathlib import Path

REQUIRED = ["meta", "pipeline", "sprint", "experts", "intake", "carryover", "next", "roadmap", "decisions"]
# meta also reads (optional): agent, meeting, team, project, tracker_url, roster, sprintSetup


def main(data_path: str, out_path: str) -> None:
    data = json.loads(Path(data_path).read_text())
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        sys.exit(f"build.py: data is missing {', '.join(missing)}")
    template = (Path(__file__).parent / "template.html").read_text()
    # "</" inside a <script> block would end it early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    if "/*DATA*/" not in template:
        sys.exit("build.py: template has no /*DATA*/ slot")
    html = template.replace("/*DATA*/", payload).replace(
        "<title>Team weekly</title>", f"<title>{data['meta'].get('meeting', 'Team weekly')} — {data['meta']['date']}</title>"
    )
    Path(out_path).write_text(html)
    print(f"wrote {out_path} ({len(html):,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
