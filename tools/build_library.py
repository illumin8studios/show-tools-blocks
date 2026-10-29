#!/usr/bin/env python3
"""Build and sign the published library from the merged submissions.

    LIBRARY_SIGNING_KEY=<hex> python3 tools/build_library.py --out site --version 42

Writes site/library.json (every merged block; status "verified" if the reviewer set it, otherwise "shared") and
site/manifest.json (version, SHA-256, Ed25519 signature) - the app downloads the manifest at start-up and the library
only when it's newer, and uses it only if the signature checks out against the public key built into the app.
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import block_share  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="site")
ap.add_argument("--version", type=int, required=True)
a = ap.parse_args()
secret = os.environ.get("LIBRARY_SIGNING_KEY", "").strip()
if not secret:
    sys.exit("LIBRARY_SIGNING_KEY isn't set")
blocks = {}
for f in sorted(glob.glob("submissions/*.json")):
    p = json.load(open(f))
    if p.get("kind") != "block" or p.get("withdrawn"):
        continue
    errs, _ = block_share.validate(p["block"])
    if errs:
        print(f"skipping {f}: {errs[0]}")
        continue
    b = dict(p["block"], status="verified" if p.get("status") == "verified" else "shared",
             contributed_by=p.get("submitter") or "", reviewed=p.get("reviewed", ""))
    blocks[block_share.block_id(b)] = b                     # the latest file for a maker / model wins
data = block_share.library_bytes(list(blocks.values()), a.version)
man = block_share.manifest_for(data, a.version, secret)
os.makedirs(a.out, exist_ok=True)
open(os.path.join(a.out, "library.json"), "wb").write(data)
json.dump(man, open(os.path.join(a.out, "manifest.json"), "w"), indent=1)
open(os.path.join(a.out, "index.html"), "w").write(
    f"<!doctype html><meta charset=utf-8><title>Illumin8 Show Tools shared blocks</title><body style='font-family:sans-serif'>"
    f"<h1>Illumin8 Show Tools shared blocks</h1><p>Version {a.version}, {man['count']} blocks, built {man['built']}.</p>"
    f"<p><a href=library.json>library.json</a> · <a href=manifest.json>manifest.json</a></p>")
print(f"library v{a.version}: {man['count']} blocks")
