"""Savething - find game saves with Ludusavi, sync them between devices with Syncthing,
using an always-on server as the hub.

Commands:
  init    run once per device: write config, set up the registry share (savething-registry)
  share   scan saves, pick games and devices, create shares on this device and the server
  accept  accept save shares offered to this device, translating paths to this machine
          (also stops syncing shares that were unshared on another device)
  unshare stop syncing a save everywhere (files are kept)
  status  show the state of all save shares

Python standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_ID = "savething-registry"
REGISTRY_LABEL = "Savething registry"
FOLDER_PREFIX = "save-"
LABEL_PREFIX = "Savething - "
IS_WINDOWS = os.name == "nt"
GLOB_CHARS = set("*?[")
LUDUSAVI_FLATPAK = "com.github.mtkennerly.ludusavi"
PROTON_USER = "steamuser"
DEFAULT_CONFIG = {
    "server_url": "",
    "server_key": "",
    "server_root": "/srv/media/game/savething",
    "server_versioning_keep": 10,
    "exclude_device_patterns": [],
    "ludusavi": "ludusavi",
}


class SavethingError(Exception):
    pass


# ---------------------------------------------------------------- paths

def norm(p) -> str:
    """Forward slashes, no trailing slash."""
    s = str(p).replace("\\", "/")
    while len(s) > 1 and s.endswith("/") and not s.endswith(":/"):
        s = s[:-1]
    if s.endswith(":/"):
        s = s[:-1]
    return s


def _ci(s) -> bool:
    """Windows paths and paths inside a Wine/Proton drive_c are case-insensitive."""
    return IS_WINDOWS or bool(re.match(r"^[A-Za-z]:", s)) or bool(re.search(r"/drive_c(?:/|$)", s))


def pkey(p) -> str:
    """Comparison key: Windows (and Wine drive_c) paths are case-insensitive."""
    s = norm(p)
    return s.casefold() if _ci(s) else s


def is_under(child, parent) -> bool:
    c, p = norm(child), norm(parent)
    if _ci(c) or _ci(p):
        c, p = c.casefold(), p.casefold()
    return c == p or c.startswith(p + "/")


def parent_of(p) -> str:
    s = norm(p)
    return s.rsplit("/", 1)[0] if "/" in s else s


def home_dir() -> str:
    return norm(os.environ.get("SAVETHING_HOME") or Path.home())


@dataclass(frozen=True)
class Env:
    """Where a game's saves live: a home dir, whether it follows Windows rules, and for a
    Proton prefix the drive_c dir that stands in for C:."""
    home: str
    win: bool
    drive_c: str | None = None


def as_env(home) -> Env:
    """Accept an Env or a plain home dir (Windows rules if it has a drive letter)."""
    if isinstance(home, Env):
        return home
    h = norm(home)
    return Env(h, IS_WINDOWS or bool(re.match(r"^[A-Za-z]:", h)))


def prefix_env(pfx) -> Env:
    """Env for a Proton prefix (the dir that contains drive_c)."""
    c = norm(pfx) + "/drive_c"
    return Env(c + "/users/" + PROTON_USER, True, c)


def to_portable(path, home) -> str:
    """Make a path portable: paths under the home dir become <home>/..., others stay absolute.
    Inside a Proton prefix, other drive_c paths become C:/..."""
    env = as_env(home)
    path, h = norm(path), norm(env.home)
    if is_under(path, h):
        return "<home>" + path[len(h):]
    if env.drive_c and is_under(path, env.drive_c):
        return "C:" + path[len(norm(env.drive_c)):]
    return path


def from_portable(portable, home) -> str | None:
    """Inverse of to_portable. In a Proton prefix, returns None for drives other than C:."""
    env = as_env(home)
    if portable.startswith("<home>"):
        return norm(env.home) + portable[len("<home>"):]
    p = norm(portable)
    if env.drive_c and re.match(r"^[A-Za-z]:", p):
        return norm(env.drive_c) + p[2:] if p[0] in "Cc" else None
    return p


def placeholder_map(home) -> dict:
    env = as_env(home)
    h = norm(env.home)
    if env.win:
        c = norm(env.drive_c) if env.drive_c else "C:"
        return {
            "<home>": h,
            "<winAppData>": h + "/AppData/Roaming",
            "<winLocalAppData>": h + "/AppData/Local",
            "<winLocalAppDataLow>": h + "/AppData/LocalLow",
            "<winDocuments>": h + "/Documents",
            "<winPublic>": c + "/Users/Public",
            "<winProgramData>": c + "/ProgramData",
            "<winDir>": c + "/Windows",
            "<osUserName>": h.rsplit("/", 1)[-1],
        }
    return {
        "<home>": h,
        "<xdgData>": os.environ.get("XDG_DATA_HOME") or h + "/.local/share",
        "<xdgConfig>": os.environ.get("XDG_CONFIG_HOME") or h + "/.config",
        "<osUserName>": h.rsplit("/", 1)[-1],
    }


def pattern_prefix(pattern, home) -> str | None:
    """Glob-free prefix of a manifest path pattern, expanded for this machine. None for install-dir patterns."""
    if pattern.startswith(("<base>", "<root>")):
        return None
    s = pattern
    for k, v in placeholder_map(home).items():
        s = s.replace(k, v)
    s = re.sub(r"<[^>]+>", "*", s)  # treat <storeUserId>, <game>, etc. as wildcards
    parts = []
    for comp in norm(s).split("/"):
        if GLOB_CHARS & set(comp):
            break
        parts.append(comp)
    if len(parts) < 2:
        return None
    return "/".join(parts)


def broad_dirs(home) -> list[str]:
    """Directories too broad to sync as a whole."""
    env = as_env(home)
    h = norm(env.home)
    if env.win:
        rel = ["", "AppData", "AppData/Local", "AppData/LocalLow", "AppData/Roaming",
               "AppData/Local/Packages", "Documents", "Documents/My Games", "Saved Games",
               "Desktop", "Downloads", "Music", "Pictures", "Videos"]
        c = norm(env.drive_c) if env.drive_c else "C:"
        extra = [c + x for x in ("", "/Users", "/Users/Public", "/Users/Public/Documents",
                                 "/ProgramData", "/Program Files", "/Program Files (x86)", "/Windows",
                                 "/XboxGames", "/XboxGames/GameSave", "/XboxGames/GameSave/pgs")]
        if env.drive_c:
            extra.append(parent_of(c))  # the prefix dir itself (user.reg etc.)
    else:
        rel = ["", ".local", ".local/share", ".config", "Documents", ".steam"]
        extra = ["/", "/home", "/tmp", "/usr", "/var"]
    return [h + ("/" + r if r else "") for r in rel] + extra


def _anchor(f, broad) -> str | None:
    """For a file not under any manifest dir: first-level child of the deepest broad dir containing it."""
    best = None
    for b in broad:
        if is_under(f, b) and pkey(f) != pkey(b) and (best is None or len(b) > len(best)):
            best = b
    if best is None:
        return parent_of(f)
    rest = norm(f)[len(norm(best)):].lstrip("/")
    if "/" not in rest:
        return None  # file sits directly in a broad dir; cannot be synced on its own
    return norm(best) + "/" + rest.split("/", 1)[0]


def collapse(roots) -> list[str]:
    out = []
    for r in sorted({pkey(x): x for x in roots}.values(), key=lambda x: (len(x), x)):
        if not any(is_under(r, o) for o in out):
            out.append(r)
    return out


def compute_roots(files, patterns, home, skip_dirs=()):
    """Work out which directories to sync from the scanned files and manifest patterns.

    Returns (roots, skipped, problems): skipped are files under Steam/install dirs,
    problems are files that cannot be assigned to any directory.
    """
    kept, skipped = [], []
    for f in files:
        (skipped if any(is_under(f, d) for d in skip_dirs) else kept).append(norm(f))
    broad = broad_dirs(home)
    broad_keys = {pkey(b) for b in broad}
    kept_keys = {pkey(f) for f in kept}

    cands = []
    for p in patterns:
        pre = pattern_prefix(p, home)
        if not pre:
            continue
        if pkey(pre) in kept_keys or os.path.isfile(pre):
            pre = parent_of(pre)
        if pkey(pre) in broad_keys or any(is_under(b, pre) for b in broad):
            continue
        if any(is_under(f, pre) for f in kept):
            cands.append(pre)

    roots, problems = [], []
    for f in kept:
        cover = [c for c in cands if is_under(f, c)]
        root = min(cover, key=len) if cover else _anchor(f, broad)
        if root is None:
            problems.append(f)
        else:
            roots.append(root)
    return merge_siblings(collapse(roots), broad), skipped, problems


def _depth_below_broad(p, broad) -> int:
    best = max((b for b in broad if is_under(p, b)), key=len, default=None)
    if best is None:
        return 99
    rest = norm(p)[len(norm(best)):].strip("/")
    return len(rest.split("/")) if rest else 0


def merge_siblings(roots, broad) -> list[str]:
    """Merge a game's dirs when their common parent is at least two levels below a broad dir (clearly game-specific)."""
    roots = list(roots)
    changed = True
    while changed and len(roots) > 1:
        changed = False
        for i in range(len(roots)):
            for j in range(i + 1, len(roots)):
                a, b = norm(roots[i]).split("/"), norm(roots[j]).split("/")
                ci = _ci(roots[i]) or _ci(roots[j])
                n = 0
                while n < min(len(a), len(b)) and (a[n].casefold() == b[n].casefold() if ci else a[n] == b[n]):
                    n += 1
                common = "/".join(a[:n])
                if n and _depth_below_broad(common, broad) >= 2:
                    roots = collapse([r for k, r in enumerate(roots) if k not in (i, j)] + [common])
                    changed = True
                    break
            if changed:
                break
    return roots


# ---------------------------------------------------------------- saves vs settings

def pattern_regex(pattern, home):
    """Compile a manifest path pattern into a regex matching the file (or anything under it)."""
    if pattern.startswith(("<base>", "<root>")):
        return None
    s = pattern
    for k, v in placeholder_map(home).items():
        s = s.replace(k, v)
    s = norm(re.sub(r"<[^>]+>", "*", s))
    out, i = [], 0
    while i < len(s):
        if s.startswith("**", i):
            out.append(".*")
            i += 2
        elif s[i] == "*":
            out.append("[^/]*")
            i += 1
        elif s[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(s[i]))
            i += 1
    flags = re.IGNORECASE if as_env(home).win else 0
    return re.compile("".join(out) + "(?:/.*)?$", flags)


def classify_files(files, manifest_entry, home):
    """Split files into (saves, configs) using the manifest tags.

    A file counts as a setting only if its matching patterns are tagged "config" and none
    is tagged "save". Unmatched or untagged files are treated as saves (never lose progress).
    """
    compiled = []
    for pat, info in (manifest_entry.get("files") or {}).items():
        rx = pattern_regex(pat, home)
        if rx:
            compiled.append((rx, set((info or {}).get("tags") or [])))
    saves, configs = [], []
    for f in files:
        nf = norm(f)
        tags = set()
        for rx, t in compiled:
            if rx.match(nf):
                tags |= t
        (configs if "config" in tags and "save" not in tags else saves).append(nf)
    return saves, configs


def config_ignores(root, save_files, config_files) -> list[str]:
    """Paths (relative to root) of settings to keep out of sync.

    A file is widened to its shallowest parent dir inside root that holds no save files,
    so e.g. an Unreal "Config" dir becomes one entry.
    """
    root = norm(root)
    saves_in = [norm(s) for s in save_files if is_under(s, root)]
    picked = set()
    for c in config_files:
        if not is_under(c, root) or pkey(c) == pkey(root):
            continue
        parts = norm(c)[len(root):].strip("/").split("/")
        chosen = "/".join(parts)
        for k in range(1, len(parts)):
            d = root + "/" + "/".join(parts[:k])
            if not any(is_under(s, d) for s in saves_in):
                chosen = "/".join(parts[:k])
                break
        picked.add(chosen)
    out = []
    for r in sorted(picked, key=lambda x: (x.count("/"), x)):
        if not any(is_under(r, o) for o in out):
            out.append(r)
    return sorted(out)


def ignore_line(rel, windows) -> str:
    """Anchored Syncthing ignore pattern for one relative path, with glob chars escaped."""
    esc = "|" if windows else "\\"
    return "/" + "".join(esc + c if c in "*?[]{}!" else c for c in norm(rel).strip("/"))


# ---------------------------------------------------------------- Steam

def steam_roots() -> list[str]:
    return [os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")]


def steam_libraries() -> list[str]:
    roots = []
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                roots.append(winreg.QueryValueEx(k, "SteamPath")[0])
        except OSError:
            pass
    else:
        roots += steam_roots()
    libs = {}
    for r in roots:
        if not os.path.isdir(r):
            continue
        libs[pkey(r)] = norm(r)
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            text = Path(vdf).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if os.path.isdir(p):
                libs[pkey(p)] = norm(p)
    return list(libs.values())


def installed_steam_ids(libs) -> set[int]:
    ids = set()
    for lib in libs:
        for p in Path(lib, "steamapps").glob("appmanifest_*.acf"):
            m = re.match(r"appmanifest_(\d+)\.acf$", p.name)
            if m:
                ids.add(int(m.group(1)))
    return ids


def steam_ids_of(entry) -> set[int]:
    ids = set()
    sid = (entry.get("steam") or {}).get("id")
    if sid:
        ids.add(int(sid))
    for x in (entry.get("id") or {}).get("steamExtra") or []:
        ids.add(int(x))
    return ids


def parse_binary_vdf(data: bytes) -> dict:
    """Parse Steam's binary KeyValues format (shortcuts.vdf)."""
    pos = 0

    def cstr():
        nonlocal pos
        end = data.index(b"\0", pos)
        out = data[pos:end].decode("utf-8", errors="replace")
        pos = end + 1
        return out

    def obj():
        nonlocal pos
        out = {}
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == 0x08:
                return out
            key = cstr()
            if t == 0x00:
                out[key] = obj()
            elif t == 0x01:
                out[key] = cstr()
            elif t in (0x02, 0x03):  # int32, float32
                out[key] = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4
            elif t in (0x07, 0x0A):  # uint64, int64
                out[key] = int.from_bytes(data[pos:pos + 8], "little")
                pos += 8
            else:
                raise ValueError(f"unknown binary VDF type {t:#x} at {pos - 1}")
        return out

    return obj()


@dataclass(frozen=True)
class Shortcut:
    name: str
    exe: str = ""
    start_dir: str = ""

    def keys(self) -> set[str]:
        """Names this shortcut may be known by: its name, the exe's file name, and the
        folders on the exe and start-in paths (usually the game's install folder)."""
        out = {title_key(self.name)}
        for p in (self.exe, self.start_dir):
            parts = [x for x in norm(p.strip().strip('"')).split("/") if x]
            if parts and p is self.exe:
                out.add(title_key(parts[-1].rsplit(".", 1)[0]))
                parts = parts[:-1]
            out |= {title_key(x) for x in parts}
        return out - {""}


def title_key(name) -> str:
    return re.sub(r"[\W_]+", "", name.casefold())


def shortcut_entries(data: bytes) -> dict[int, Shortcut]:
    """{app ID: Shortcut} of the non-Steam shortcuts in a shortcuts.vdf. The app ID is also
    the name of the shortcut's Proton prefix dir in steamapps/compatdata."""
    out = {}
    for sc in (parse_binary_vdf(data).get("shortcuts") or {}).values():
        if not isinstance(sc, dict):
            continue
        low = {k.lower(): v for k, v in sc.items()}
        name = low.get("appname") or ""
        appid = low.get("appid")
        if not appid:  # older Steam: derived from exe + name
            appid = zlib.crc32((low.get("exe", "") + name).encode()) | 0x80000000
        if name:
            out[int(appid)] = Shortcut(name, low.get("exe", ""), low.get("startdir", ""))
    return out


def steam_shortcuts() -> dict[int, Shortcut]:
    out = {}
    for r in steam_roots():
        for p in Path(r).glob("userdata/*/config/shortcuts.vdf"):
            try:
                out.update(shortcut_entries(p.read_bytes()))
            except (OSError, ValueError, IndexError):
                continue
    return out


def proton_prefix_of(path, libs) -> tuple[int, str] | None:
    """(app ID, prefix dir) if path is inside a Proton prefix's drive_c."""
    p = norm(path)
    for lib in libs:
        base = norm(lib) + "/steamapps/compatdata/"
        if p.startswith(base):
            m = re.match(r"(\d+)/pfx/drive_c(?:/|$)", p[len(base):])
            if m:
                return int(m.group(1)), base + m.group(1) + "/pfx"
    return None


def find_prefix(appid, libs) -> str | None:
    for lib in libs:
        pfx = norm(lib) + f"/steamapps/compatdata/{appid}/pfx"
        if os.path.isdir(pfx + "/drive_c/users/" + PROTON_USER):
            return pfx
    return None


def pick_proton_files(paths, libs, shortcuts):
    """Keep a game's files from one Proton prefix (a Steam game's or a non-Steam shortcut's).

    Returns (env, files, reason): reason is set when nothing can be synced. Files outside
    drive_c (e.g. the prefix's *.reg registry files) are never synced.
    """
    groups = {}
    for f in paths:
        hit = proton_prefix_of(f, libs)
        if hit:
            groups.setdefault(hit, []).append(f)
    if not groups:
        if any(re.search(r"/compatdata/\d+/pfx/", norm(f)) for f in paths):
            return None, [], "registry-only saves, cannot sync"
        return None, [], "not in a Proton prefix (native Linux?)"

    def newest(fs):
        return max((os.path.getmtime(f) for f in fs if os.path.exists(f)), default=0)

    def name(a):
        return shortcuts[a].name if a in shortcuts else f"Steam app {a}"

    (appid, pfx), files = max(groups.items(), key=lambda kv: newest(kv[1]))
    if len(groups) > 1:
        others = ", ".join(name(a) for a, _ in groups if a != appid)
        print(f"  note: saves found in several prefixes; using the newest ({name(appid)}), not {others}")
    return prefix_env(pfx), files, None


# ---------------------------------------------------------------- Syncthing API

class Syncthing:
    def __init__(self, url, api_key, name):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self._ctx = ssl._create_unverified_context() if self.url.startswith("https") else None

    def request(self, method, path, body=None, query=None, ok404=False):
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"X-API-Key": self.api_key,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if ok404 and e.code == 404:
                return None
            detail = e.read().decode(errors="replace").strip()
            raise SavethingError(f"{self.name} Syncthing returned HTTP {e.code} ({method} {path}): {detail}")
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise SavethingError(f"Cannot reach {self.name} Syncthing ({self.url}): {e}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def my_id(self):
        return self.request("GET", "/rest/system/status")["myID"]

    def devices(self):
        return self.request("GET", "/rest/config/devices")

    def folders(self):
        return self.request("GET", "/rest/config/folders")

    def folder(self, fid):
        return self.request("GET", f"/rest/config/folders/{urllib.parse.quote(fid)}", ok404=True)

    def is_windows(self) -> bool:
        if not hasattr(self, "_is_windows"):
            self._is_windows = self.request("GET", "/rest/system/version").get("os") == "windows"
        return self._is_windows

    def put_folder(self, fid, label, path, device_ids, versioning=None, ignores=(), ignore_case=False):
        """Create/replace a folder. With ignores, it is created paused, the ignore patterns
        are written, and only then is it resumed, so ignored files are never announced.
        ignore_case makes the patterns case-insensitive (for Proton prefixes)."""
        obj = self.request("GET", "/rest/config/defaults/folder") or {}
        obj.update({"id": fid, "label": label, "path": path, "type": "sendreceive",
                    "paused": bool(ignores),
                    "devices": [{"deviceID": d, "introducedBy": "", "encryptionPassword": ""}
                                for d in dict.fromkeys(device_ids)]})
        if versioning:
            obj["versioning"] = versioning
        self.request("PUT", f"/rest/config/folders/{urllib.parse.quote(fid)}", obj)
        if ignores:
            win = self.is_windows()
            self.set_ignores(fid, [("(?i)" if ignore_case else "") + ignore_line(r, win) for r in ignores])
            self.request("PATCH", f"/rest/config/folders/{urllib.parse.quote(fid)}", {"paused": False})

    def set_ignores(self, fid, lines):
        self.request("POST", "/rest/db/ignores", {"ignore": list(lines)}, query={"folder": fid})

    def get_ignores(self, fid) -> list[str]:
        return (self.request("GET", "/rest/db/ignores", query={"folder": fid}) or {}).get("ignore") or []

    def set_folder_devices(self, fid, device_ids):
        f = self.folder(fid)
        if f is None:
            raise SavethingError(f"Folder {fid} does not exist on {self.name}")
        existing = {d["deviceID"]: d for d in f.get("devices", [])}
        for d in device_ids:
            existing.setdefault(d, {"deviceID": d, "introducedBy": "", "encryptionPassword": ""})
        f["devices"] = list(existing.values())
        self.request("PUT", f"/rest/config/folders/{urllib.parse.quote(fid)}", f)

    def delete_folder(self, fid):
        self.request("DELETE", f"/rest/config/folders/{urllib.parse.quote(fid)}", ok404=True)

    def add_device(self, device_id, name):
        obj = self.request("GET", "/rest/config/defaults/device") or {}
        obj.update({"deviceID": device_id, "name": name, "addresses": ["dynamic"]})
        self.request("PUT", f"/rest/config/devices/{device_id}", obj)

    def ignore_offer(self, device_id, fid, label):
        """Add fid to the device's ignoredFolders so future offers of it (including
        auto-accept) are dropped silently."""
        d = self.request("GET", f"/rest/config/devices/{device_id}", ok404=True)
        if d is None:
            return
        ignored = d.get("ignoredFolders") or []
        if any(x.get("id") == fid for x in ignored):
            return
        ignored.append({"id": fid, "label": label,
                        "time": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        d["ignoredFolders"] = ignored
        self.request("PUT", f"/rest/config/devices/{device_id}", d)

    def dismiss_pending(self, fid):
        try:
            self.request("DELETE", "/rest/cluster/pending/folders", query={"folder": fid})
        except SavethingError:
            pass

    def pending_folders(self):
        return self.request("GET", "/rest/cluster/pending/folders") or {}


def syncthing_config_candidates(st_home=None) -> list[Path]:
    if st_home:
        return [Path(st_home, "config.xml")]
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return [Path(base, "Syncthing", "config.xml")]
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    out = [Path(state, "syncthing", "config.xml"), Path(conf, "syncthing", "config.xml")]
    # Flatpak builds (e.g. SyncThingy on the Steam Deck) keep theirs in the app's sandbox dir
    flatpak = Path(os.path.expanduser("~/.var/app"))
    out += sorted(flatpak.glob("*/.local/state/syncthing/config.xml"))
    out += sorted(flatpak.glob("*/config/syncthing/config.xml"))
    return out


def local_syncthing(st_home=None) -> Syncthing:
    for p in syncthing_config_candidates(st_home):
        if p.is_file():
            gui = ET.parse(p).getroot().find("gui")
            addr = (gui.findtext("address") or "127.0.0.1:8384").strip()
            key = (gui.findtext("apikey") or "").strip()
            host, _, port = addr.rpartition(":")
            if host in ("", "0.0.0.0"):
                host = "127.0.0.1"
            elif host == "[::]":
                host = "[::1]"
            scheme = "https" if gui.get("tls", "false").lower() == "true" else "http"
            if not key:
                raise SavethingError(f"No API key in {p}; generate one in the Syncthing web UI (Settings > GUI)")
            return Syncthing(f"{scheme}://{host}:{port}", key, "local")
    raise SavethingError("Cannot find the local Syncthing config.xml. Is Syncthing installed?")


# ---------------------------------------------------------------- config / registry

def default_config_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming"), "savething")
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "savething")


def load_config(cfg_dir: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = cfg_dir / "config.json"
    if p.is_file():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg_dir: Path, cfg: dict):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def read_registry(reg_dir: Path) -> dict:
    out = {}
    for p in sorted(Path(reg_dir).glob(FOLDER_PREFIX + "*.json")):
        if "sync-conflict" in p.name:
            continue
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and e.get("folder_id"):
            out[e["folder_id"]] = e
    return out


def active(entry) -> bool:
    return not entry.get("removed")


def split_registry(registry, my_id, local_ids):
    """Returns (to_remove, to_accept): unshared entries still configured here, and
    active entries offered to this device that are not configured yet."""
    to_remove = [e for e in registry.values() if not active(e) and e["folder_id"] in local_ids]
    to_accept = [e for e in registry.values()
                 if active(e) and my_id in e.get("devices", []) and e["folder_id"] not in local_ids]
    return to_remove, to_accept


def write_entry(reg_dir: Path, entry: dict):
    p = Path(reg_dir, entry["folder_id"] + ".json")
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def slugify(name) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return s or "g" + hashlib.sha1(name.encode()).hexdigest()[:8]


# ---------------------------------------------------------------- interaction

def human(n) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def parse_selection(text, n) -> list[int]:
    """Parse numbers like '1,3,5-7' (1-based) into 0-based indices."""
    out = []
    for part in text.replace("\uff0c", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"Invalid input: {part}")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > n:
            raise ValueError(f"Out of range: {part} (1-{n})")
        out += [i - 1 for i in range(a, b + 1) if i - 1 not in out]
    return out


def ask(prompt) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_selection(prompt, n, empty_means_all=False, commands=()) -> list[int] | str:
    """Indexes picked by the user, or the entered word if it is one of commands."""
    while True:
        text = ask(prompt)
        if text.lower() in commands:
            return text.lower()
        if text.lower() in ("q", "quit", "0"):
            return []
        if not text:
            return list(range(n)) if empty_means_all else []
        try:
            return parse_selection(text, n)
        except ValueError as e:
            print(f"  {e}")


def ask_required(prompt) -> str:
    """Ask until something is entered; end of input (or Ctrl+D) cancels."""
    while True:
        try:
            text = input(prompt).strip()
        except EOFError:
            raise SavethingError("Cancelled; nothing was entered")
        if text:
            return text
        print("  This is required.")


def confirm(prompt, default=True) -> bool:
    text = ask(prompt + (" [Y/n] " if default else " [y/N] ")).lower()
    return default if not text else text in ("y", "yes")


# ---------------------------------------------------------------- context

@dataclass
class Ctx:
    cfg_dir: Path
    cfg: dict
    local: Syncthing
    server: Syncthing
    my_id: str
    server_id: str
    home: str
    device_names: dict = field(default_factory=dict)

    def dev_name(self, did):
        return self.device_names.get(did) or did[:7]

    def excluded_device(self, name) -> bool:
        return any(p.lower() in (name or "").lower() for p in self.cfg.get("exclude_device_patterns", []))

    def registry_dir(self) -> Path:
        f = self.local.folder(REGISTRY_ID)
        if not f:
            raise SavethingError("This device is not initialized. Run: savething init")
        return Path(f["path"])


def make_ctx(args, cfg=None) -> Ctx:
    cfg_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    cfg = cfg or load_config(cfg_dir)
    if not cfg.get("server_url") or not cfg.get("server_key"):
        raise SavethingError("Server is not configured. Run: savething init")
    local = local_syncthing(args.st_home)
    server = Syncthing(cfg["server_url"], cfg["server_key"], "server")
    my_id, server_id = local.my_id(), server.my_id()
    if my_id == server_id:
        raise SavethingError("This device is the server; run Savething on a gaming device")
    names = {d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in server.devices()}
    names.update({d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in local.devices()})
    return Ctx(cfg_dir, cfg, local, server, my_id, server_id, home_dir(), names)


# ---------------------------------------------------------------- init

def cmd_init(args):
    cfg_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    first_time = not (cfg_dir / "config.json").is_file()
    cfg = load_config(cfg_dir)
    if first_time and not (args.server_url or args.server_root):  # fully interactive first setup
        root = ask(f"Save directory on the server [{cfg['server_root']}]: ")
        if root:
            cfg["server_root"] = root
    if args.server_url:
        cfg["server_url"] = args.server_url
    if args.server_key:
        cfg["server_key"] = args.server_key
    if args.server_root:
        cfg["server_root"] = args.server_root
    if not cfg["server_url"]:
        cfg["server_url"] = ask_required("Server Syncthing URL (e.g. http://192.168.1.10:8384): ")
    if not cfg["server_key"]:
        cfg["server_key"] = ask_required("Server Syncthing API key: ")
    cfg["server_url"] = cfg["server_url"].rstrip("/")
    if not re.match(r"^https?://", cfg["server_url"], re.IGNORECASE):
        cfg["server_url"] = "http://" + cfg["server_url"]
    save_config(cfg_dir, cfg)
    print(f"Config saved: {cfg_dir / 'config.json'}")

    ctx = make_ctx(args, cfg)
    server_known = {d["deviceID"] for d in ctx.server.devices()}
    local_known = {d["deviceID"] for d in ctx.local.devices()}
    print(f"This device: {ctx.dev_name(ctx.my_id)} ({ctx.my_id[:7]})  Server: {ctx.dev_name(ctx.server_id)} ({ctx.server_id[:7]})")
    if ctx.my_id not in server_known:
        raise SavethingError("The server Syncthing does not know this device yet; add this device on the server first")
    if ctx.server_id not in local_known:
        raise SavethingError("The local Syncthing has not added the server device yet; add it first")

    server_path = cfg["server_root"].rstrip("/") + "/" + REGISTRY_ID
    f = ctx.server.folder(REGISTRY_ID)
    if f is None:
        ctx.server.put_folder(REGISTRY_ID, REGISTRY_LABEL, server_path, [ctx.server_id, ctx.my_id])
        print("Created the registry share on the server")
    elif ctx.my_id not in {d["deviceID"] for d in f["devices"]}:
        ctx.server.set_folder_devices(REGISTRY_ID, [ctx.my_id])
        print("Added this device to the registry share on the server")

    f = ctx.local.folder(REGISTRY_ID)
    if f is None:
        reg_dir = cfg_dir / "registry"
        reg_dir.mkdir(parents=True, exist_ok=True)
        ctx.local.put_folder(REGISTRY_ID, REGISTRY_LABEL, str(reg_dir), [ctx.my_id, ctx.server_id])
        print(f"Created the local registry share: {reg_dir}")
    else:
        ctx.local.set_folder_devices(REGISTRY_ID, [ctx.server_id])
        print(f"Local registry share already exists: {f['path']}")
    ctx.local.dismiss_pending(REGISTRY_ID)
    print("Init complete.")


# ---------------------------------------------------------------- share

def ludusavi_cmd(cfg) -> list[str]:
    """The command that runs Ludusavi: config "ludusavi" (a path or an argument list),
    falling back to the Flatpak build on Linux when it is not on PATH."""
    exe = cfg.get("ludusavi") or "ludusavi"
    if isinstance(exe, list):
        return exe
    if exe == "ludusavi" and not IS_WINDOWS and not shutil.which(exe) and shutil.which("flatpak"):
        return ["flatpak", "run", LUDUSAVI_FLATPAK]
    return [exe]


def ludusavi_json(ctx, *args):
    cmd = ludusavi_cmd(ctx.cfg)
    try:
        r = subprocess.run([*cmd, *args], capture_output=True)
    except FileNotFoundError:
        raise SavethingError(f"Cannot find ludusavi ({' '.join(cmd)}); install it or put its full path in config.json")
    if r.returncode != 0 and not r.stdout.strip():
        raise SavethingError(f"ludusavi {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
    return json.loads(r.stdout.decode("utf-8"))


@dataclass
class GameRow:
    name: str
    files: list
    size: int
    reason: str | None
    ignored: bool
    shared: list  # existing registry entries
    selectable: bool
    env: Env | None = None  # Proton prefix the saves are in (Linux); None = this device's home


def choose_devices(ctx, spec=None) -> list[str]:
    cands = [d for d in ctx.local.devices()
             if d["deviceID"] not in (ctx.my_id, ctx.server_id) and not ctx.excluded_device(d.get("name"))]
    if spec is not None:
        wanted = [s.strip().lower() for s in spec.split(",") if s.strip()]
        out = []
        for w in wanted:
            hit = [d["deviceID"] for d in cands
                   if (d.get("name") or "").lower() == w or d["deviceID"].lower().startswith(w)]
            if not hit:
                raise SavethingError(f"Device not found: {w}")
            out += hit
        return list(dict.fromkeys(out))
    if not cands:
        print("No other devices available; syncing to the server only.")
        return []
    print(f"\nShare with which devices? (the server {ctx.dev_name(ctx.server_id)} is always included)")
    for i, d in enumerate(cands, 1):
        print(f"  {i}. {d.get('name') or d['deviceID'][:7]}  ({d['deviceID'][:7]})")
    while True:
        text = ask("Numbers, comma-separated; Enter = all; 0 = server only: ")
        if text == "0":
            return []
        if not text:
            return [d["deviceID"] for d in cands]
        try:
            return [cands[i]["deviceID"] for i in parse_selection(text, len(cands))]
        except ValueError as e:
            print(f"  {e}")


def allocate_id(ctx, game, taken) -> str:
    base = FOLDER_PREFIX + slugify(game)
    fid, n = base, 2
    while fid.lower() in taken:
        fid, n = f"{base}-{n}", n + 1
    taken.add(fid.lower())
    return fid


def create_share(ctx, reg_dir, game, root, fid, targets, ignores=(), env=None, steam_ids=()):
    label = LABEL_PREFIX + game
    ignores = list(ignores)
    server_known = {d["deviceID"] for d in ctx.server.devices()}
    for t in targets:
        if t not in server_known:
            ctx.server.add_device(t, ctx.dev_name(t))
            print(f"  Added device {ctx.dev_name(t)} to the server")
    server_path = ctx.cfg["server_root"].rstrip("/") + "/" + fid
    versioning = None
    keep = int(ctx.cfg.get("server_versioning_keep") or 0)
    if keep > 0:
        versioning = {"type": "simple", "params": {"keep": str(keep), "cleanoutDays": "0"},
                      "cleanupIntervalS": 3600}
    try:
        ctx.server.put_folder(fid, label, server_path, [ctx.server_id, ctx.my_id, *targets], versioning, ignores)
        ctx.local.put_folder(fid, label, root, [ctx.my_id, ctx.server_id, *targets], ignores=ignores,
                             ignore_case=bool(env and env.drive_c))
    except SavethingError:
        ctx.local.delete_folder(fid)
        ctx.server.delete_folder(fid)
        raise
    entry = {
        "game": game,
        "folder_id": fid,
        "label": label,
        "portable_path": to_portable(root, env or ctx.home),
        "ignore": ignores,
        "devices": [ctx.my_id, *targets],
        "created_by": ctx.my_id,
        "created_by_name": ctx.dev_name(ctx.my_id),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if steam_ids:  # lets SteamOS find the Steam game's own Proton prefix on accept
        entry["steam_ids"] = sorted(steam_ids)
    try:
        write_entry(reg_dir, entry)
    except OSError as e:
        ctx.local.delete_folder(fid)
        ctx.server.delete_folder(fid)
        raise SavethingError(f"Failed to write the registry entry; rolled back: {e}")
    ctx.local.dismiss_pending(fid)
    print(f"  OK {fid}  {root}  ->  server:{server_path}")
    if ignores:
        print(f"     not syncing settings: {', '.join(ignores)}")
    return entry


def add_devices_to_share(ctx, reg_dir, entry, targets):
    new = [t for t in targets if t not in entry["devices"]]
    if not new:
        print(f"  {entry['folder_id']} is already shared with these devices")
        return
    server_known = {d["deviceID"] for d in ctx.server.devices()}
    for t in new:
        if t not in server_known:
            ctx.server.add_device(t, ctx.dev_name(t))
    ctx.server.set_folder_devices(entry["folder_id"], new)
    if ctx.local.folder(entry["folder_id"]):
        ctx.local.set_folder_devices(entry["folder_id"], new)
    entry["devices"] = list(dict.fromkeys(entry["devices"] + new))
    write_entry(reg_dir, entry)
    print(f"  OK {entry['folder_id']} added devices: {', '.join(ctx.dev_name(t) for t in new)}")


def nesting_conflict(root, local_folders) -> str | None:
    for f in local_folders:
        p = f.get("path") or ""
        if p and (is_under(root, p) or is_under(p, root)):
            return f"{f['id']} ({p})"
    return None


def ludusavi_config_dirs(cmd) -> list[Path]:
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    out = [Path(conf, "ludusavi")]
    if LUDUSAVI_FLATPAK in cmd:
        out.insert(0, Path(os.path.expanduser(f"~/.var/app/{LUDUSAVI_FLATPAK}/config/ludusavi")))
    return out


def prefix_scan_config(ctx, lconf, prefixes) -> Path:
    """A Ludusavi config dir = the user's config plus every prefix as an "otherWine" root, so
    Ludusavi finds saves in a shortcut's prefix whatever the shortcut is called. It lives in
    Savething's config dir (Flatpak apps can't see this process's /tmp); the manifest is
    linked from the user's Ludusavi config so it isn't downloaded again."""
    d = ctx.cfg_dir / "ludusavi-scan"
    d.mkdir(parents=True, exist_ok=True)
    conf = dict(lconf)
    have = {pkey(r.get("path") or "") for r in conf.get("roots") or []}
    conf["roots"] = list(conf.get("roots") or []) + [
        {"store": "otherWine", "path": p} for p in prefixes if pkey(p) not in have]
    (d / "config.yaml").write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")  # JSON is YAML
    link = d / "manifest.yaml"
    if not link.exists():
        for src in ludusavi_config_dirs(ludusavi_cmd(ctx.cfg)):
            if (src / "manifest.yaml").is_file():
                try:
                    link.unlink(missing_ok=True)
                    link.symlink_to(src / "manifest.yaml")
                except OSError:
                    pass
                break
    return d


def scan_games(ctx, registry, local_ids):
    print("Scanning saves with Ludusavi, this can take a minute or two...", flush=True)
    lconf = ludusavi_json(ctx, "config", "show", "--api")
    libs = steam_libraries()
    skip_dirs = libs + [norm(r["path"]) for r in lconf.get("roots", []) if r.get("path")]
    shortcuts = {} if IS_WINDOWS else steam_shortcuts()
    prefixes = [p for p in (find_prefix(a, libs) for a in shortcuts) if p]
    if prefixes:
        scan_dir = prefix_scan_config(ctx, lconf, prefixes)
        preview = ludusavi_json(ctx, "--config", str(scan_dir), "backup", "--preview", "--api")
    else:
        preview = ludusavi_json(ctx, "backup", "--preview", "--api")
    manifest = ludusavi_json(ctx, "manifest", "show", "--api")

    by_game = {}
    for e in registry.values():
        if active(e) and e["folder_id"] in local_ids:
            by_game.setdefault(e["game"], []).append(e)

    rows = []
    for name, g in sorted(preview.get("games", {}).items(), key=lambda kv: kv[0].casefold()):
        files = g.get("files") or {}
        paths = list(files)
        env, reason = None, None
        if not paths:
            reason = "registry-only saves, cannot sync" if g.get("registry") else "no save files"
        elif not IS_WINDOWS:  # only Windows games run through Proton are supported
            env, paths, reason = pick_proton_files(paths, libs, shortcuts)
        if reason is None and all_skipped(paths, row_skip_dirs(env, skip_dirs)):
            reason = "saves only in Steam/install dirs"
        rows.append(GameRow(name, paths, sum(v.get("bytes", 0) for v in files.values()), reason,
                            g.get("decision") == "Ignored", by_game.get(name, []), bool(paths), env))
    return rows, manifest, skip_dirs


def tilde(path, home) -> str:
    return to_portable(path, home).replace("<home>", "~", 1)


def row_skip_dirs(env, skip_dirs) -> list[str]:
    """skip_dirs for a game's files; a Proton prefix sits inside a Steam library, so it isn't skipped."""
    if env and env.drive_c:
        return [d for d in skip_dirs if not is_under(env.drive_c, d)]
    return skip_dirs


def all_skipped(files, skip_dirs) -> bool:
    """True if every file sits under a Steam/install dir, so nothing of the game can be synced."""
    return all(any(is_under(f, d) for d in skip_dirs) for f in files)


def plan_game(ctx, row, manifest, skip_dirs, include_config):
    """Returns (roots, ignores_by_root, skipped, problems, settings_files) for one game."""
    entry = manifest.get(row.name) or {}
    env = row.env or ctx.home
    skip_dirs = row_skip_dirs(row.env, skip_dirs)
    saves, configs = classify_files(row.files, entry, env)
    configs = [c for c in configs if not any(is_under(c, d) for d in skip_dirs)]
    roots, skipped, problems = compute_roots(row.files if include_config else saves,
                                             list(entry.get("files", {})), env, skip_dirs)
    ignores = {r: [] if include_config else config_ignores(r, saves, configs) for r in roots}
    return roots, ignores, skipped, problems, configs


def ask_include_config(args, configs, home) -> bool:
    if not configs or args.exclude_config:
        return False
    if args.include_config:
        return True
    if args.yes:
        return False
    print(f"  Found {len(configs)} settings files (resolution, graphics, keybinds...), e.g.:")
    for c in configs[:5]:
        print(f"    {tilde(c, home)}")
    if len(configs) > 5:
        print(f"    ... and {len(configs) - 5} more")
    return confirm("  Also sync these settings files?", default=False)


def list_games(rows, show_all, how_to_show):
    """Print the numbered game list. Returns (visible rows, number of hidden rows)."""
    visible = [r for r in rows if show_all or (r.reason is None and r.selectable)]
    hidden = len(rows) - len(visible)
    print()
    if not rows:
        print("No games with saves found.")
    elif not visible:
        print("No games to share.")
    for i, r in enumerate(visible, 1):
        marks = []
        if r.shared:
            marks.append("shared")
        if r.ignored:
            marks.append("ignored in Ludusavi")
        if r.reason:
            marks.append("excluded: " + r.reason)
        mark = f"  [{' | '.join(marks)}]" if marks else ""
        print(f"{i:3}. {r.name}  ({human(r.size)}){mark}")
    if hidden:
        print(f"\n{hidden} more games excluded (no save files, saves only in Steam/install dirs, etc.); "
              f"{how_to_show} to see them.")
    return visible, hidden


def cmd_share(args):
    ctx = make_ctx(args)
    reg_dir = ctx.registry_dir()
    registry = read_registry(reg_dir)
    local_folders = ctx.local.folders()
    local_ids = {f["id"] for f in local_folders}
    taken = {f["id"].lower() for f in local_folders} | {f["id"].lower() for f in ctx.server.folders()} \
        | {k.lower() for k in registry}

    if args.test_path:  # testing: share the given dir directly, bypassing Ludusavi
        game = args.game or "Savething Test"
        root = norm(os.path.abspath(args.test_path))
        conflict = nesting_conflict(root, local_folders)
        if conflict:
            raise SavethingError(f"{root} overlaps existing share {conflict}")
        targets = choose_devices(ctx, args.devices if args.devices is not None else "")
        ignores = [x.strip() for x in (args.test_ignore or "").split(",") if x.strip()]
        create_share(ctx, reg_dir, game, root, allocate_id(ctx, game, taken), targets, ignores)
        return

    rows, manifest, skip_dirs = scan_games(ctx, registry, local_ids)
    visible, hidden = list_games(rows, args.all, "use --all" if args.dry_run else "type showall")
    if not rows or (args.dry_run and not visible):
        return

    if args.dry_run:
        print("\n-- Directories that would be synced (--dry-run, nothing is created) --")
        for r in visible:
            if not r.selectable:
                continue
            include = bool(args.include_config)
            roots, ignores, skipped, problems, configs = plan_game(ctx, r, manifest, skip_dirs, include)
            note = ""
            if configs and not include:
                note = f"  ({len(configs)} settings files; share will ask, default: don't sync)"
            print(f"\n{r.name}{note}")
            if not roots and configs and not include:
                print("    (only settings files found; nothing to sync)")
            for x in roots:
                print(f"    -> {x}")
                for ig in ignores[x]:
                    print(f"         ignore /{ig}")
            if skipped:
                print(f"    (skipping {len(skipped)} files under Steam/install dirs)")
            if problems:
                print(f"    ! {len(problems)} files sit directly in a broad dir and cannot be synced")
        return

    while True:
        extra = "showall = list all games; " if hidden else ""
        idx = ask_selection(f"\nGames to share (e.g. 1,3,5-7; {extra}Enter to cancel): ", len(visible),
                            commands=("showall",) if hidden else ())
        if idx != "showall":
            break
        visible, hidden = list_games(rows, True, "")
    chosen = [visible[i] for i in idx if visible[i].selectable]
    if not chosen:
        print("Cancelled.")
        return
    targets = choose_devices(ctx, args.devices)

    for r in chosen:
        print(f"\n[{r.name}]")
        if r.shared:
            for e in r.shared:
                add_devices_to_share(ctx, reg_dir, e, targets)
            continue
        roots, ignores, skipped, problems, configs = plan_game(ctx, r, manifest, skip_dirs, False)
        if ask_include_config(args, configs, ctx.home):
            roots, ignores, skipped, problems, configs = plan_game(ctx, r, manifest, skip_dirs, True)
        elif configs and not roots:
            print("  Only settings files found; nothing to sync, skipping.")
            continue
        if skipped:
            print(f"  Skipping {len(skipped)} files under Steam/install dirs")
        if problems:
            print(f"  ! {len(problems)} files sit directly in a broad dir and cannot be synced, e.g. {problems[0]}")
        ok_roots = []
        for root in roots:
            conflict = nesting_conflict(root, local_folders)
            if conflict:
                print(f"  ! Skipping {root}: overlaps existing share {conflict}")
            else:
                ok_roots.append(root)
        if not ok_roots:
            print("  No directory to sync, skipping.")
            continue
        for root in ok_roots:
            print(f"  Will sync: {root}")
            for ig in ignores[root]:
                print(f"    except settings: /{ig}")
        if not args.yes and not confirm("  Create?"):
            continue
        for root in ok_roots:
            create_share(ctx, reg_dir, r.name, root, allocate_id(ctx, r.name, taken), targets, ignores[root],
                         r.env, steam_ids_of(manifest.get(r.name) or {}))
            local_folders.append({"id": "?", "path": root})
    print("\nDone. Run `savething accept` on the other devices to receive.")


# ---------------------------------------------------------------- accept

def resolve_ci(path) -> str:
    """Match each existing component case-insensitively, as Wine does for the game;
    components that do not exist yet are kept as given."""
    parts = norm(path).split("/")
    out = [parts[0]]
    for i in range(1, len(parts)):
        base = "/".join(out) or "/"
        comp = parts[i]
        if not os.path.exists(os.path.join(base, comp)):
            try:
                hits = sorted(n for n in os.listdir(base) if n.casefold() == comp.casefold())
            except OSError:
                hits = []
            if not hits:
                return "/".join(out + parts[i:])
            comp = hits[0]
        out.append(comp)
    return "/".join(out)


def match_shortcut(entry, shortcuts, libs, install_dirs=list) -> int | None:
    """App ID of the shortcut that most likely runs this game, or None if unsure. Tried in
    order: the game's name as the shortcut name / exe name / a folder on its path; a prefix
    that already has this save dir; the game's install folder names (from Ludusavi's
    manifest, fetched only if needed via the install_dirs callable) on the shortcut's path."""
    def unique(ids):
        return ids[0] if len(ids) == 1 else None

    game = title_key(entry["game"])
    hit = unique([a for a, sc in shortcuts.items() if game in sc.keys()])
    if hit is None:
        have = []
        for a in shortcuts:
            pfx = find_prefix(a, libs)
            path = pfx and from_portable(entry["portable_path"], prefix_env(pfx))
            if path and os.path.isdir(resolve_ci(path)):
                have.append(a)
        hit = unique(have)
    if hit is None:
        dirs = {title_key(d) for d in install_dirs()} - {""}
        hit = unique([a for a, sc in shortcuts.items() if dirs & sc.keys()])
    return hit


def choose_prefix(ctx, entry, shortcuts, libs, yes, install_dirs=list, steam_ids=set) -> Env | None:
    """Env of the Proton prefix that runs this game: the Steam game's own prefix if it is
    installed in Steam, else a non-Steam shortcut's. The choice is remembered in config.json
    ("prefix_map"). steam_ids is a callable, like install_dirs, so the manifest is only read if needed."""
    game = entry["game"]
    pmap = ctx.cfg.setdefault("prefix_map", {})
    appid = pmap.get(game)
    if appid is None:
        in_steam = sorted(set(entry.get("steam_ids") or steam_ids()) & installed_steam_ids(libs))
        if in_steam:
            appid = in_steam[0]
            print(f"  {game}: using Steam game prefix ({appid})")
        elif (appid := match_shortcut(entry, shortcuts, libs, install_dirs)) is not None:
            print(f"  {game}: using Steam shortcut \"{shortcuts[appid].name}\"")
        elif not shortcuts:
            print(f"  ! Skipping {game}: not installed in Steam and no non-Steam games in Steam; "
                  f"install it or add it to Steam first")
            return None
        elif yes:
            print(f"  ! Skipping {game}: no Steam shortcut named like it; run accept without --yes to pick one")
            return None
        else:
            items = sorted(shortcuts.items(), key=lambda kv: kv[1].name.casefold())
            print(f"  Which non-Steam game in Steam is {game}?")
            for i, (a, sc) in enumerate(items, 1):
                print(f"    {i}. {sc.name}  ({sc.exe.strip(chr(34))})")
            idx = ask_selection("  Number (Enter = skip): ", len(items))
            if not idx:
                print(f"  Skipped {game}.")
                return None
            appid = items[idx[0]][0]
        pmap[game] = appid
        save_config(ctx.cfg_dir, ctx.cfg)
    pfx = find_prefix(appid, libs)
    if not pfx:
        print(f"  ! Skipping {game}: its Proton prefix does not exist yet; "
              f"start the game once from Steam, then run accept again")
        return None
    return prefix_env(pfx)


def cmd_accept(args):
    ctx = make_ctx(args)
    reg_dir = ctx.registry_dir()
    registry = read_registry(reg_dir)
    local_folders = ctx.local.folders()
    local_ids = {f["id"] for f in local_folders}
    known = {d["deviceID"] for d in ctx.local.devices()}

    to_remove, todo = split_registry(registry, ctx.my_id, local_ids)
    for e in to_remove:
        stop_local(ctx, e)
        local_folders = [f for f in local_folders if f["id"] != e["folder_id"]]
        print(f"Stopped syncing {e['game']} (unshared on {e.get('removed_by_name', '?')}); files kept.")
    if not todo:
        if to_remove:
            return
        print("No save shares waiting to be accepted.")
        pend = [fid for fid in ctx.local.pending_folders() if fid.startswith(FOLDER_PREFIX) and fid not in registry]
        if pend:
            print(f"({len(pend)} Syncthing offers are not in the registry yet: {', '.join(pend)}; "
                  f"the registry may still be syncing, try again shortly)")
        return

    print("Save shares offered to this device:")
    for i, e in enumerate(todo, 1):
        if IS_WINDOWS:
            path = from_portable(e["portable_path"], ctx.home)
            note = ""
            if os.path.isdir(path) and any(Path(path).iterdir()):
                note = "  [local files exist; Syncthing will merge]"
        else:  # the Proton prefix is picked per game below
            path, note = e["portable_path"], ""
        print(f"  {i}. {e['game']}  <-  {e.get('created_by_name', '?')}\n       {path}{note}")
    idx = list(range(len(todo))) if args.yes else \
        ask_selection("Accept which? (numbers; Enter = all; q = cancel): ", len(todo), empty_means_all=True)

    libs = [] if IS_WINDOWS else steam_libraries()
    shortcuts = {} if IS_WINDOWS else steam_shortcuts()
    manifest = None

    def game_entry(ctx, game):  # Ludusavi is optional on receiving devices
        nonlocal manifest
        if manifest is None:
            try:
                manifest = ludusavi_json(ctx, "manifest", "show", "--api")
            except (SavethingError, ValueError):
                manifest = {}
        return manifest.get(game) or {}

    for i in idx:
        e = todo[i]
        fid = e["folder_id"]
        env = None
        if IS_WINDOWS:
            path = from_portable(e["portable_path"], ctx.home)
        else:
            env = choose_prefix(ctx, e, shortcuts, libs, args.yes,
                                lambda: list(game_entry(ctx, e["game"]).get("installDir") or {}),
                                lambda: steam_ids_of(game_entry(ctx, e["game"])))
            if env is None:
                continue
            path = from_portable(e["portable_path"], env)
            if path is None:
                print(f"  ! Skipping {e['game']}: {e['portable_path']} is not on C: and cannot be mapped into Proton")
                continue
            path = resolve_ci(path)
        conflict = nesting_conflict(path, local_folders)
        if conflict:
            print(f"  ! Skipping {e['game']}: {path} overlaps existing share {conflict}")
            continue
        server_f = ctx.server.folder(fid)
        if server_f is None:
            print(f"  ! Skipping {e['game']}: {fid} not found on the server (maybe deleted)")
            continue
        if ctx.my_id not in {d["deviceID"] for d in server_f["devices"]}:
            ctx.server.set_folder_devices(fid, [ctx.my_id])
        os.makedirs(path, exist_ok=True)
        peers = [d for d in e.get("devices", []) if d in known and d != ctx.my_id]
        ignores = e.get("ignore") or []
        ctx.local.put_folder(fid, e.get("label") or LABEL_PREFIX + e["game"], path,
                             [ctx.my_id, ctx.server_id, *peers], ignores=ignores, ignore_case=env is not None)
        ctx.local.dismiss_pending(fid)
        local_folders.append({"id": fid, "path": path})
        print(f"  OK {e['game']}  ->  {path}")
        if ignores:
            print(f"     keeping local settings: {', '.join(ignores)}")


# ---------------------------------------------------------------- unshare

def stop_local(ctx, entry):
    """Remove the share from this device and ignore further offers of it. Files are kept."""
    fid = entry["folder_id"]
    label = entry.get("label") or LABEL_PREFIX + entry["game"]
    # delete first: Syncthing drops ignoredFolders entries for folders that are still configured
    ctx.local.delete_folder(fid)
    for d in dict.fromkeys([ctx.server_id, *entry.get("devices", [])]):
        if d != ctx.my_id:
            ctx.local.ignore_offer(d, fid, label)
    ctx.local.dismiss_pending(fid)


def unshare_entry(ctx, reg_dir, entry):
    fid = entry["folder_id"]
    label = entry.get("label") or LABEL_PREFIX + entry["game"]
    # ignore right after deleting (it is dropped while the folder still exists), so the server
    # does not keep showing an offer from a device that still shares it.
    # Note: server auto-accept for a device overrides this; keep auto-accept off on the server.
    ctx.server.delete_folder(fid)
    for d in entry.get("devices", []):
        ctx.server.ignore_offer(d, fid, label)
    stop_local(ctx, entry)
    entry.update(removed=True, removed_by_name=ctx.dev_name(ctx.my_id),
                 removed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    write_entry(reg_dir, entry)


def cmd_unshare(args):
    ctx = make_ctx(args)
    reg_dir = ctx.registry_dir()
    entries = [e for e in read_registry(reg_dir).values() if active(e)]
    if args.game:
        w = args.game.lower()
        chosen = [e for e in entries if w in (e["folder_id"].lower(), e["game"].lower())]
        if not chosen:
            raise SavethingError(f"No active save share matches: {args.game}")
    else:
        if not entries:
            print("No active save shares.")
            return
        local = {f["id"]: f for f in ctx.local.folders()}
        for i, e in enumerate(entries, 1):
            path = (local.get(e["folder_id"]) or {}).get("path") or "(not on this device)"
            devs = ", ".join(ctx.dev_name(d) for d in e.get("devices", []) if d != ctx.my_id) or "-"
            print(f"{i:3}. {e['game']}  [{e['folder_id']}]\n       {path}\n       shared with: server, {devs}")
        idx = ask_selection("\nStop syncing which? (e.g. 1,3; Enter to cancel): ", len(entries))
        chosen = [entries[i] for i in idx]
        if not chosen:
            print("Cancelled.")
            return
    print("\nWill stop syncing (save files are kept on every device and on the server):")
    for e in chosen:
        print(f"  - {e['game']}  [{e['folder_id']}]")
    if not args.yes and not confirm("Continue?"):
        print("Cancelled.")
        return
    for e in chosen:
        unshare_entry(ctx, reg_dir, e)
        print(f"  OK stopped {e['game']}")
    print("\nFiles were not deleted. Other devices stop syncing the next time they run `savething accept`.")


# ---------------------------------------------------------------- status

def cmd_status(args):
    ctx = make_ctx(args)
    reg_dir = ctx.registry_dir()
    registry = read_registry(reg_dir)
    folders = [f for f in ctx.local.folders() if f["id"].startswith(FOLDER_PREFIX)]
    if not folders:
        print("No save shares on this device yet.")
    for f in folders:
        fid = f["id"]
        st = ctx.local.request("GET", "/rest/db/status", query={"folder": fid}) or {}
        try:
            comp = ctx.local.request("GET", "/rest/db/completion", query={"folder": fid, "device": ctx.server_id})
            server_pct = f"{comp.get('completion', 0):.0f}%"
        except SavethingError:
            server_pct = "?"
        devs = ", ".join(ctx.dev_name(d["deviceID"]) for d in f["devices"] if d["deviceID"] != ctx.my_id)
        game = (registry.get(fid) or {}).get("game") or f.get("label")
        print(f"{game}\n    {f['path']}\n    state: {st.get('state', '?')}  Server sync: {server_pct}  devices: {devs}")
        ignores = (registry.get(fid) or {}).get("ignore") or []
        if ignores:
            print(f"    not syncing {len(ignores)} settings paths: {', '.join(ignores)}")
    local_ids = {f["id"] for f in folders}
    leftover, waiting = split_registry(registry, ctx.my_id, local_ids)
    if leftover:
        print(f"\n{len(leftover)} shares were unshared on another device: "
              f"{', '.join(e['game'] for e in leftover)} (run savething accept to stop syncing them here)")
    if waiting:
        print(f"\n{len(waiting)} shares waiting to be accepted: {', '.join(e['game'] for e in waiting)} (run savething accept)")


# ---------------------------------------------------------------- main

MENU = [
    ("share", "Share saves: scan with Ludusavi, pick games and devices", ["share"]),
    ("preview", "Preview what share would sync (changes nothing)", ["share", "--dry-run"]),
    ("accept", "Accept new saves shared to this device; also drops ones already unshared elsewhere",
     ["accept"]),
    ("unshare", "Stop syncing a save on all devices (files are kept)", ["unshare"]),
    ("status", "Show sync status", ["status"]),
    ("init", "Set up this device (run once per device)", ["init"]),
]


def run(args) -> int:
    try:
        args.func(args)
    except SavethingError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def interactive_menu(ap, base) -> int:
    """Shown when Savething is started without a command, e.g. by double-clicking the exe."""
    while True:
        print("\n=== Savething: game save sync ===")
        cfg_dir = Path(base[base.index("--config-dir") + 1]) if "--config-dir" in base else default_config_dir()
        if not (cfg_dir / "config.json").is_file():
            print("This device is not set up yet; choose 'init' first.")
        print()
        for i, (name, desc, _) in enumerate(MENU, 1):
            print(f"  {i}. {name:<8} {desc}")
        print("  0. exit")
        choice = ask("\nChoose a number: ")
        if choice in ("", "0", "q", "quit", "exit"):
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            print("  Invalid choice.")
            continue
        name, _, cmd = MENU[int(choice) - 1]
        print(f"\n--- {name} ---")
        run(ap.parse_args(base + cmd))
        ask("\nPress Enter to return to the menu...")


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if isinstance(s, io.TextIOWrapper):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="savething", description="Sync game saves with Ludusavi + Syncthing. "
                                 "Run without a command for an interactive menu.")
    ap.add_argument("--config-dir", help="config dir (default %%APPDATA%%\\savething or ~/.config/savething)")
    ap.add_argument("--st-home", help="local Syncthing home dir (auto-detected by default)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("init", help="initialize this device (once per device)")
    p.add_argument("--server-url")
    p.add_argument("--server-key")
    p.add_argument("--server-root", help="save directory on the server (default /srv/media/game/savething)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("share", help="pick games and create shares")
    p.add_argument("--all", action="store_true",
                   help="also show excluded games (no save files, etc.); in the menu, type showall instead")
    p.add_argument("--dry-run", action="store_true", help="only show the list and target dirs; create nothing")
    p.add_argument("--devices", help="target devices (names or ID prefixes, comma-separated); skips the prompt")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation (settings files are not synced)")
    cfg_group = p.add_mutually_exclusive_group()
    cfg_group.add_argument("--include-config", action="store_true",
                           help="also sync settings files (resolution, graphics...) without asking")
    cfg_group.add_argument("--exclude-config", action="store_true",
                           help="never sync settings files, without asking")
    p.add_argument("--test-path", help=argparse.SUPPRESS)
    p.add_argument("--test-ignore", help=argparse.SUPPRESS)
    p.add_argument("--game", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_share)

    p = sub.add_parser("accept", help="accept save shares offered to this device")
    p.add_argument("--yes", action="store_true", help="accept all without asking")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("unshare", help="stop syncing a save on all devices (files are kept)")
    p.add_argument("--game", help="folder ID or game name; skips the menu")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_unshare)

    p = sub.add_parser("status", help="show save share status")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    try:
        if args.cmd is None:
            base = []
            if args.config_dir:
                base += ["--config-dir", args.config_dir]
            if args.st_home:
                base += ["--st-home", args.st_home]
            return interactive_menu(ap, base)
        return run(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
