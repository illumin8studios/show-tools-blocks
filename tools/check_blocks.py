#!/usr/bin/env python3
"""Check submitted blocks (pull requests): writes the reviewer's report to report.md, fails if a block has errors.

    python3 tools/check_blocks.py submissions/acme-da-4.json [more files ...]
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import block_share  # noqa: E402


def library(exclude=()):
    out = []
    for f in sorted(glob.glob("submissions/*.json")):
        if os.path.abspath(f) in exclude:
            continue
        try:
            p = json.load(open(f))
            out.append(dict(p["block"], status=p.get("status", "shared")))
        except (OSError, ValueError, KeyError):
            pass
    return out


def main(files):
    reports, failed = [], False
    lib = library({os.path.abspath(f) for f in files})
    for f in files:
        try:
            p = json.load(open(f))
        except (OSError, ValueError) as ex:
            reports.append(f"## {f}\n\n- ❌ Not readable JSON: {ex}")
            failed = True
            continue
        if p.get("kind") != "block" or "block" not in p:
            reports.append(f"## {f}\n\n- ❌ Not a block submission")
            failed = True
            continue
        text, passed = block_share.review_report(p["block"], lib, p.get("spec_text") or None)
        who = p.get("submitter") or "anonymous"
        notes = f"\n\n> {p['notes']}" if p.get("notes") else ""
        reports.append(f"{text}\n\n_Submitted by {who} from Show Tools {p.get('app_version', '?')} on {p.get('sent', '?')}._{notes}")
        failed = failed or not passed
    with open("report.md", "w") as fh:
        fh.write("\n\n---\n\n".join(reports) or "No block files changed.")
    print(open("report.md").read())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
