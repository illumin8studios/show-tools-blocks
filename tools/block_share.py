#!/usr/bin/env python3
"""
Illumin8 Show Tools - the shared block library

Anyone can share a block they've built; once it's been checked it's in everyone's library, downloaded when the app
starts.

  submit      In the app: Share block…  ->  validate() here first (nothing obviously wrong goes out)  ->  POST to the
              submissions service (a small Cloudflare Worker, repository/worker.js). No account needed; the Worker holds
              the GitHub token and opens a pull request in the block repository with the block and a review checklist.
              Offline, or before the service is set up, the submission is kept in an outbox and sent next time (or
              saved as a file to email).
  check       Every pull request runs repository/tools/check_blocks.py (this file's validate() and review_report()):
              catalogue checks (signals, connectors, directions, names), plausibility, a comparison with any block for
              the same maker / model already in the library, and - when the submission includes the spec sheet text or
              a reachable PDF - the spec-sheet reader's count of ports by signal and direction against the block's.
              A reviewer (Illumin8) reads the report and the manufacturer's sheet and merges. Blocks are published as
              "shared" (checked by a reviewer) or "verified" (checked against the manufacturer's published spec).
  publish     On merge, repository/tools/build_library.py builds library.json, signs its SHA-256 with the library's
              Ed25519 private key (a GitHub secret) and publishes library.json + manifest.json on GitHub Pages.
  download    At start-up the app fetches manifest.json (a few hundred bytes), and only if the library is newer downloads
              library.json, checks the hash and the signature against the public key built into the app, and keeps it
              in the app data folder. A library that doesn't verify is never used. Blocks appear under their category,
              marked shared or verified; used offline from the cache.
  feedback    Report a problem with a shared block (wrong socket, missing port) from the app: it goes to the same
              service as an issue on the repository.

    python3 block_share.py --new-keys            make the library signing key pair (once; keep the private key secret)
    python3 block_share.py --selftest
"""

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.request

# Where the shared library lives. Set these when the repository and Worker are set up (see repository/README.md).
SUBMIT_URL = ""                          # e.g. https://blocks.illumin8studios.workers.dev/submit
LIBRARY_URL = ""                         # e.g. https://illumin8studios.github.io/show-tools-blocks/
LIBRARY_PUBLIC_KEY = ""                  # hex Ed25519 public key from --new-keys (public; safe to ship)
CATEGORIES = ("Audio", "Video", "Lighting", "Network", "Power", "Show control", "Comms", "Media", "Patch panels", "Generic",
              "Control", "Imported")
STATES = {"shared": "\U0001f465 Shared library: checked by a reviewer - check against the spec sheet before relying on it",
          "verified": "\u2713 Shared library: verified against the manufacturer's published specification"}


def _settings():
    """URLs and key, from this file or overridden in the app data folder (share_settings.json)."""
    st = dict(submit_url=SUBMIT_URL, library_url=LIBRARY_URL, public_key=LIBRARY_PUBLIC_KEY)
    try:
        with open(os.path.join(_data_dir(), "share_settings.json")) as fh:
            st.update({k: v for k, v in json.load(fh).items() if v})
    except (OSError, ValueError):
        pass
    return st


def _data_dir():
    try:
        from netutil import app_data_dir
        return app_data_dir("Illumin8 Show Tools")
    except ImportError:
        return os.path.expanduser("~/.illumin8")


# ================================================================ checking a block
def catalogue():
    """(signals, connectors, signal -> connectors): from the app, or catalogue.json beside this file (the repository's
    copy, made with --export-repo-tools)."""
    try:
        from schematic_tool import SIGNALS, CONNECTORS, SIGNAL_CONNECTORS
        return SIGNALS, CONNECTORS, SIGNAL_CONNECTORS
    except ImportError:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalogue.json")) as fh:
            c = json.load(fh)
        return c["signals"], c["connectors"], c["signal_connectors"]


def block_id(b):
    return re.sub(r"[^a-z0-9]+", "-", f"{b.get('maker', '')}-{b.get('model') or b.get('name', '')}".lower()).strip("-")


def validate(b, library=None):
    """-> (errors, warnings). Errors stop a submission; warnings go to the reviewer."""
    SIGNALS, CONNECTORS, SIGNAL_CONNECTORS = catalogue()
    errs, warns = [], []
    for k, label in (("name", "a name"), ("maker", "the maker"), ("model", "the model")):
        if not str(b.get(k, "")).strip():
            errs.append(f"Give it {label}.")
    cat = str(b.get("category", ""))
    if cat.split("/")[0] not in CATEGORIES:
        warns.append(f"Category \u201c{cat}\u201d isn't one of the library's ({', '.join(CATEGORIES[:9])} …)")
    if not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}", str(b.get("prefix", ""))):
        errs.append("The tag prefix should be 1-6 capital letters or digits, e.g. AMP.")
    socks = b.get("sockets") or []
    if not socks:
        errs.append("It has no sockets.")
    if len(socks) > 400:
        errs.append(f"{len(socks)} sockets is more than any device should have - check the counts.")
    seen = {}
    for k, s in enumerate(socks):
        nm = str(s.get("name", "")).strip()
        where = f"Socket {k + 1}" + (f" ({nm})" if nm else "")
        if not nm:
            errs.append(f"{where} has no name.")
        elif nm.lower() in seen:
            errs.append(f"Two sockets are called \u201c{nm}\u201d.")
        seen[nm.lower()] = True
        if s.get("dir") not in ("in", "out", "io"):
            errs.append(f"{where}: direction must be in, out or in/out.")
        sig, con = s.get("signal"), s.get("connector")
        if sig not in SIGNALS:
            errs.append(f"{where}: \u201c{sig}\u201d isn't a known signal.")
        elif con not in CONNECTORS:
            errs.append(f"{where}: \u201c{con}\u201d isn't a known connector.")
        elif con not in SIGNAL_CONNECTORS.get(sig, []):
            warns.append(f"{where}: {con} is unusual for {sig} - right?")
        if sig in ("Ethernet", "Dante / AES67", "PoE", "AVB / Milan", "Q-LAN", "NDI") and s.get("dir") != "io":
            warns.append(f"{where}: network ports are usually in/out.")
        if sig in ("Speaker",) and s.get("dir") == "in" and "amplif" in (b.get("name", "") + cat).lower():
            warns.append(f"{where}: an amplifier's speaker sockets are outputs.")
    t = (b.get("name", "") + " " + cat).lower()
    if socks and not any(s.get("signal") in ("Power", "DC power", "PoE") for s in socks) and \
            not any(w in t for w in ("patch", "panel", "adapter", "passive", "speaker", "loudspeaker", "microphone")):
        warns.append("No power inlet - most active devices have one (or are PoE powered).")
    src = str(b.get("source", "")).strip()
    if not re.match(r"^https?://\S+\.\S+", src):
        errs.append("Add a link to the manufacturer's spec sheet or product page - reviewers check the block against it.")
    elif b.get("maker") and _n(b["maker"])[:5] not in _n(src) and not src.lower().endswith(".pdf"):
        warns.append("The source link isn't on the maker's own site - a manufacturer's page or spec sheet is best.")
    same = [x for x in (library or []) if _n(x.get("maker", "")) == _n(b.get("maker", "")) and _n(x.get("model", "")) == _n(b.get("model", ""))
            and _n(b.get("model", ""))]
    for x in same[:1]:
        diff = socket_diff(x.get("sockets", []), socks)
        warns.append(f"Already in the library ({x.get('name')}, {x.get('status', '')})"
                     + (": differs - " + "; ".join(diff[:6]) if diff else ": the sockets are the same"))
    return errs, warns


def _n(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def socket_diff(a, b):
    """Socket counts by signal and direction that differ: ['HDMI in: 2 here, 3 submitted', ...]."""
    def count(ss):
        out = {}
        for s in ss:
            out[(s.get("signal"), s.get("dir"))] = out.get((s.get("signal"), s.get("dir")), 0) + 1
        return out
    ca, cb = count(a), count(b)
    return [f"{k[0]} {k[1]}: {ca.get(k, 0)} here, {cb.get(k, 0)} submitted" for k in sorted(set(ca) | set(cb), key=str)
            if ca.get(k, 0) != cb.get(k, 0)]


def spec_check(b, spec_text):
    """Compare the block with the spec sheet text: -> (score 0-100, [differences])."""
    import specs
    groups = specs.extract(spec_text or "")
    if not groups:
        return None, ["The spec sheet reader found no connections in the text"]
    diffs = specs.compare(b.get("sockets", []), groups)
    total = sum(g["count"] for g in groups) or 1
    off = sum(abs(h - s) for _, _, h, s in diffs)
    score = max(0, round(100 * (1 - off / max(total, len(b.get("sockets", [])) or 1))))
    return score, [f"{sig} {d}: block {h}, spec sheet {s}" for sig, d, h, s in diffs]


def review_report(b, library=None, spec_text=None):
    """The reviewer's report for a submission (Markdown) -> (text, passed automated checks)."""
    errs, warns = validate(b, library)
    lines = [f"## {b.get('maker', '?')} {b.get('model', '?')} - {b.get('name', '')}", "",
             f"Category: {b.get('category', '')}  ·  {len(b.get('sockets', []))} sockets  ·  source: {b.get('source', '-')}", ""]
    lines += ["### Automated checks", ""]
    lines += [f"- ❌ {e}" for e in errs] + [f"- ⚠️ {w}" for w in warns] or ["- ✅ Nothing found"]
    if spec_text:
        score, diffs = spec_check(b, spec_text)
        lines += ["", f"### Against the spec sheet: {score if score is not None else '-'} / 100", ""]
        lines += [f"- {d}" for d in diffs] or ["- ✅ Every port on the sheet is on the block"]
    lines += ["", "### Sockets", "", "| # | Name | Dir | Signal | Connector |", "|---|---|---|---|---|"]
    lines += [f"| {k + 1} | {s.get('name')} | {s.get('dir')} | {s.get('signal')} | {s.get('connector')} |" for k, s in enumerate(b.get("sockets", []))]
    lines += ["", "### Reviewer checklist", "", "- [ ] Every socket on the manufacturer's sheet is here, with the right count",
              "- [ ] Directions, signals and connectors match the sheet", "- [ ] Power inlet(s) included",
              "- [ ] Maker, model and category are right", "- [ ] Publish as **shared** (reviewed) or **verified** (checked line by line "
              "against the manufacturer's published spec)"]
    return "\n".join(lines), not errs


def submission(b, submitter="", notes="", spec_text=""):
    """The JSON sent to the submissions service."""
    keep = {k: b[k] for k in ("name", "maker", "model", "category", "prefix", "sockets", "source", "patch") if k in b}
    keep["sockets"] = [{k: s[k] for k in ("name", "dir", "signal", "connector", "side") if k in s} for s in b.get("sockets", [])]
    try:
        from show_tools import APP_VERSION
    except Exception:
        APP_VERSION = "?"
    return dict(kind="block", id=block_id(b), block=keep, submitter=submitter[:80], notes=notes[:2000],
                spec_text=(spec_text or "")[:60000], app_version=APP_VERSION, sent=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


# ================================================================ sending
def _outbox():
    p = os.path.join(_data_dir(), "share_outbox")
    os.makedirs(p, exist_ok=True)
    return p


def submit(b, submitter="", notes="", spec_text="", library=None, timeout=15):
    """Send a block -> (ok, message). Kept in the outbox if it can't be sent now."""
    errs, _ = validate(b, library)
    if errs:
        return False, "Fix these first:\n" + "\n".join(errs)
    payload = submission(b, submitter, notes, spec_text)
    url = _settings()["submit_url"]
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                                                                                          "User-Agent": "Illumin8-Show-Tools"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode() or "{}")
            return True, f"Shared - thank you. It's waiting for review{(': ' + res['url']) if res.get('url') else ''}."
        except Exception as ex:                              # offline / service down: keep it and try again later
            reason = str(ex)
    else:
        reason = "the shared library service isn't set up in this build yet"
    path = os.path.join(_outbox(), f"{payload['id']}-{int(time.time())}.json")
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    return True, f"Saved to send later ({reason}). It goes automatically next time, or email the file:\n{path}"


def send_outbox(timeout=10):
    """Try to send everything waiting -> number sent."""
    url = _settings()["submit_url"]
    if not url:
        return 0
    sent = 0
    for f in sorted(os.listdir(_outbox())):
        p = os.path.join(_outbox(), f)
        try:
            data = open(p, "rb").read()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Illumin8-Show-Tools"})
            urllib.request.urlopen(req, timeout=timeout).read()
            os.remove(p)
            sent += 1
        except Exception:
            break
    return sent


def report_problem(b, text, submitter=""):
    """'This shared block is wrong' -> (ok, message)."""
    url = _settings()["submit_url"]
    payload = dict(kind="problem", id=block_id(b), maker=b.get("maker"), model=b.get("model"), text=text[:4000], submitter=submitter[:80])
    if not url:
        return False, "The shared library service isn't set up in this build yet."
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                                                                                      "User-Agent": "Illumin8-Show-Tools"})
        urllib.request.urlopen(req, timeout=15).read()
        return True, "Reported - thank you."
    except Exception as ex:
        return False, f"Couldn't send it: {ex}"


# ================================================================ the published library
def library_bytes(blocks, version):
    blocks = sorted(blocks, key=lambda b: (b.get("category", ""), b.get("maker", ""), b.get("model", "")))
    return json.dumps(dict(format="Illumin8 shared blocks", version=version, blocks=blocks), indent=0, sort_keys=True).encode()


def manifest_for(data, version, secret_hex):
    import licensing
    digest = hashlib.sha256(data).digest()
    sig = licensing.sign(bytes.fromhex(secret_hex), digest)
    return dict(version=version, sha256=digest.hex(), signature=sig.hex(), size=len(data), built=time.strftime("%Y-%m-%d %H:%M"),
                count=len(json.loads(data)["blocks"]))


def verify(data, manifest, public_hex):
    import licensing
    digest = hashlib.sha256(data).digest()
    if digest.hex() != manifest.get("sha256"):
        return False
    try:
        return bool(licensing.verify(bytes.fromhex(public_hex), digest, bytes.fromhex(manifest.get("signature", ""))))
    except Exception:
        return False


def cache_paths():
    d = _data_dir()
    return os.path.join(d, "shared_library.json"), os.path.join(d, "shared_manifest.json")


def load_cache():
    """The last good shared library -> (blocks, manifest) ([] if none)."""
    lp, mp = cache_paths()
    try:
        data = open(lp, "rb").read()
        man = json.load(open(mp))
        pub = _settings()["public_key"]
        if pub and not verify(data, man, pub):
            return [], {}
        return _mark(json.loads(data)["blocks"]), man
    except (OSError, ValueError, KeyError):
        return [], {}


def _mark(blocks):
    for b in blocks:
        b["status"] = "verified" if b.get("status") == "verified" else "shared"
        b["shared"], b["builtin"] = True, True                 # shown in the category tree, like the built-in blocks
    return blocks


def update(timeout=10):
    """Check for a newer shared library and download it -> (changed, message). Safe to run in a background thread."""
    st = _settings()
    if not st["library_url"] or not st["public_key"]:
        return False, "Shared library not set up in this build"
    base = st["library_url"].rstrip("/") + "/"
    try:
        man = json.loads(urllib.request.urlopen(urllib.request.Request(base + "manifest.json", headers={"User-Agent": "Illumin8-Show-Tools"}),
                                                timeout=timeout).read().decode())
    except Exception as ex:
        return False, f"Shared library: offline ({ex.__class__.__name__}), using the saved copy"
    lp, mp = cache_paths()
    try:
        have = json.load(open(mp)).get("version", 0)
    except (OSError, ValueError):
        have = 0
    if man.get("version", 0) <= have and os.path.exists(lp):
        return False, f"Shared library v{have} is up to date"
    try:
        data = urllib.request.urlopen(urllib.request.Request(base + "library.json", headers={"User-Agent": "Illumin8-Show-Tools"}),
                                      timeout=max(timeout, 30)).read()
    except Exception as ex:
        return False, f"Shared library: download failed ({ex.__class__.__name__})"
    if not verify(data, man, st["public_key"]):
        return False, "Shared library download didn't verify - not used"
    with open(lp, "wb") as fh:
        fh.write(data)
    with open(mp, "w") as fh:
        json.dump(man, fh)
    return True, f"Shared library updated to v{man['version']} ({man.get('count', '?')} blocks)"


def update_in_background(done=None):
    """Start-up: send the outbox, then fetch a newer library, without holding anything up. done(changed, message) is
    called from the worker thread - hand it to the UI thread (e.g. a flag the UI polls), never touch Tk from it."""
    def run():
        try:
            send_outbox()
            res = update()
        except Exception as ex:
            res = (False, f"Shared library: {ex}")
        if done:
            done(*res)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def export_repo_tools(folder):
    """Copy what the repository's checks need (this file, the spec sheet reader, the signing code and the signal /
    connector catalogue) into folder - run again whenever signals or connectors change in the app."""
    import shutil
    from schematic_tool import SIGNALS, CONNECTORS, SIGNAL_CONNECTORS
    os.makedirs(folder, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    for f in ("block_share.py", "specs.py", "licensing.py"):
        shutil.copy(os.path.join(here, f), os.path.join(folder, f))
    with open(os.path.join(folder, "catalogue.json"), "w") as fh:
        json.dump(dict(signals={k: list(v) for k, v in SIGNALS.items()}, connectors=list(CONNECTORS) if not isinstance(CONNECTORS, dict)
                       else {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in CONNECTORS.items()},
                       signal_connectors={k: list(v) for k, v in SIGNAL_CONNECTORS.items()}), fh, indent=0)
    return folder


def new_keys():
    import licensing
    secret = os.urandom(32)
    return secret.hex(), licensing.public_key(secret).hex()


# ================================================================ self test
def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and bool(cond)
    from schematic_tool import builtin_library
    lib = builtin_library()
    good = dict(name="Dante amplifier", maker="Acme Audio", model="DA-4", category="Audio/Amplifiers", prefix="AMP",
                source="https://www.acmeaudio.com/da-4/spec.pdf",
                sockets=[dict(name="Dante primary", dir="io", signal="Dante / AES67", connector="etherCON"),
                         dict(name="Dante secondary", dir="io", signal="Dante / AES67", connector="etherCON"),
                         dict(name="Out 1", dir="out", signal="Speaker", connector="speakON NL4"),
                         dict(name="Mains", dir="in", signal="Power", connector="powerCON TRUE1")])
    e, w = validate(good, lib)
    check(f"A properly built block passes ({w[:2]})", not e)
    bad = dict(good, prefix="amp!", source="", sockets=[dict(name="X", dir="sideways", signal="Smoke", connector="RJ45"),
                                                        dict(name="X", dir="in", signal="HDMI", connector="BNC")])
    e2, w2 = validate(bad, lib)
    check(f"Mistakes are caught ({len(e2)} errors: prefix, direction, signal, duplicate name, source)",
          len(e2) >= 5 and any("unusual" in x for x in w2))
    ex = next(b for b in lib if b.get("model") == "M4250-26G4F-PoE+")
    dup = dict(ex, source="https://www.netgear.com/x", sockets=ex["sockets"][:-2])
    _, w3 = validate(dup, lib)
    check("A different version of a block already in the library is flagged with the differences", any("Already in the library" in x and "differs" in x for x in w3))
    score, diffs = spec_check(good, "Network: 2 x Dante etherCON. Outputs: 4 x speakON NL4. Power: powerCON TRUE1 input")
    check(f"Checked against spec sheet text: {score}/100, {diffs[:2]}", score is not None and score < 100 and any("Speaker" in d for d in diffs))
    rep, passed = review_report(good, lib, "Outputs: 1 x speakON")
    check("Reviewer report with automated checks, socket table and checklist", passed and "Reviewer checklist" in rep and "| 1 |" in rep)
    sec, pub = new_keys()
    data = library_bytes([good], 3)
    man = manifest_for(data, 3, sec)
    check("Published library signed and verified; a tampered copy is refused",
          verify(data, man, pub) and not verify(data.replace(b"DA-4", b"DA-5"), man, pub)
          and not verify(data, dict(man, signature="00" * 64), pub))
    global _data_dir
    keep, tmp = _data_dir, tempfile.mkdtemp()
    _data_dir = lambda: tmp                                   # noqa: E731
    try:
        ok_, msg = submit(good, "Test", library=lib)
        check(f"Not set up / offline: kept in the outbox to send later ({msg.splitlines()[0][:60]}...)",
              ok_ and len(os.listdir(os.path.join(tmp, "share_outbox"))) == 1)
        lp, mp = cache_paths()
        open(lp, "wb").write(data)
        json.dump(man, open(mp, "w"))
        with open(os.path.join(tmp, "share_settings.json"), "w") as fh:
            json.dump(dict(public_key=pub), fh)
        blocks, m2 = load_cache()
        check("The saved shared library loads (verified against the key) and its blocks are marked shared",
              len(blocks) == 1 and blocks[0]["status"] == "shared" and m2["version"] == 3)
        open(lp, "wb").write(data + b" ")
        check("A changed saved copy is ignored", load_cache()[0] == [])
    finally:
        _data_dir = keep
    here = os.path.dirname(os.path.abspath(__file__))
    rt = os.path.join(here, "repository", "tools")
    if os.path.isdir(rt):
        stale = [f for f in ("block_share.py", "specs.py", "licensing.py") if not os.path.exists(os.path.join(rt, f))
                 or open(os.path.join(rt, f), "rb").read() != open(os.path.join(here, f), "rb").read()]
        from schematic_tool import SIGNALS
        try:
            cat = json.load(open(os.path.join(rt, "catalogue.json")))
            stale += [] if set(cat["signals"]) == set(SIGNALS) else ["catalogue.json"]
        except (OSError, ValueError, KeyError):
            stale.append("catalogue.json")
        check("The repository's checks use the same code and catalogue as the app"
              + (f" - out of date: {stale}; run python3 block_share.py --export-repo-tools repository/tools" if stale else ""), not stale)
    print("\nAll shared library tests passed." if ok else "\nSome shared library tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--export-repo-tools" in sys.argv:
        print(export_repo_tools(sys.argv[sys.argv.index("--export-repo-tools") + 1]))
    elif "--new-keys" in sys.argv:
        s, p = new_keys()
        print("PRIVATE key (GitHub secret LIBRARY_SIGNING_KEY - never commit or share):\n" + s)
        print("\nPUBLIC key (put in block_share.py LIBRARY_PUBLIC_KEY):\n" + p)
    else:
        raise SystemExit(selftest())
