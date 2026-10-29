"""
Illumin8 Show Tools - read connections from specification sheets (free, no AI, no account)

  extract(text)        rule-based reader: finds phrases like "3 x HDMI 2.0 inputs", "Outputs: 20x RJ45",
                       "12G-SDI (loop): 1" and turns them into socket groups (direction, signal, connector, count),
                       each with the words it came from
  read_document(path)  text from a PDF (pypdf, or pdftotext), HTML or plain text file
  web_lookup(product)  searches the web (DuckDuckGo HTML, no key), opens the best pages - the maker's own site and
                       spec-sheet PDFs first - and reads them. Results always go to the review screen first.
"""

import html as htmlmod
import os
import re
import urllib.parse
import urllib.request

# keyword -> (signal, connector); longer / more specific keys first
KEYS = [
    (r"12g[- ]?sdi", "12G-SDI", "BNC"), (r"6g[- ]?sdi|3g[- ]?sdi|hd[- ]?sdi|sdi", "SDI", "BNC"),
    (r"hdbaset|hdbt|tps", "HDBaseT", "RJ45"), (r"hdmi", "HDMI", "HDMI"), (r"mini ?displayport|mini ?dp", "DisplayPort", "Mini DisplayPort"),
    (r"displayport|\bdp\b", "DisplayPort", "DisplayPort"), (r"dvi", "DVI", "DVI-D"), (r"vga|d-?sub ?15", "VGA", "DE-15 (VGA)"),
    (r"sfp28", "Fibre", "SFP28"), (r"sfp\+|10g ?sfp", "Fibre", "SFP+"), (r"\bsfp\b", "Fibre", "SFP"),
    (r"opticalcon", "Fibre", "opticalCON DUO"), (r"\b(opt|optical)\b(?! audio)(?!.*toslink)", "Fibre", "SFP+"),
    (r"poe\+?\+?", "PoE", "RJ45"), (r"dante", "Dante / AES67", "RJ45"), (r"ethercon", "Ethernet", "etherCON"),
    (r"rj-?45|ethernet|\blan\b|gigabit|10/100", "Ethernet", "RJ45"), (r"aes/?ebu|aes3", "AES3", "XLR3"),
    (r"speakon|nl4|speaker out", "Speaker", "speakON NL4"), (r"xlr ?5|dmx", "DMX", "XLR5"),
    (r"xlr", "Analog audio", "XLR3"), (r"toslink|s/?pdif|optical audio", "S/PDIF", "Toslink"),
    (r"rs-?232|serial", "RS-232", "DB9"), (r"rs-?485", "RS-485", "Phoenix"), (r"rs-?422", "RS-422", "DB9"),
    (r"gpio|gpi/o|\bgpi\b|contact closure", "GPIO", "Phoenix"), (r"\bir\b|infra-?red", "IR", "TRS 3.5 mm"),
    (r"usb[- ]?c|type[- ]?c", "USB", "USB-C"), (r"usb[- ]?b", "USB", "USB-B"), (r"usb[- ]?a|usb", "USB", "USB-A"),
    (r"genlock|tri-?level|black ?burst|reference", "Genlock / tri-level sync", "BNC"), (r"word ?clock", "Word clock", "BNC"),
    (r"ltc|timecode", "Timecode", "XLR3"), (r"midi", "MIDI", "DIN5"), (r"rj-?11|pots|telephone", "Telephone (POTS)", "RJ11"),
    (r"3\.5 ?mm|mini-?jack|1/8", "Analog audio", "TRS 3.5 mm"), (r"6\.35|1/4\"?|jack", "Analog audio", "TRS 6.35 mm"),
    (r"euro ?block|phoenix|terminal block", "Analog audio", "Phoenix"), (r"\brca\b|phono", "Analog audio", "RCA"),
    (r"powercon true1", "Power", "powerCON TRUE1"), (r"powercon", "Power", "powerCON"), (r"iec|c14", "Power", "IEC C14"),
    (r"\bc13\b", "Power", "IEC C13"), (r"dc in|dc power|12 ?v ?dc|24 ?v ?dc|barrel", "DC power", "DC barrel 2.1 mm"),
]
CONNECTOR_HINTS = [(r"phoenix|euro ?block|terminal", "Phoenix"), (r"\bdb-?9\b|d-?sub ?9|9-?pin", "DB9"), (r"3\.5 ?mm", "TRS 3.5 mm"),
                   (r"ethercon", "etherCON"), (r"\bbnc\b", "BNC"), (r"xlr ?3|3-?pin xlr", "XLR3"), (r"rj-?45", "RJ45")]
IN_WORDS = r"\b(in|inputs?|ingang|entr[ée]e?s?|receive|rx|sink|capture)\b"
OUT_WORDS = r"\b(out|outputs?|loop|thru|through|sends?|tx|led out|ausgang|sorties?)\b"
IO_SIGNALS = {"Ethernet", "PoE", "Dante / AES67", "Fibre", "RS-232", "RS-485", "RS-422", "GPIO", "USB", "MIDI", "IR", "Telephone (POTS)"}
NUM = r"(\d{1,3})(?![\d.,]|\s*-?\s*pins?\b|-)"        # not part of 3.5 mm, 12-pin, 2.0 ...
TIMES = r"\s*(?:x|×|\*|pcs?\.?|nos?\.?)?\s*"


def _sig(phrase):
    p = phrase.lower()
    for pat, sig, con in KEYS:
        if re.search(pat, p):
            for hp, hc in CONNECTOR_HINTS:
                if re.search(hp, p) and hc != con and not (sig == "HDMI" or sig.startswith("SDI") or sig == "12G-SDI"):
                    con = hc
                    break
            return sig, con
    return None


def _direction(phrase, section, sig):
    p = phrase.lower()
    if re.search(r"with loop|with loop-?through|\+ ?loop|in(put)? (and|&|\+) loop", p):
        return "in+loop", False
    loop = re.search(r"\b(loop|thru|through)\b", p)
    if loop or re.search(OUT_WORDS, p):
        return "out", bool(loop)
    if re.search(IN_WORDS, p):
        return "in", False
    if section:
        return section, False                            # under an Inputs / Outputs heading
    return "io", False


def extract(text):
    """-> [dict(dir, signal, connector, count, evidence, loop)] (largest count per kind)."""
    groups = {}
    section = None
    for raw in re.split(r"[\n\r]+|(?<=[.;])\s+|\s{3,}|\u2022|\u25cf", text):
        line = raw.strip(" -*\t")
        if not line:
            continue
        low = line.lower()
        h = re.match(r"^(video |audio |other )?(inputs?|outputs?|connections|control|network|i/o|power)\s*[:\-]?\s*$", low)
        if h:
            section = "in" if "input" in low else "out" if "output" in low else None
            continue
        if re.search(r"\d{3,4}\s*[x×]\s*\d{3,4}|4:4:4|4:2:[02]|@\s*\d+\s*hz|\bbit\b.*\brgb\b", low):
            continue                                      # resolutions and colour formats, not ports
        line_section = section
        if re.match(r"^(inputs?)\s*[:\-]", low):
            section = line_section = "in"
        elif re.match(r"^(outputs?)\s*[:\-]", low):
            section = line_section = "out"
        elif re.match(r"^(control|network|other|power|management|serial|gpio|usb)\b", low):
            line_section = None                           # "Control: (2) Ethernet ports" - bidirectional
        found = []
        for m in re.finditer(NUM + TIMES + r"([A-Za-z][A-Za-z0-9 ./+\-()]{1,40})", line):           # "3 x HDMI 2.0 inputs"
            found.append((int(m.group(1)), m.group(2), m.group(0)))
        for m in re.finditer(r"([A-Za-z][A-Za-z0-9 ./+\-()]{1,40}?)\s*[:=]\s*" + NUM + r"\b", line):    # "HDMI 2.0 (loop): 3"
            found.append((int(m.group(2)), m.group(1), m.group(0)))
        for m in re.finditer(r"\(" + NUM + r"\)\s*([A-Za-z][A-Za-z0-9 ./+\-()]{1,40})", line):          # "(3) HDMI 2.0 In"
            found.append((int(m.group(1)), m.group(2), m.group(0)))
        for n, phrase, ev in found:
            if not 1 <= n <= 128 or re.match(r"^\s*(hz|khz|mhz|ghz|gb|mb|w|v|a|ms|mm|cm|kg|lb|bit|fps|ohm|db|m|ft|k|p|x\d)", phrase.lower()):
                continue
            s = _sig(phrase)
            if not s:
                continue
            sig, con = s
            if re.search(r"\b(\d{3,4}\s*[x×]\s*\d{3,4}|resolution|pixels?|channels?|budget|watts?)\b", phrase.lower()):
                continue
            d, loop = _direction(phrase + " " + low, line_section, sig)
            if sig in IO_SIGNALS and not line_section and not re.search(IN_WORDS + "|" + OUT_WORDS, phrase.lower()):
                d = "io"
            for dd, lp in ((("in", False), ("out", True)) if d == "in+loop" else ((d, loop),)):
                key = (dd, sig, con, lp)
                if n > groups.get(key, {}).get("count", 0):
                    groups[key] = dict(dir=dd, signal=sig, connector=con, count=n, evidence=ev.strip(), loop=lp)
    order = {"in": 0, "out": 1, "io": 2}
    return sorted(groups.values(), key=lambda g: (order[g["dir"]], g["signal"], g["loop"]))


def to_sockets(groups):
    socks = []
    for g in groups:
        base = g["signal"] + (" loop" if g["loop"] else " in" if g["dir"] == "in" else " out" if g["dir"] == "out" else "")
        socks += [dict(dir=g["dir"], signal=g["signal"], connector=g["connector"], name=base if g["count"] == 1 else f"{base} {k}")
                  for k in range(1, g["count"] + 1)]
    return socks


def compare(existing, groups):
    """Differences between a block's sockets and what a sheet says: [(signal, dir, block count, sheet count)]."""
    have = {}
    for s in existing:
        have[(s["signal"], s["dir"])] = have.get((s["signal"], s["dir"]), 0) + 1
    sheet = {}
    for g in groups:
        sheet[(g["signal"], g["dir"])] = sheet.get((g["signal"], g["dir"]), 0) + g["count"]
    keys = sorted(set(have) | set(sheet))
    return [(k[0], k[1], have.get(k, 0), sheet.get(k, 0)) for k in keys if have.get(k, 0) != sheet.get(k, 0)]


# ============================================================ documents and the web
def html_text(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|td|th)>", "\n", h)
    return htmlmod.unescape(re.sub(r"<[^>]+>", " ", h))


def pdf_text(data_or_path):
    import io
    try:
        import pypdf
        src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(src).pages)
    except ImportError:
        import subprocess
        import tempfile
        path = data_or_path
        if isinstance(data_or_path, (bytes, bytearray)):
            fd, path = tempfile.mkstemp(suffix=".pdf")
            os.write(fd, data_or_path)
            os.close(fd)
        r = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, timeout=60)
        return r.stdout.decode("utf-8", "replace")


def read_document(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return pdf_text(path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        t = fh.read()
    return html_text(t) if ext in (".htm", ".html") or "<html" in t[:500].lower() else t


SEARCH_URL = "https://html.duckduckgo.com/html/?q={q}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    from netutil import urlopen
    with urlopen(req, timeout=timeout) as r:
        return r.read(4_000_000), r.headers.get("Content-Type", "")


def search(query):
    data, _ = _get(SEARCH_URL.format(q=urllib.parse.quote_plus(query)))
    links = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', data.decode("utf-8", "replace")):
        href = htmlmod.unescape(href)
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [href])[0])
        if href.startswith("http") and href not in links:
            links.append(href)
    return links


def web_lookup(product, max_pages=4, log=None):
    """Free lookup -> dict(groups, sources, text). Raises RuntimeError when nothing usable is found."""
    log = log or (lambda m: None)
    maker = product.split()[0].lower() if product.split() else ""
    links = []
    for q in (f"{product} specifications", f"{product} spec sheet pdf"):
        try:
            links += [l for l in search(q) if l not in links]
        except Exception as ex:
            raise RuntimeError(f"Couldn't search the web: {ex}")
    if not links:
        raise RuntimeError("The web search returned nothing")
    score = lambda u: (maker not in urllib.parse.urlparse(u).netloc.lower(), not u.lower().endswith(".pdf"),
                       "spec" not in u.lower(), links.index(u))
    merged, sources, texts = {}, [], []
    for url in sorted(links, key=score)[:max_pages]:
        try:
            log(f"Reading {url}")
            data, ctype = _get(url)
            text = pdf_text(data) if url.lower().endswith(".pdf") or "pdf" in ctype else html_text(data.decode("utf-8", "replace"))
        except Exception:
            continue
        groups = extract(text)
        if groups:
            sources.append(url)
            texts.append(f"--- {url}\n{text[:20000]}")
            for g in groups:
                k = (g["dir"], g["signal"], g["connector"], g["loop"])
                if g["count"] > merged.get(k, {}).get("count", 0):
                    merged[k] = dict(g, source=url)
    if not merged:
        raise RuntimeError("No connection details could be read from the pages found (they may need a browser). "
                           "Try a spec-sheet PDF instead.")
    return dict(groups=list(merged.values()), sources=sources, text="\n\n".join(texts))


# ============================================================ self test
def selftest():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and bool(cond)

    novastar = """MX40 Pro LED Display Controller
Inputs
3x HDMI 2.0 (with loop through)
1x DP 1.2
1x 12G-SDI (with loop through)
Outputs
20 x RJ45 Ethernet
4 x Optical Fiber 10G
1 x Optical TOSLINK S/PDIF
Other I/O
1 x BNC (Genlock, Input)
1 x BNC (Genlock, Loop Output)
Control: (2) Ethernet ports
Power: 100-240VAC, 50-60 Hz, 70W
HDMI 2.0-3 8bit RGB 4:4:4 4096×2160@60Hz"""
    g = {(x["dir"], x["signal"]): x["count"] for x in extract(novastar)}
    check(f"Reads a NovaStar-style sheet: {g}", g.get(("in", "HDMI")) == 3 and g.get(("in", "DisplayPort")) == 1
          and g.get(("in", "12G-SDI")) == 1 and g.get(("out", "Ethernet")) == 20 and g.get(("out", "Fibre")) == 4
          and g.get(("out", "S/PDIF")) == 1 and g.get(("in", "Genlock / tri-level sync")) == 1)
    bs = "Features: HDMI Outputs 1x HDMI 2.1a. HDMI In: HDMI 2.0a. USB: 1x USB-A, 1x USB-C. Serial: 3.5mm RS-232. " \
         "GPIO: 12-pin Phoenix Connector with 8 bi-directional Ports. Ethernet: 1x Gigabit with PoE+."
    gb = {(x["dir"], x["signal"], x["connector"]): x["count"] for x in extract(bs)}
    check(f"Reads a BrightSign-style sheet: {gb}", gb.get(("out", "HDMI", "HDMI")) == 1 and gb.get(("io", "USB", "USB-A")) == 1
          and gb.get(("io", "USB", "USB-C")) == 1)
    ng = "Number of Ports: 30 (24x 1G PoE+, 2x 1G, 4x SFP+). PoE Ports / Budget: 24 PoE+ (30W/port), 480W Budget."
    gn = {(x["dir"], x["signal"], x["connector"]): x["count"] for x in extract(ng)}
    check(f"Reads a NETGEAR-style line: {gn}", gn.get(("io", "PoE", "RJ45")) == 24 and gn.get(("io", "Fibre", "SFP+")) == 4)
    socks = to_sockets(extract(novastar))
    check("Turned into named sockets (HDMI in 1-3, HDMI loop ...)", any(s["name"] == "HDMI in 3" for s in socks)
          and any(s["name"].startswith("HDMI loop") for s in socks))
    diff = compare([dict(signal="HDMI", dir="in"), dict(signal="HDMI", dir="in")], extract(novastar))
    check("Compare shows differences with a library block", ("HDMI", "in", 2, 3) in diff)

    pages = {"/html/": b'<a class="result__a" href="//duckduckgo.com/l/?uddg=http%3A%2F%2F127.0.0.1%3A18995%2Fnovastar%2Fmx40-spec">MX40</a>'
                       b'<a class="result__a" href="http://127.0.0.1:18995/forum/thread">forum</a>',
             "/novastar/mx40-spec": ("<html><body><h2>Inputs</h2><ul><li>3x HDMI 2.0</li><li>1x DP 1.2</li></ul><h2>Outputs</h2>"
                                     "<ul><li>20 x RJ45 Ethernet</li></ul><script>var x=1;</script></body></html>").encode(),
             "/forum/thread": b"<p>anyone know the ports?</p>"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            body = pages.get(path, b"")
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
    srv = HTTPServer(("127.0.0.1", 18995), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    global SEARCH_URL
    old, SEARCH_URL = SEARCH_URL, "http://127.0.0.1:18995/html/?q={q}"
    try:
        res = web_lookup("novastar MX40 Pro")
        gw = {(x["dir"], x["signal"]): x["count"] for x in res["groups"]}
        check(f"Free web lookup: search, open the maker's page, read it ({gw}, sources {len(res['sources'])})",
              gw.get(("in", "HDMI")) == 3 and gw.get(("out", "Ethernet")) == 20 and res["sources"][0].endswith("/novastar/mx40-spec"))
    finally:
        SEARCH_URL = old
        srv.shutdown()
    try:
        from reportlab.pdfgen import canvas
        import tempfile
        pp = os.path.join(tempfile.mkdtemp(), "sheet.pdf")
        cv = canvas.Canvas(pp)
        y = 800
        for l in ["Product: Example Scaler", "Inputs", "2 x HDMI 2.0 inputs", "1 x 3G-SDI input", "Outputs", "1 x HDBaseT output",
                  "Control", "1 x RS-232 (Phoenix)", "1 x RJ45 LAN"]:
            cv.drawString(40, y, l)
            y -= 18
        cv.save()
        gp = {(x["dir"], x["signal"], x["connector"]): x["count"] for x in extract(read_document(pp))}
        check(f"Reads a spec-sheet PDF: {gp}", gp.get(("in", "HDMI", "HDMI")) == 2 and gp.get(("in", "SDI", "BNC")) == 1
              and gp.get(("out", "HDBaseT", "RJ45")) == 1 and gp.get(("io", "RS-232", "Phoenix")) == 1 and gp.get(("io", "Ethernet", "RJ45")) == 1)
    except ImportError:
        print("SKIP  PDF sheet test (reportlab not installed here)")
    print("\nAll spec-reading tests passed." if ok else "\nSome spec-reading tests FAILED.")
    return 0 if ok else 1
