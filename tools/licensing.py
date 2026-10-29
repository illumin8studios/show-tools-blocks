"""
Illumin8 Show Tools - licensing

Every tool is a licensable module. A licence key is a signed list of modules for one computer:
  payload  {"v":1, "id", "name", "mods":[...], "machine": code, "iss", "exp" (optional), "upd" (optional), "dev"}
  key      "I8-" + base32(payload + Ed25519 signature), grouped for reading
Keys are signed by the Illumin8 Licence Generator with a private key that stays on the issuer's computer; Show Tools
holds only the public key (licence_public_key.py), so it can check keys but never make them.

Running from source is development mode (everything unlocked, "DEV" in the title). Builds made with the build scripts'
--release option write _build.py (RELEASE = True) and enforce licences; the development path isn't used there.

Trial: every time Show Tools opens, unlicensed tools can be used for TRIAL_MINUTES; then their locked panel appears and
they stop. Unlicensed tools never save anything (files, exports, recordings, their state when Show Tools closes) - the
app sets ACTIVE, and save paths ask check_save() / the app's save-dialog guard, which finds the tool from the caller.
"""

import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time

MODULES ={"dmx": "DMX Net", "psn": "PSN Emulator", "tc": "Timecode", "ctl": "Control", "net": "Network", "dev": "Virtual Devices",
           "aud": "Audio", "cue": "Cues & Reports", "plot": "Cue Plotter", "3d": "Stage 3D", "sys": "System Designer", "ndi": "NDI",
           "conv": "Media Converter"}
BUNDLES = {"Everything": list(MODULES), "Lighting pack": ["dmx", "psn", "3d"], "Show control pack": ["tc", "ctl", "cue", "plot"],
           "Video pack": ["ndi", "conv", "3d"], "Integration pack": ["net", "dev", "sys"]}
TRIAL_MINUTES = 10
# which tool each source file belongs to (helpers shared by several tools aren't listed: the tool calling them is found)
SOURCE_MODULES = {"dmx_net": "dmx", "psn_emulator": "psn", "timecode_tool": "tc", "control_tool": "ctl", "qsys_serial": "ctl",
                  "network_tool": "net", "switch_tool": "net", "devices_tool": "dev", "media_servers": "dev", "audio_tool": "aud",
                  "cues_tool": "cue", "plot_tool": "plot", "scene_tool": "3d", "realview": "3d", "realview_page": "3d",
                  "gl_render": "3d", "schematic_tool": "sys", "drawing_sheets": "sys", "boq": "sys", "reports": "sys",
                  "rack_plan": "sys", "network_plan": "sys", "dmx_patch": "sys", "schematic_import": "sys", "autobuild": "sys",
                  "av_library": "sys", "block_share": "sys", "community": "sys", "backlog_build": "sys", "ndi_tool": "ndi",
                  "convert_tool": "conv", "hapr": "conv"}
ACTIVE = None                                               # the app's Licences (None: nothing enforced, e.g. a tool on its own)

try:
    from _build import RELEASE, BUILD_DATE
except ImportError:
    RELEASE, BUILD_DATE = False, datetime.date.today().isoformat()
try:
    from licence_public_key import PUBLIC_KEY
except ImportError:
    PUBLIC_KEY = ""

# ============================================================ Ed25519 (RFC 8032, pure Python)
_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _h(m):
    return hashlib.sha512(m).digest()


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _xrecover(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p:
        x = x * _I % _p
    if (x * x - x2) % _p:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * pow(5, _p - 2, _p) % _p
_G = (_xrecover(_gy, 0), _gy, 1, _xrecover(_gy, 0) * _gy % _p)


def _compress(P):
    zi = pow(P[2], _p - 2, _p)
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _xrecover(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(secret):
    h = _h(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret):
    return _compress(_mul(_expand(secret)[0], _G))


def sign(secret, msg):
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = int.from_bytes(_h(prefix + msg), "little") % _q
    R = _compress(_mul(r, _G))
    k = int.from_bytes(_h(R + A + msg), "little") % _q
    return R + int.to_bytes((r + k * a) % _q, 32, "little")


def verify(public, msg, sig):
    if len(public) != 32 or len(sig) != 64:
        return False
    A, R = _decompress(public), _decompress(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if not A or not R or s >= _q:
        return False
    k = int.from_bytes(_h(sig[:32] + public + msg), "little") % _q
    return _equal(_mul(s, _G), _add(R, _mul(k, A)))


# ============================================================ machine code
def machine_id():
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if m:
                return m.group(1)
        elif sys.platform.startswith("win"):
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | 0x0100)
            return winreg.QueryValueEx(k, "MachineGuid")[0]
        for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            if os.path.exists(f):
                return open(f).read().strip()
    except Exception:
        pass
    import uuid
    return f"node-{uuid.getnode():012x}"


def machine_code(mid=None):
    h = hashlib.sha256(("illumin8-show-tools:" + (mid or machine_id())).encode()).digest()
    c = base64.b32encode(h[:10]).decode()
    return "-".join(c[i:i + 4] for i in range(0, 16, 4))


# ============================================================ keys
def make_key(secret, name, modules, machine, licence_id, expires="", updates="", dev=False, issued=None):
    payload = json.dumps({"v": 1, "id": licence_id, "name": name, "mods": sorted(set(modules)), "machine": machine.upper(),
                          "iss": issued or datetime.date.today().isoformat(), "exp": expires, "upd": updates, "dev": bool(dev)},
                         separators=(",", ":"), sort_keys=True).encode()
    raw = base64.b32encode(payload + sign(secret, payload)).decode().rstrip("=")
    return "I8-" + "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))


def read_key(text, public=None, machine=None, today=None, build_date=None):
    """-> (licence dict or None, problem or "")."""
    public = public if public is not None else (bytes.fromhex(PUBLIC_KEY) if PUBLIC_KEY else b"")
    raw = re.sub(r"[^A-Z2-7]", "", (text or "").upper().replace("I8-", "", 1))
    if not raw:
        return None, "That isn't a licence key"
    try:
        data = base64.b32decode(raw + "=" * (-len(raw) % 8))
    except Exception:
        return None, "That isn't a licence key (it may be incomplete)"
    if len(data) < 80 or not public:
        return None, "That isn't a licence key" if public else "This copy has no licence public key (release build not set up)"
    payload, sig = data[:-64], data[-64:]
    if not verify(public, payload, sig):
        return None, "The key isn't valid (it may have been changed or mistyped)"
    lic = json.loads(payload)
    today = today or datetime.date.today().isoformat()
    if lic.get("machine") and lic["machine"] != (machine or machine_code()):
        return lic, f"This key is for another computer ({lic['machine']})"
    if lic.get("exp") and today > lic["exp"]:
        return lic, f"This key expired on {lic['exp']}"
    if lic.get("upd") and (build_date or BUILD_DATE) > lic["upd"]:
        return lic, f"This key covers updates until {lic['upd']}; this version is newer"
    return lic, ""


class Licences:
    def __init__(self, path=None, public=None, release=None, machine=None, trial_minutes=TRIAL_MINUTES, clock=time.monotonic):
        self.release = RELEASE if release is None else release
        self.public = public
        self.machine = machine or machine_code()
        self.clock, self.trial_seconds = clock, trial_minutes * 60
        self.started = clock()
        self.path = path or self._path()
        self.keys = []
        try:
            with open(self.path) as fh:
                self.keys = json.load(fh)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _path():
        from netutil import app_data_dir
        return os.path.join(app_data_dir("Illumin8 Show Tools"), "licences.json")

    @property
    def dev_mode(self):
        return not self.release

    def status(self):
        """[(key, licence or None, problem)]"""
        return [(k,) + read_key(k, self.public, self.machine) for k in self.keys]

    def modules(self):
        if self.dev_mode:
            return set(MODULES)
        mods = set()
        for _, lic, problem in self.status():
            if lic and not problem:
                mods |= set(lic.get("mods", []))
        return mods

    def licensed(self, module):
        return module not in MODULES or module in self.modules()

    def trial_left(self):
        """Seconds of this session's trial left (unlicensed tools work until then)."""
        return max(0.0, self.trial_seconds - (self.clock() - self.started))

    def usable(self, module):
        """Licensed, or still in this session's trial."""
        return self.licensed(module) or self.trial_left() > 0

    def may_save(self, module):
        """Only licensed tools save - the trial never does."""
        return self.licensed(module)

    def add(self, text):
        lic, problem = read_key(text, self.public, self.machine)
        if lic and not problem:
            norm = text.strip()
            if norm not in self.keys:
                self.keys.append(norm)
                self.save()
        return lic, problem

    def remove(self, key):
        if key in self.keys:
            self.keys.remove(key)
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(self.keys, fh, indent=1)


def calling_module(skip=1):
    """The licensable tool whose code is running this call (the nearest caller in a tool's own file), or None."""
    f = sys._getframe(skip)
    while f is not None:
        m = SOURCE_MODULES.get(str(f.f_globals.get("__name__", "")).rsplit(".", 1)[-1])
        if m:
            return m
        f = f.f_back
    return None


def save_allowed(module):
    return module is None or ACTIVE is None or ACTIVE.may_save(module)


def check_save(module, parent=None, quiet=False):
    """True if this tool may save; otherwise says why (unless quiet) and returns False."""
    if save_allowed(module):
        return True
    if not quiet:
        try:
            from tkinter import messagebox
            messagebox.showinfo("Saving needs a licence", f"{MODULES.get(module, module)} isn't licensed on this computer, so it "
                                f"can't save or export anything.\n\nThis computer's machine code: {ACTIVE.machine}\n"
                                "Send it to Illumin8 Studios for a key, then enter it in Help > Licences.", parent=parent)
        except Exception:
            pass
    return False


def selftest():
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + name)
        ok = ok and bool(cond)
    sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pk = public_key(sk)
    sig = sign(sk, b"")
    check("Ed25519 matches the RFC 8032 test vector", pk.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a" and
          sig.hex() == "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
          and verify(pk, b"", sig) and not verify(pk, b"x", sig))
    test_sk = hashlib.sha256(b"illumin8 test only").digest()
    test_pk = public_key(test_sk)
    me = machine_code("TEST-MAC-1")
    other = machine_code("TEST-MAC-2")
    check(f"Machine codes: {me} (stable, differs per machine)", me == machine_code("TEST-MAC-1") and me != other and len(me) == 19)
    key = make_key(test_sk, "Illumin8 Studios", ["ndi", "conv"], me, "T-0001", updates="2099-12-31")
    lic, problem = read_key(key, test_pk, me)
    check(f"A key unlocks its modules on its machine ({len(key)} characters)", lic and not problem and lic["mods"] == ["conv", "ndi"])
    check("Rejected on another computer", "another computer" in read_key(key, test_pk, other)[1])
    tampered = key[:40] + ("A" if key[40] != "A" else "B") + key[41:]
    check("Changed or mistyped keys are rejected", read_key(tampered, test_pk, me)[1] != "")
    exp = make_key(test_sk, "Trial", list(MODULES), me, "T-0002", expires="2026-01-31")
    check("Expiry (trials, rentals)", "expired" in read_key(exp, test_pk, me, today="2026-02-01")[1] and not read_key(exp, test_pk, me, today="2026-01-15")[1])
    upd = make_key(test_sk, "Perpetual", ["dmx"], me, "T-0003", updates="2026-06-30")
    check("Updates-until date against the build date", "updates until" in read_key(upd, test_pk, me, build_date="2026-10-27")[1])
    d = tempfile.mkdtemp()
    L = Licences(os.path.join(d, "lic.json"), test_pk, release=True, machine=me)
    check("Release build: nothing unlocked without a key", L.modules() == set() and not L.licensed("ndi"))
    L.add(key)
    L.add(make_key(test_sk, "Illumin8 Studios", ["dmx"], me, "T-0004"))
    check("Several keys combine; saved for next time", L.licensed("ndi") and L.licensed("dmx") and not L.licensed("sys")
          and Licences(os.path.join(d, "lic.json"), test_pk, release=True, machine=me).modules() == {"ndi", "conv", "dmx"})
    check("Development mode (running from source): everything unlocked", Licences(os.path.join(d, "x.json"), test_pk, release=False, machine=me).modules() == set(MODULES))
    now = [1000.0]
    T = Licences(os.path.join(d, "lic.json"), test_pk, release=True, machine=me, clock=lambda: now[0])
    check(f"Trial: every unlicensed tool works for the first {TRIAL_MINUTES} minutes, but only licensed tools save",
          T.usable("sys") and not T.may_save("sys") and T.may_save("ndi") and abs(T.trial_left() - TRIAL_MINUTES * 60) < 1e-6)
    now[0] += TRIAL_MINUTES * 60 + 1
    check("After the trial: unlicensed tools lock, licensed ones carry on", not T.usable("sys") and T.usable("ndi") and T.trial_left() == 0)
    global ACTIVE
    keep = ACTIVE
    ACTIVE = T
    try:
        check("Saving: refused for an unlicensed tool, allowed for a licensed one or code outside the tools",
              not check_save("sys", quiet=True) and check_save("ndi", quiet=True) and check_save(None, quiet=True))
        g = {"__name__": "schematic_tool"}
        exec("def probe():\n    return calling_module(1)", dict(g, calling_module=calling_module), g)
        check("The tool a save comes from is found from the calling code", g["probe"]() == "sys" and calling_module(1) is None)
    finally:
        ACTIVE = keep
    print("\nAll licensing tests passed." if ok else "\nSome licensing tests FAILED.")
    return 0 if ok else 1
