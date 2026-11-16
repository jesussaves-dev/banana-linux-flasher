#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BananaLinux Flasher
===================

A minimalist, cross-platform (Linux / Windows / macOS) GUI utility that:

  1. Dynamically resolves the newest release on GitHub and picks the
     correct image asset for the selected handheld (Anbernic RG DS,
     Anbernic RG DS Plus, or Retroid Pocket Classic).
  2. Enumerates physical microSD drives, always filtering out the drive the
     host operating system is running from.
  3. Securely wipes the partition table / boot area of the target (wipefs + dd
     on Linux, diskpart on Windows, diskutil on macOS).
  4. Decompresses the ``.img.gz`` on the fly and streams the raw bytes straight
     to the block device -- no BalenaEtcher, Rufus or other third-party flasher.

Only the Python standard library is required (Tkinter ships with CPython).

Running it
----------
    Linux / macOS :  python3 flash_banana.py      # will ask for your sudo password
    Windows       :  python flash_banana.py       # run as Administrator (auto-elevates)

Author: built for Thomas' BananaLinux flashing workflow.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import plistlib
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------------------
#  Platform detection
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

DEVNULL = subprocess.DEVNULL

# Windows: console tools (powershell, diskpart, mountvol) launched from a
# windowed (--noconsole) build would each flash a black console window. That
# looks alarming to users and "hidden console child processes" is a classic
# antivirus heuristic, so every such child is started with CREATE_NO_WINDOW.
CREATE_NO_WINDOW = 0x08000000


def _hidden_run(cmd: list[str], timeout: int = 60, encoding: str = "utf-8",
                merge_stderr: bool = False) -> subprocess.CompletedProcess:
    """
    Run a console tool without a window, stdin closed, output decoded leniently.

    A windowed PyInstaller build has no valid stdio handles, so stdin is always
    redirected. Output is decoded with ``errors="replace"`` so a localised
    Windows (non-ASCII drive models, OEM code pages) can never crash parsing.
    """
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    proc = subprocess.run(
        cmd,
        stdin=DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else DEVNULL,
        timeout=timeout,
        **kwargs,
    )
    proc.stdout = (proc.stdout or b"").decode(encoding, errors="replace")
    return proc

# ---------------------------------------------------------------------------
#  Application / release constants
# ---------------------------------------------------------------------------
APP_NAME = "BananaLinux Flasher"
APP_VERSION = "1.0.5"

DEFAULT_REPO = "JorreFog/BananaLinux"
GITHUB_REPO = DEFAULT_REPO
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_LIST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"

USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+https://github.com/{GITHUB_REPO})"

# Friendly model name -> release asset filename
MODELS = {
    "Anbernic RG DS Plus": "banana-rgds-plus.img.gz",
    "Anbernic RG DS": "banana-rgds.img.gz",
    "Retroid Pocket Classic": "banana-retroid-pocket-classic.img.gz",
}

# Friendly model name -> GitHub repository (owner/repo)
MODEL_REPOS = {
    "Anbernic RG DS Plus": "JorreFog/BananaLinux",
    "Anbernic RG DS": "JorreFog/BananaLinux",
    "Retroid Pocket Classic": "jesussaves-dev/Banana-Pocket",
}

DEFAULT_MODEL = "Anbernic RG DS Plus"


def repo_for_model(model: str) -> str:
    """Return the GitHub repository (owner/repo) for a given handheld model."""
    return MODEL_REPOS.get(model, DEFAULT_REPO)


def repo_for_asset(asset_name: str) -> str:
    """Return the GitHub repository (owner/repo) that publishes the given asset."""
    for model, asset in MODELS.items():
        if asset == asset_name:
            return MODEL_REPOS.get(model, DEFAULT_REPO)
    return DEFAULT_REPO

CHUNK_SIZE = 1 << 20          # 1 MiB streaming granularity
# Raw \\.\PhysicalDriveN writes must be whole sectors; Windows rejects a
# ragged final write with ERROR_INVALID_PARAMETER (87). 4096 covers both 512e
# and 4Kn media, and the disk was wiped first so the zero tail is harmless.
WIN_SECTOR_ALIGN = 4096
ZERO_WIPE_BYTES = 10 * 1024 * 1024   # "first 10MB" zeroing on Linux

# ---------------------------------------------------------------------------
#  Visual theme -- one cohesive palette shared by every widget
# ---------------------------------------------------------------------------
# "Banana Blast", matching the Banana Pocket site (site/style.css). Colours sit
# on the Mega Drive's 3-bit grid (00 24 49 6d 92 b6 db ff).
BG         = "#000000"   # --void: app canvas
SURFACE    = "#000000"   # cards are outlined, not filled
INK        = "#ffffff"   # --ink
MUTED      = "#929292"   # --dim
FAINT      = "#6d6d6d"   # hints / disabled text
BORDER     = "#494949"   # --line
ACCENT     = "#ff00b6"   # --a: pink, primary action
ACCENT_TX  = "#000000"   # text on pink
CYAN       = "#00dbff"   # --b: rails, ghost buttons, focus
LABEL      = "#ffdb00"   # --label: yellow headings
HEADER_BG  = "#000000"
HEADER_TX  = "#ffffff"
HEADER_SUB = "#929292"
LOG_BG     = "#0a0a0a"   # activity log panel
LOG_TX     = "#dbdbdb"
LOG_DIM    = "#6d6d6d"
DANGER     = "#ff2424"
# The site's --chrome title gradient, as stacked bands for the header rule.
CHROME_STOPS = ("#ffffff", "#b6ffff", "#00dbff", "#2449db",
                "#ff6d00", "#ffb600", "#ffff49")
PIXEL_FONT = "Press Start 2P"
BODY_FONT = "Chakra Petch"

# sudo is resolved once at runtime; an empty list means "we are already root".
SUDO_PREFIX: list[str] = []


# ===========================================================================
#  Small helpers
# ===========================================================================
def format_bytes(num: float) -> str:
    """Human friendly byte size."""
    if not num or num < 0:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024.0 or unit == "TiB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} TiB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}h {minutes:02d}m"
    if minutes:
        return f"{minutes:d}m {secs:02d}s"
    return f"{secs:d}s"


def resource_path(relative: str) -> str:
    """
    Resolve a bundled resource path.

    When frozen by PyInstaller, data files are unpacked to ``sys._MEIPASS``;
    when running from a checkout, they live next to this script.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def pick_family(root, preferences: tuple[str, ...], fallback: str) -> str:
    """Return the first installed font family from `preferences`."""
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except Exception:
        return fallback
    for name in preferences:
        if name.lower() in available:
            return name
    return fallback


def register_bundled_fonts() -> None:
    """
    Make the bundled OFL fonts visible to Tk for this process only.

    Must run before the Tk root exists. Windows uses AddFontResourceExW with
    FR_PRIVATE; Linux adds them to fontconfig's in-process app font set.
    Anything else (or any failure) silently leaves the system fallbacks.
    """
    folder = resource_path(os.path.join("docs", "fonts"))
    try:
        files = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                 if n.lower().endswith(".ttf")]
    except OSError:
        return
    if not files:
        return
    try:
        import ctypes
        import ctypes.util
        if IS_WINDOWS:
            fr_private = 0x10
            add = ctypes.windll.gdi32.AddFontResourceExW
            add.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            for path in files:
                add(path, fr_private, None)
        elif sys.platform.startswith("linux"):
            lib = ctypes.util.find_library("fontconfig") or "libfontconfig.so.1"
            fc = ctypes.CDLL(lib)
            fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            for path in files:
                fc.FcConfigAppFontAddFile(None, os.fsencode(path))
    except Exception:
        pass


class PixelButton(tk.Canvas):
    """
    The site's .btn: a skewed slab with a hard offset shadow.

    variant "main" = pink face / cyan shadow, "ghost" = black face, cyan
    outline / pink shadow. Hover lifts it onto a yellow shadow, press sinks it.
    Supports configure(state="normal"|"disabled") like a ttk.Button.
    """

    SHADOW = 5
    SKEW = 6     # px of horizontal lean across the button height

    def __init__(self, parent, text: str, command=None, variant: str = "main",
                 font=None, bg: str = BG, state: str = "normal"):
        self._font = tkfont.Font(parent, font=font or ("TkDefaultFont", 9))
        self._text = text
        self._command = command
        self._variant = variant
        self._state = state
        self._hover = False
        self._pressed = False
        self._focused = False
        pad_x, pad_y = 18, 10
        self._face_w = self._font.measure(text) + 2 * pad_x + self.SKEW
        self._face_h = self._font.metrics("linespace") + 2 * pad_y
        pad = 3   # room for the hover lift
        super().__init__(
            parent, width=self._face_w + self.SHADOW + 2 * pad,
            height=self._face_h + self.SHADOW + 2 * pad, bg=bg,
            highlightthickness=0, bd=0,
            takefocus=0 if state == "disabled" else 1,
            cursor="hand2" if state != "disabled" else "",
        )
        self._pad = pad
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<FocusIn>", lambda e: self._set(focused=True))
        self.bind("<FocusOut>", lambda e: self._set(focused=False))
        self.bind("<KeyPress-space>", lambda e: self._invoke())
        self.bind("<KeyPress-Return>", lambda e: self._invoke())
        self._draw()

    # ttk.Button-compatible state handling
    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._state = kw.pop("state")
            self.config_cursor()
            self._draw()
        if cnf or kw:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def config_cursor(self) -> None:
        enabled = self._state != "disabled"
        super().configure(cursor="hand2" if enabled else "",
                          takefocus=1 if enabled else 0)

    def _set(self, **flags) -> None:
        for key, value in flags.items():
            setattr(self, "_" + key, value)
        self._draw()

    def _release(self, event) -> None:
        was_pressed = self._pressed
        self._set(pressed=False)
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if was_pressed and inside:
            self._invoke()

    def _invoke(self) -> None:
        if self._state != "disabled" and self._command:
            self._command()

    def _slab(self, x: int, y: int, **kw) -> None:
        w, h, k = self._face_w, self._face_h, self.SKEW
        self.create_polygon(x + k, y, x + w, y, x + w - k, y + h, x, y + h, **kw)

    def _draw(self) -> None:
        self.delete("all")
        disabled = self._state == "disabled"
        if self._variant == "main":
            face, edge, ink, shadow = ACCENT, ACCENT, ACCENT_TX, CYAN
        else:
            face, edge, ink, shadow = BG, CYAN, CYAN, ACCENT
        if disabled:
            face, edge, ink, shadow = "#121212", BORDER, FAINT, "#121212"

        lift = 0
        if not disabled:
            if self._pressed:
                lift = self.SHADOW - 1
            elif self._hover or self._focused:
                lift = -2
                shadow = LABEL
        base = self._pad
        if not (self._pressed and not disabled):
            self._slab(base + self.SHADOW, base + self.SHADOW, fill=shadow,
                       outline=shadow)
        x = y = base + lift
        self._slab(x, y, fill=face, outline=edge, width=3)
        self.create_text(x + self._face_w / 2, y + self._face_h / 2 + 1,
                         text=self._text.upper(), fill=ink, font=self._font)


def is_elevated() -> bool:
    """True when the process can already write raw block devices."""
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False


class Cancelled(Exception):
    """Raised internally when the user presses Cancel."""


class FlasherError(RuntimeError):
    """A user-facing error with a clean message."""


# ===========================================================================
#  Privilege handling (sudo caching + keep-alive, Windows elevation)
# ===========================================================================
class Privileges:
    """
    Manages elevated access in a portable way.

    * Linux / macOS : validates the sudo password once, caches the sudo
      timestamp and keeps it alive in a daemon thread, so the actual flashing
      commands can run non-interactively (`sudo -n`) while stdin stays free for
      the image data.
    * Windows       : the GUI runs unprivileged and the write elevates itself
      on demand in a short-lived helper process (see `_spawn_elevated_write`);
      nothing is handled by this class.
    """

    def __init__(self, log):
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- POSIX -------------------------------------------------------------
    def cache_sudo(self, password: str) -> bool:
        """Validate `password` and remember it in sudo's timestamp file."""
        global SUDO_PREFIX
        if is_elevated():
            SUDO_PREFIX = []
            return True
        try:
            proc = subprocess.run(
                ["sudo", "-S", "-p", "", "-v"],
                input=(password + "\n").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(f"sudo failed: {exc}")
            return False

        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            self._log(f"sudo rejected the password. {detail}")
            return False

        SUDO_PREFIX = ["sudo", "-n"]
        self._start_keepalive()
        return True

    def _start_keepalive(self) -> None:
        """Refresh the sudo timestamp so long flashes never time out."""
        if self._thread is not None:
            return

        def loop() -> None:
            # Default sudo timeout is 5 min; refresh comfortably inside that.
            while not self._stop.wait(240):
                try:
                    subprocess.run(
                        ["sudo", "-n", "-v"],
                        stdout=DEVNULL,
                        stderr=DEVNULL,
                        timeout=15,
                    )
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=loop, name="sudo-keepalive", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()


# ===========================================================================
#  Networking -- GitHub release lookup + streaming download
# ===========================================================================
def _open_url(url: str, timeout: int = 30):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _fetch_json(url: str) -> object:
    """GET `url` and parse the JSON body, mapping errors to FlasherError."""
    try:
        with _open_url(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FlasherError(f"GitHub returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise FlasherError(
            f"Could not reach GitHub ({exc.reason}). Check your connection."
        ) from exc
    except Exception as exc:  # malformed JSON, TLS issues, ...
        raise FlasherError(f"Unexpected error talking to GitHub: {exc}") from exc


def _release_candidates(repo: str = DEFAULT_REPO) -> list[dict]:
    """
    Return candidate release objects, newest first.

    The documented `/releases/latest` endpoint is tried first, exactly as
    requested -- but upstream projects currently ship pre-releases, and
    GitHub's `latest` endpoint deliberately ignores pre-releases (returning
    404). We therefore fall back to the release list and walk it newest-first,
    skipping drafts, so the tool keeps working.
    """
    errors: list[FlasherError] = []
    releases: list[dict] = []
    seen: set = set()

    def add(release: dict) -> None:
        key = release.get("id") or release.get("tag_name")
        if key in seen:
            return
        seen.add(key)
        releases.append(release)

    latest_api = f"https://api.github.com/repos/{repo}/releases/latest"
    list_api = f"https://api.github.com/repos/{repo}/releases"

    try:
        latest = _fetch_json(latest_api)
        if isinstance(latest, dict):
            add(latest)
    except FlasherError as exc:
        errors.append(exc)

    try:
        listing = _fetch_json(f"{list_api}?per_page=30")
        if isinstance(listing, list):
            for release in listing:
                if isinstance(release, dict) and not release.get("draft"):
                    add(release)
    except FlasherError as exc:
        errors.append(exc)

    if not releases and errors:
        # Surface the real reason (offline, rate-limited, ...) instead of a
        # misleading "no release found".
        raise errors[0]
    return releases


def resolve_release_asset(asset_name: str, repo: str | None = None) -> dict:
    """
    Return {'name', 'url', 'size', 'tag', 'checksum', 'repo'} for `asset_name`.

    The newest non-draft release that actually contains the requested asset
    wins. `checksum` is the matching SHA-256 from the release's SHA256SUMS
    file when one is published, or from the release asset's sha256 digest,
    otherwise None.
    """
    if repo is None:
        repo = repo_for_asset(asset_name)

    seen_failure: str | None = None
    tried: list[str] = []

    for release in _release_candidates(repo):
        assets = {
            a.get("name"): a for a in (release.get("assets") or []) if a.get("name")
        }
        if not assets:
            continue

        tag = release.get("tag_name") or "latest"
        asset = assets.get(asset_name)
        if asset is None:
            tried.append(tag)
            continue

        # Model tokens let us pair e.g. banana-rgds-plus.img.gz with
        # SHA256SUMS-rgds-plus.txt (but not with SHA256SUMS-rgds.txt).
        want = _model_tokens(asset_name)
        sums_files = [
            name for name in assets
            if name.startswith("SHA256SUMS") and name.endswith(".txt")
        ]
        sums_files.sort(key=lambda name: _model_tokens(name) != want)

        checksum = None
        for sums_name in sums_files:
            digest = _fetch_checksum(assets[sums_name]["browser_download_url"],
                                     asset_name)
            if digest:
                checksum = digest
                break

        # If no separate checksums file was published, check if GitHub's asset
        # metadata provides a sha256 digest.
        if checksum is None:
            raw_digest = asset.get("digest") or ""
            if raw_digest.startswith("sha256:"):
                checksum = raw_digest.split(":", 1)[1].strip()

        return {
            "name": asset["name"],
            "url": asset["browser_download_url"],
            "size": int(asset.get("size") or 0),
            "tag": tag,
            "checksum": checksum,
            "repo": repo,
        }

    if tried:
        seen_failure = (
            f"'{asset_name}' was not found in these recent releases: "
            + ", ".join(tried[:6])
        )
    releases_page = f"https://github.com/{repo}/releases/latest"
    raise FlasherError(
        seen_failure
        or f"No published release with assets was found for {repo}. "
           "See " + releases_page
    )


def _model_tokens(name: str) -> list[str]:
    """Reduce an asset/sums filename to its model tokens, e.g. ['rgds','plus']."""
    stem = name
    for suffix in (".img.gz", ".update.tar", ".img", ".txt", ".tar"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    tokens = [t for t in stem.split("-") if t]
    for prefix in ("banana", "SHA256SUMS"):
        if tokens and tokens[0] == prefix:
            tokens = tokens[1:]
    return tokens


def _fetch_checksum(sums_url: str, asset_name: str) -> str | None:
    """
    Fetch a SHA256SUMS file and return the hex digest whose *exact* filename
    matches `asset_name`, or None when the file does not cover that asset.
    """
    try:
        with _open_url(sums_url) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip()
        filename = parts[-1].lstrip("*").strip()
        if filename != asset_name:
            continue
        if len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest):
            return digest.lower()
    return None


def download_asset(
    url: str,
    destination: str,
    progress,
    cancel: threading.Event,
) -> None:
    """Stream `url` to `destination`, reporting (done_bytes, total_bytes)."""
    try:
        response = _open_url(url)
    except urllib.error.HTTPError as exc:
        raise FlasherError(f"Download failed with HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise FlasherError(f"Download failed: {exc.reason}") from exc

    with response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        progress(0, total)
        with open(destination, "wb") as handle:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                progress(done, total)

    if total and done != total:
        raise FlasherError(
            f"Download was truncated ({format_bytes(done)} of "
            f"{format_bytes(total)})."
        )


def sha256_file(path: str, cancel: threading.Event | None = None) -> str:
    """Stream a file through SHA-256, honouring cancellation."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = handle.read(4 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def gzip_uncompressed_size(path: str) -> int:
    """
    Read the ISIZE trailer of a gzip stream for an approximate progress total.

    The value is stored modulo 2**32, so images larger than 4 GiB report a
    wrapped number; callers treat an implausible value as "unknown".
    """
    try:
        size = os.path.getsize(path)
        if size < 18:
            return 0
        with open(path, "rb") as handle:
            handle.seek(-4, os.SEEK_END)
            trailer = handle.read(4)
        return struct.unpack("<I", trailer)[0]
    except OSError:
        return 0


# ===========================================================================
#  Device enumeration -- every platform, OS drive always excluded
# ===========================================================================
def _device(path, raw, label, size, removable, protected, detail="") -> dict:
    return {
        "path": path,       # addressable device node / handle
        "raw": raw,         # raw (unbuffered) variant used for flashing
        "label": label,     # text shown in the dropdown
        "size": int(size or 0),
        "removable": bool(removable),
        "protected": bool(protected),
        "detail": detail,
        "index": None,      # Windows physical drive number
    }


def list_devices() -> list[dict]:
    """Return the physical drives of this machine, OS drive marked protected."""
    if IS_WINDOWS:
        return _list_devices_windows()
    if IS_MACOS:
        return _list_devices_macos()
    return _list_devices_linux()


def revalidate_device(device: dict) -> dict:
    """
    Re-enumerate drives and confirm `device` is still the same disk.

    Guards against a card being swapped (and its node reused) between picking
    it and wiping it. Returns the fresh entry; raises FlasherError otherwise.
    """
    try:
        current = list_devices()
    except Exception as exc:
        raise FlasherError(f"Could not re-check the drives: {exc}") from exc
    fresh = next((d for d in current if d["path"] == device["path"]), None)
    if fresh is None:
        raise FlasherError(
            f"{device['path']} is no longer connected. Re-insert the card, "
            "press Refresh and select it again. Nothing was written."
        )
    same = (fresh["size"] == device["size"]
            and fresh.get("detail", "") == device.get("detail", "")
            and fresh.get("index") == device.get("index"))
    if not same:
        raise FlasherError(
            f"The drive at {device['path']} has changed since it was selected "
            f"(now: {fresh['label']}). Press Refresh and select the card "
            "again. Nothing was written."
        )
    if fresh["protected"]:
        raise FlasherError(
            f"{device['path']} now holds the system or mounted volumes and is "
            "protected. Nothing was written."
        )
    return fresh


def check_image_fits(image_size: int, device: dict) -> None:
    """Refuse before wiping when the image is known not to fit the drive."""
    if image_size and device.get("size") and image_size > device["size"]:
        raise FlasherError(
            f"The image needs {format_bytes(image_size)} but "
            f"{device['path']} holds only {format_bytes(device['size'])}. "
            "Use a larger card. Nothing was written."
        )


# -- Linux ------------------------------------------------------------------
SYS_CLASS_BLOCK = "/sys/class/block"
# udisks/desktop automounts live here; a card mounted there is unmounted and
# flashed. A disk with a mount anywhere else (/, /home, /mnt/data, swap ...)
# is treated as part of this computer and protected.
REMOVABLE_MOUNT_ROOTS = ("/media/", "/run/media/")


def _unescape_mount_field(field: str) -> str:
    """/proc/mounts and /proc/swaps octal-escape spaces etc. (\\040)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _linux_disks_of(node: str, _depth: int = 0) -> set[str]:
    """
    Whole-disk names (e.g. {"nvme0n1"}) backing a block device node.

    Resolves partitions to their disk and walks device-mapper/MD "slaves"
    (LUKS, LVM, RAID) down to every physical disk underneath.
    """
    # findmnt prints btrfs subvolumes as "/dev/sda2[/@]".
    node = re.sub(r"\[.*\]$", "", node.strip())
    if not node.startswith("/dev/") or _depth > 8:
        return set()
    name = os.path.basename(os.path.realpath(node))
    sys_dir = os.path.join(SYS_CLASS_BLOCK, name)
    if not os.path.exists(sys_dir):
        return set()
    try:
        slaves = os.listdir(os.path.join(sys_dir, "slaves"))
    except OSError:
        slaves = []
    if slaves:
        disks: set[str] = set()
        for slave in slaves:
            disks |= _linux_disks_of("/dev/" + slave, _depth + 1)
        return disks
    if os.path.exists(os.path.join(sys_dir, "partition")):
        return {os.path.basename(os.path.dirname(os.path.realpath(sys_dir)))}
    return {name}


def _linux_mounts() -> list[tuple[str, str]]:
    """(source device, mountpoint) for every /dev-backed mount and swap."""
    entries: list[tuple[str, str]] = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) >= 2 and fields[0].startswith("/dev/"):
                    entries.append((_unescape_mount_field(fields[0]),
                                    _unescape_mount_field(fields[1])))
    except OSError:
        pass
    try:
        with open("/proc/swaps", encoding="utf-8", errors="replace") as fh:
            for line in fh.readlines()[1:]:
                fields = line.split()
                if fields and fields[0].startswith("/dev/"):
                    entries.append((_unescape_mount_field(fields[0]), "[SWAP]"))
    except OSError:
        pass
    return entries


def _linux_root_disks() -> set[str]:
    """Every physical disk backing '/' (several for LVM/RAID spanning disks)."""
    disks: set[str] = set()
    for cmd in (["findmnt", "-no", "SOURCE", "/"],):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=DEVNULL).strip()
        except Exception:
            continue
        disks |= _linux_disks_of(out.split(" ")[0])
    for source, point in _linux_mounts():
        if point == "/":
            disks |= _linux_disks_of(source)
    return disks


def _linux_protected_disks() -> set[str]:
    """Disks that hold the OS or any mount outside the automount folders."""
    protected = _linux_root_disks()
    for source, point in _linux_mounts():
        if point == "[SWAP]" or not point.startswith(REMOVABLE_MOUNT_ROOTS):
            protected |= _linux_disks_of(source)
    return protected


def _list_devices_linux() -> list[dict]:
    devices: list[dict] = []
    system_disks = _linux_protected_disks()

    # Preferred path: lsblk gives us model names and mountpoints in one shot.
    try:
        out = subprocess.check_output(
            [
                "lsblk", "-d", "-b", "-J",
                "-o", "NAME,SIZE,MODEL,TRAN,RM,TYPE,MOUNTPOINTS",
            ],
            text=True,
            stderr=DEVNULL,
        )
        data = json.loads(out)
        for block in data.get("blockdevices", []):
            if (block.get("type") or "").lower() != "disk":
                continue
            name = block.get("name") or ""
            if not name or name.startswith(("loop", "ram", "zram", "dm-")):
                continue
            size = int(block.get("size") or 0)
            model = (block.get("model") or "").strip()
            tran = (block.get("tran") or "").strip().upper()
            removable = block.get("rm") is True or block.get("rm") == "1"
            protected = name in system_disks

            label = f"/dev/{name}"
            if size:
                label += f"  —  {format_bytes(size)}"
            if model:
                label += f"  ({model})"
            if removable or tran == "USB":
                label += "  [USB/SD]"
            if protected:
                label += "  ⚠ SYSTEM / MOUNTED"

            device = _device(
                path=f"/dev/{name}",
                raw=f"/dev/{name}",
                label=label,
                size=size,
                removable=removable or tran == "USB",
                protected=protected,
                detail=(f"{model} {tran}").strip(),
            )
            devices.append(device)
    except Exception:
        # Fallback: pure /sys/block probing (works on minimal systems).
        try:
            names = sorted(os.listdir("/sys/block"))
        except OSError:
            return []

        for name in names:
            if name.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
                continue
            base = os.path.join("/sys/block", name)
            try:
                with open(os.path.join(base, "size")) as fh:
                    size = int(fh.read().strip()) * 512
            except Exception:
                size = 0
            removable = False
            try:
                with open(os.path.join(base, "removable")) as fh:
                    removable = fh.read().strip() == "1"
            except Exception:
                pass
            model = ""
            try:
                with open(os.path.join(base, "device", "model")) as fh:
                    model = fh.read().strip()
            except Exception:
                pass
            protected = name in system_disks

            label = f"/dev/{name}"
            if size:
                label += f"  —  {format_bytes(size)}"
            if model:
                label += f"  ({model})"
            if removable:
                label += "  [Removable]"
            if protected:
                label += "  ⚠ SYSTEM"

            devices.append(
                _device(
                    path=f"/dev/{name}",
                    raw=f"/dev/{name}",
                    label=label,
                    size=size,
                    removable=removable,
                    protected=protected,
                    detail=model,
                )
            )

    # Removable / external first, protected last.
    devices.sort(key=lambda d: (d["protected"], not d["removable"], -(d["size"] or 0)))
    return devices


# -- macOS ------------------------------------------------------------------
def _list_devices_macos() -> list[dict]:
    devices: list[dict] = []
    try:
        raw = subprocess.check_output(
            ["diskutil", "list", "-plist"], stderr=DEVNULL
        )
        listing = plistlib.loads(raw)
    except Exception:
        return []

    for disk in listing.get("WholeDisks", []):
        try:
            info_raw = subprocess.check_output(
                ["diskutil", "info", "-plist", disk], stderr=DEVNULL
            )
            info = plistlib.loads(info_raw)
        except Exception:
            continue

        node = info.get("DeviceNode") or f"/dev/{disk}"
        size = int(info.get("Size") or info.get("TotalSize") or 0)
        removable = bool(info.get("RemovableMedia") or info.get("Removable"))
        internal = bool(info.get("Internal"))
        mounted = bool(info.get("Mounted"))
        name = info.get("MediaName") or info.get("DeviceNode") or disk

        # Never allow the internal boot disk.
        protected = internal and not removable

        label = f"{node}  —  {format_bytes(size)}"
        if name:
            label += f"  ({name})"
        if removable:
            label += "  [Removable]"
        if mounted:
            label += "  [mounted]"
        if protected:
            label += "  ⚠ INTERNAL / SYSTEM"

        devices.append(
            _device(
                path=node,
                raw=node.replace("/dev/disk", "/dev/rdisk"),
                label=label,
                size=size,
                removable=removable,
                protected=protected,
                detail=str(name),
            )
        )

    devices.sort(key=lambda d: (d["protected"], not d["removable"], -(d["size"] or 0)))
    return devices


# -- Windows ----------------------------------------------------------------
def _windows_system_disk_number() -> int | None:
    """Disk number that currently holds the running Windows installation."""
    script = (
        "$d = Get-Partition -DriveLetter ($env:SystemDrive[0]) -ErrorAction Stop;"
        "$d.DiskNumber"
    )
    try:
        out = _powershell(script, timeout=30).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:
        return 0  # Conservative fallback: disk 0 is almost always the OS disk.


def _list_devices_windows() -> list[dict]:
    devices: list[dict] = []
    system_disk = _windows_system_disk_number()

    script = (
        "Get-CimInstance Win32_DiskDrive | "
        "Select-Object Index,Model,Size,InterfaceType,MediaType | "
        "ConvertTo-Json -Compress"
    )
    records: list[dict] = []
    try:
        out = _powershell(script, timeout=45).stdout.strip()
        if out:
            parsed = json.loads(out)
            records = parsed if isinstance(parsed, list) else [parsed]
    except Exception:
        records = []

    if not records:
        # Blind fallback: probe the 16 conventional physical drive handles.
        for index in range(16):
            device = _device(
                path=rf"\\.\PhysicalDrive{index}",
                raw=rf"\\.\PhysicalDrive{index}",
                label=rf"\\.\PhysicalDrive{index}  (device handle)",
                size=0,
                removable=False,
                protected=(index == system_disk),
            )
            device["index"] = index
            devices.append(device)
        return devices

    for rec in records:
        index = int(rec.get("Index") or 0)
        size = int(rec.get("Size") or 0)
        model = (rec.get("Model") or "").strip()
        interface = (rec.get("InterfaceType") or "").strip()
        media = (rec.get("MediaType") or "").strip()
        removable = ("removable" in media.lower()) or (interface.upper() == "USB")
        protected = index == system_disk

        label = rf"\\.\PhysicalDrive{index}"
        if size:
            label += f"  —  {format_bytes(size)}"
        if model:
            label += f"  ({model})"
        if interface:
            label += f"  [{interface}]"
        if protected:
            label += "  ⚠ SYSTEM DISK"

        device = _device(
            path=rf"\\.\PhysicalDrive{index}",
            raw=rf"\\.\PhysicalDrive{index}",
            label=label,
            size=size,
            removable=removable,
            protected=protected,
            detail=f"{model} {interface}".strip(),
        )
        device["index"] = index
        devices.append(device)

    devices.sort(key=lambda d: (d["protected"], not d["removable"], -(d["size"] or 0)))
    return devices


# ===========================================================================
#  Secure wipe (partition table / first sectors)
# ===========================================================================
def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run `cmd` with the active sudo prefix, raising FlasherError on failure."""
    full = list(SUDO_PREFIX) + list(cmd)
    try:
        return subprocess.run(full, check=True, **kwargs)
    except FileNotFoundError as exc:
        raise FlasherError(f"Required tool not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode(errors="replace").strip()
        raise FlasherError(
            f"Command failed ({cmd[0]}, exit {exc.returncode})."
            + (f"\n{stderr}" if stderr else "")
        ) from exc


def wipe_device(device: dict, log) -> None:
    """Erase the partition table and leading sectors of `device`."""
    if IS_WINDOWS:
        _wipe_windows(device, log)
    elif IS_MACOS:
        _wipe_macos(device, log)
    else:
        _wipe_linux(device, log)


def _wipe_linux(device: dict, log) -> None:
    path = device["path"]
    log(f"Unmounting any volumes on {path} ...")
    # Every mount and swap on the card must be released before we clobber it;
    # writing under a live filesystem corrupts the result, so fail closed.
    disk = os.path.basename(os.path.realpath(path))
    busy = [(src, point) for src, point in _linux_mounts()
            if disk in _linux_disks_of(src)]
    # Deepest mountpoints first so nested mounts come off before parents.
    for src, point in sorted(busy, key=lambda e: e[1].count("/"), reverse=True):
        if point == "[SWAP]":
            log(f"  swapoff {src}")
            cmd = ["swapoff", src]
        else:
            log(f"  unmounting {point}")
            cmd = ["umount", point]
        try:
            _run(cmd, stdout=DEVNULL, stderr=subprocess.PIPE)
        except FlasherError as exc:
            raise FlasherError(
                f"Could not release {point} on {path}:\n{exc}\n\n"
                "Close any file manager windows or programs using the card, "
                "then try again. The card was NOT modified."
            ) from exc

    log(f"Wiping filesystem signatures on {path} (wipefs) ...")
    try:
        _run(["wipefs", "-a", path],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FlasherError as exc:
        # wipefs is optional -- zeroing below still destroys the partition table.
        log(f"  wipefs skipped: {exc}")

    log(f"Zeroing the first {format_bytes(ZERO_WIPE_BYTES)} of {path} ...")
    _run([
        "dd", "if=/dev/zero", f"of={path}",
        "bs=1M", f"count={ZERO_WIPE_BYTES // (1024 * 1024)}",
        "conv=fsync", "status=none",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _run(["sync"], stdout=DEVNULL, stderr=DEVNULL)
    log("  Target wiped.")


def _wipe_macos(device: dict, log) -> None:
    path = device["path"]
    log(f"Unmounting {path} ...")
    _run(["diskutil", "unmountDisk", "force", path],
         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    log(f"Erasing partition map on {path} (diskutil) ...")
    try:
        _run(["diskutil", "eraseDisk", "free", "%noformat%", path],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FlasherError as exc:
        # Erase can fail on some media/systems; the raw streaming write below
        # still overwrites the whole partition table, so this is non-fatal.
        log(f"  eraseDisk skipped: {exc}")
    log("  Target wiped.")


def _wipe_windows(device: dict, log) -> None:
    index = device.get("index")
    if index is None:
        raise FlasherError("Could not determine the physical drive number to wipe.")

    log(f"Wiping PhysicalDrive{index} with diskpart (clean) ...")
    script = (
        f"select disk {index}\n"
        "attributes disk clear readonly\n"
        "clean\n"
        "exit\n"
    )
    handle, path = tempfile.mkstemp(suffix=".txt", prefix="banana_diskpart_")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(script)
        try:
            # diskpart prints in the console's OEM code page.
            proc = _hidden_run(
                ["diskpart", "/s", path],
                timeout=300,
                encoding="oem" if IS_WINDOWS else "utf-8",
                merge_stderr=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise FlasherError("diskpart timed out while cleaning the disk.") from exc
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 or "DiskPart has encountered an error" in output:
            raise FlasherError(f"diskpart failed:\n{output}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    log("  Target wiped (diskpart clean completed).")


# -- Windows: stop the OS from grabbing the card mid-write ------------------
# Part-way through a raw write the disk's partition table changes. Windows'
# mount manager notices the "new" volumes, mounts them (they are unrecognised
# Linux filesystems), pops up "You need to format the disk ...", and -- worst
# of all -- keeps the volume open. Every subsequent raw write that crosses the
# mounted volume then fails with ERROR_ACCESS_DENIED (5), which is exactly the
# crash reported here. Suspending automatic mounting and the shell's hardware
# detection service for the duration of the destructive step stops Windows
# from touching the card until we are finished.
SHELL_HW_SERVICE = "ShellHWDetection"


def _powershell(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet (hidden window), returning UTF-8 decoded stdout."""
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + script
    return _hidden_run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive",
         "-Command", script],
        timeout=timeout,
    )


def _shell_hw_detection_running() -> bool:
    try:
        proc = _powershell(
            f"(Get-Service -Name '{SHELL_HW_SERVICE}' -ErrorAction Stop).Status"
        )
        return proc.stdout.strip().lower() == "running"
    except Exception:
        return False


def _set_shell_hw_detection(running: bool) -> bool:
    verb = "Start-Service" if running else "Stop-Service -Force"
    try:
        proc = _powershell(f"{verb} -Name '{SHELL_HW_SERVICE}' -ErrorAction Stop")
        return proc.returncode == 0
    except Exception:
        return False


def _automount_enabled() -> bool:
    """True when Windows is configured to automatically mount new volumes."""
    try:
        proc = _powershell(
            "(Get-ItemProperty -Path "
            "'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\MountMgr' "
            "-Name NoAutoMount -ErrorAction Stop).NoAutoMount"
        )
        return proc.stdout.strip() != "1"
    except Exception:
        # Unknown -- assume the Windows default (enabled) so we restore it.
        return True


def _set_automount(enabled: bool) -> bool:
    """Enable ('/E') or disable ('/N') automatic mounting of new volumes."""
    try:
        proc = _hidden_run(["mountvol", "/E" if enabled else "/N"], timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


class WindowsRawAccessGuard:
    """
    Temporarily stop Windows from mounting the target disk's volumes.

    Both changes are reverted on exit -- even when the flash fails or is
    cancelled -- so the host is left exactly as we found it.
    """

    def __init__(self, log):
        self._log = log
        self._stopped_service = False
        self._disabled_automount = False

    def __enter__(self) -> "WindowsRawAccessGuard":
        if not IS_WINDOWS:
            return self

        if _automount_enabled():
            if _set_automount(False):
                self._disabled_automount = True
                self._log("  (paused Windows automatic volume mounting)")
            else:
                self._log("  (could not pause automatic volume mounting)")
        else:
            self._log("  (automatic volume mounting already disabled)")

        if _shell_hw_detection_running():
            if _set_shell_hw_detection(False):
                self._stopped_service = True
                self._log("  (paused Shell Hardware Detection)")
            else:
                self._log("  (could not pause Shell Hardware Detection)")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._stopped_service:
            if not _set_shell_hw_detection(True):
                self._log("  WARNING: could not restart Shell Hardware "
                          "Detection; it will resume on reboot.")
        if self._disabled_automount:
            if not _set_automount(True):
                self._log("  WARNING: could not re-enable automatic volume "
                          "mounting; it resets on reboot.")
        return False


# ===========================================================================
#  Streaming flasher -- decompress on the fly straight to the block device
# ===========================================================================
def flash_device(
    image_path: str,
    device: dict,
    total_uncompressed: int,
    progress,
    cancel: threading.Event,
    log,
) -> int:
    """
    Decompress `image_path` and stream it to `device`'s raw node.

    Returns the number of bytes written.
    """
    if IS_WINDOWS:
        return _flash_windows(image_path, device, total_uncompressed, progress, cancel, log)
    return _flash_posix(image_path, device, total_uncompressed, progress, cancel, log)


def _stream_with_progress(image_path, total, write_chunk, progress, cancel, start_clock):
    """Shared gzip -> callback loop with cancellation and throttled reporting."""
    done = 0
    last_report = 0.0
    with gzip.open(image_path, "rb") as gz:
        while True:
            if cancel.is_set():
                raise Cancelled()
            chunk = gz.read(CHUNK_SIZE)
            if not chunk:
                break
            write_chunk(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last_report >= 0.1:
                last_report = now
                progress(done, total, now - start_clock)
    return done


def _flash_posix(image_path, device, total, progress, cancel, log) -> int:
    """Linux/macOS: pipe the decompressed image into `sudo dd` on a raw device."""
    target = device["raw"] or device["path"]
    log(f"Streaming image to {target} via dd (this can take a while) ...")

    cmd = list(SUDO_PREFIX) + [
        "dd",
        f"of={target}",
        # BSD dd (macOS) wants lowercase 'm'; GNU dd (Linux) wants 'M'.
        "bs=4m" if IS_MACOS else "bs=4M",
        "conv=fsync",         # flush to media before returning
    ]
    if shutil.which("dd") is None:
        raise FlasherError("dd is not available on this system.")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    start_clock = time.monotonic()
    written = 0

    def write_chunk(chunk: bytes) -> None:
        nonlocal written
        if proc.stdin is None:
            raise FlasherError("Internal error: dd stdin is unavailable.")
        try:
            proc.stdin.write(chunk)
        except (BrokenPipeError, OSError) as exc:
            raise FlasherError(_dd_failure(proc)) from exc
        written += len(chunk)

    try:
        written = _stream_with_progress(
            image_path, total, write_chunk, progress, cancel, start_clock
        )
        if proc.stdin is not None:
            proc.stdin.close()
        # conv=fsync can take many minutes on a slow card; keep waiting but
        # stay responsive to Cancel.
        while True:
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set():
                    raise Cancelled() from None
    except Cancelled:
        _kill(proc)
        raise
    except BrokenPipeError as exc:
        raise FlasherError(_dd_failure(proc)) from exc

    if proc.returncode != 0:
        raise FlasherError(_dd_failure(proc))

    with contextlib.suppress(Exception):
        # Extra safety flush on Linux; harmless if `sync` is unavailable.
        subprocess.run(list(SUDO_PREFIX) + ["sync"],
                       stdout=DEVNULL, stderr=DEVNULL, timeout=600)

    if IS_MACOS:
        log("Ejecting the disk ...")
        try:
            _run(["diskutil", "eject", device["path"]],
                 stdout=DEVNULL, stderr=DEVNULL)
        except FlasherError as exc:
            log(f"  eject warning: {exc}")

    return written


def _dd_failure(proc: subprocess.Popen) -> str:
    detail = ""
    try:
        if proc.stderr is not None:
            detail = proc.stderr.read().decode(errors="replace").strip()
    except Exception:
        pass
    code = proc.poll()
    return f"dd failed (exit {code})." + (f"\n{detail}" if detail else "")


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except Exception:
        pass
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


# -- Windows: lock + dismount the disk's volumes ----------------------------
# A raw write to \\.\PhysicalDriveN fails with ERROR_ACCESS_DENIED (5) as soon
# as it crosses the extents of a *mounted* volume -- typically the first
# partition of a card that still carries a FAT/exFAT filesystem, which is why
# the failure tends to appear part-way through (e.g. "at 19.0 MiB"). Locking
# and force-dismounting every volume on the disk before we start, and keeping
# those locks held until the write finishes, takes the file-system driver out
# of the picture so the raw stream is never refused.
def _lock_and_dismount_volumes(kernel32, disk_number: int, log,
                               quiet: bool = False,
                               failed: list | None = None):
    """
    Open every volume that lives on `disk_number`, lock + force-dismount it,
    and return the still-open volume handles (caller must release them).

    A volume that refuses both lock and dismount (e.g. in use) is logged,
    skipped, and its name appended to `failed` when a list is given.
    """
    import ctypes
    from ctypes import wintypes

    IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class DISK_EXTENT(ctypes.Structure):
        _fields_ = [
            ("DiskNumber", wintypes.DWORD),
            ("StartingOffset", ctypes.c_longlong),
            ("ExtentLength", ctypes.c_longlong),
        ]

    class VOLUME_DISK_EXTENTS(ctypes.Structure):
        _fields_ = [
            ("NumberOfDiskExtents", wintypes.DWORD),
            ("Extents", DISK_EXTENT * 32),
        ]

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.FindFirstVolumeW.restype = wintypes.HANDLE
    kernel32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    kernel32.FindNextVolumeW.restype = wintypes.BOOL
    kernel32.FindNextVolumeW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
    ]
    kernel32.FindVolumeClose.argtypes = [wintypes.HANDLE]

    locked: list = []
    buffer = ctypes.create_unicode_buffer(260)
    find = kernel32.FindFirstVolumeW(buffer, len(buffer))
    if find == INVALID_HANDLE_VALUE or find is None:
        if not quiet:
            log("  (could not enumerate volumes: Windows error "
                f"{ctypes.get_last_error()})")
        return locked

    try:
        while True:
            name = buffer.value
            # FindFirstVolumeW names end in "\\"; with it, CreateFileW opens the
            # volume's root directory instead of the volume device.
            volume = kernel32.CreateFileW(
                name.rstrip("\\"),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if volume != INVALID_HANDLE_VALUE and volume is not None:
                extents = VOLUME_DISK_EXTENTS()
                returned = wintypes.DWORD(0)
                queried = kernel32.DeviceIoControl(
                    volume, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                    None, 0, ctypes.byref(extents), ctypes.sizeof(extents),
                    ctypes.byref(returned), None,
                )
                on_disk = queried and extents.NumberOfDiskExtents and any(
                    extents.Extents[i].DiskNumber == disk_number
                    for i in range(min(extents.NumberOfDiskExtents,
                                       len(extents.Extents)))
                )
                if on_disk and _lock_volume(kernel32, volume, name, log):
                    locked.append(volume)   # keep the handle open (locked)
                else:
                    if on_disk and failed is not None:
                        failed.append(name)
                    kernel32.CloseHandle(volume)

            if not kernel32.FindNextVolumeW(find, buffer, len(buffer)):
                break
    finally:
        kernel32.FindVolumeClose(find)

    if not quiet:
        if locked:
            log(f"  Locked and dismounted {len(locked)} volume(s) on the "
                "target disk.")
        else:
            log("  No mounted volumes on the target disk needed locking.")
    return locked


def _lock_volume(kernel32, volume, name: str, log) -> bool:
    """Lock then force-dismount one volume handle; True if it is now held."""
    import ctypes
    from ctypes import wintypes

    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_DISMOUNT_VOLUME = 0x00090020

    returned = wintypes.DWORD(0)
    locked = kernel32.DeviceIoControl(
        volume, FSCTL_LOCK_VOLUME, None, 0, None, 0,
        ctypes.byref(returned), None,
    )
    dismounted = kernel32.DeviceIoControl(
        volume, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
        ctypes.byref(returned), None,
    )
    if locked or dismounted:
        log(f"    locked {name}")
        return True
    log(f"    (could not lock {name}: Windows error "
        f"{ctypes.get_last_error()})")
    return False


def _unlock_and_close_volumes(kernel32, handles) -> None:
    """Release the locks taken by `_lock_and_dismount_volumes`."""
    import ctypes
    from ctypes import wintypes

    FSCTL_UNLOCK_VOLUME = 0x0009001C
    for volume in handles:
        returned = wintypes.DWORD(0)
        kernel32.DeviceIoControl(
            volume, FSCTL_UNLOCK_VOLUME, None, 0, None, 0,
            ctypes.byref(returned), None,
        )
        kernel32.CloseHandle(volume)


# -- Windows raw device writing --------------------------------------------
def _flash_windows(image_path, device, total, progress, cancel, log) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    IOCTL_DISK_UPDATE_PROPERTIES = 0x00070100
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    # Win32 error codes that are worth retrying rather than aborting on.
    ERROR_ACCESS_DENIED = 5
    ERROR_NOT_READY = 21
    ERROR_SEM_TIMEOUT = 121

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    disk_number = device.get("index")
    target = device["path"]
    log(f"Opening raw device {target} for writing ...")

    handle = kernel32.CreateFileW(
        target,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle == INVALID_HANDLE_VALUE or handle is None:
        err = ctypes.get_last_error()
        raise FlasherError(
            f"Could not open {target} (Windows error {err}). "
            "Make sure the app is running as Administrator and the card is not "
            "in use by another program."
        )

    # Volumes we lock stay open (and locked) for the whole write and are
    # released in the finally block below.
    locked_volumes: list = []

    try:
        # Lock + force-dismount every mounted volume on this disk before the
        # first raw write. Windows refuses raw writes that cross a mounted
        # volume's extents with ERROR_ACCESS_DENIED (5); holding these locks
        # removes that refusal.
        if disk_number is None:
            log("  (physical drive number unknown; skipping volume lock)")
        else:
            unlockable: list = []
            locked_volumes = _lock_and_dismount_volumes(
                kernel32, disk_number, log, failed=unlockable,
            )
            if unlockable:
                # Raw writes across a live volume are refused (or worse,
                # raced by the file system); stop before writing anything.
                raise FlasherError(
                    "A volume on the card is in use and could not be locked:\n"
                    + "\n".join(f"  {name}" for name in unlockable)
                    + "\n\nClose any Explorer windows or programs using the "
                    "card, then try again."
                )

        zero = ctypes.c_longlong(0)
        kernel32.SetFilePointerEx(handle, zero, None, 0)

        start_clock = time.monotonic()

        # Position every write explicitly and tolerate short writes and
        # transient failures. If the OS momentarily grabs a volume we re-seek
        # to the exact offset and retry instead of desyncing (or aborting) the
        # whole flash.
        offset = 0
        retries_remaining = 8

        def write_chunk(chunk: bytes) -> None:
            nonlocal offset, retries_remaining
            ragged = len(chunk) % WIN_SECTOR_ALIGN
            if ragged:
                # Only the image's final chunk can be ragged; zero-pad it.
                chunk = bytes(chunk) + b"\0" * (WIN_SECTOR_ALIGN - ragged)
            view = memoryview(chunk)
            while view:
                position = ctypes.c_longlong(offset)
                kernel32.SetFilePointerEx(handle, position, None, 0)
                buffer = ctypes.create_string_buffer(bytes(view), len(view))
                written_count = wintypes.DWORD(0)
                ok = kernel32.WriteFile(
                    handle, buffer, len(view),
                    ctypes.byref(written_count), None,
                )
                if ok and written_count.value:
                    offset += written_count.value
                    view = view[written_count.value:]
                    retries_remaining = 8
                    continue

                err = ctypes.get_last_error()
                if err in (ERROR_ACCESS_DENIED, ERROR_NOT_READY,
                           ERROR_SEM_TIMEOUT) and retries_remaining:
                    retries_remaining -= 1
                    if err == ERROR_ACCESS_DENIED and disk_number is not None:
                        # A volume may have (re)mounted under us mid-write;
                        # grab any that appeared before retrying.
                        locked_volumes.extend(
                            _lock_and_dismount_volumes(
                                kernel32, disk_number, log, quiet=True,
                            )
                        )
                    log(f"  Retrying write at {format_bytes(offset)} "
                        f"(Windows error {err}) ...")
                    time.sleep(0.5)
                    continue

                raise FlasherError(
                    f"Write to {target} failed (Windows error {err}) at "
                    f"offset {format_bytes(offset)}. "
                    "The card may have been removed or is failing."
                )

        written = _stream_with_progress(
            image_path, total, write_chunk, progress, cancel, start_clock
        )

        log("Flushing write buffers to the card ...")
        if not kernel32.FlushFileBuffers(handle):
            log("  (FlushFileBuffers reported an issue, continuing)")

        returned = wintypes.DWORD(0)
        kernel32.DeviceIoControl(
            handle, IOCTL_DISK_UPDATE_PROPERTIES, None, 0, None, 0,
            ctypes.byref(returned), None,
        )
    finally:
        _unlock_and_close_volumes(kernel32, locked_volumes)
        kernel32.CloseHandle(handle)

    return written


# ===========================================================================
#  Windows on-demand elevation (privileged helper process)
# ===========================================================================
# Rather than forcing the whole Tk application to run as Administrator, the GUI
# stays a normal unprivileged process. Only when the user actually starts a
# flash do we spawn a short-lived, GUI-less helper through the UAC "runas"
# verb. The helper performs the privileged steps (raw handle, volume
# dismounting, image writing) and streams progress back to the GUI over a
# newline-delimited JSON status file. The download and checksum verification
# stay in the unprivileged parent; the helper only receives the already
# downloaded image path and the target drive index.
PRIVILEGED_WRITE_FLAG = "--privileged-write"
ERROR_CANCELLED = 1223  # UAC prompt dismissed
# How long Cancel waits for the helper to stop by itself (restoring automount
# and Shell Hardware Detection) before it is forcibly terminated.
HELPER_CANCEL_GRACE_S = 20.0
# How long closing the window waits for a cancelled flash to clean up.
CLOSE_WAIT_S = 30.0


def _request_cancel(cancel_path: str | None) -> None:
    """Ask the elevated helper to stop by creating its cancel marker file."""
    if not cancel_path:
        return
    try:
        with open(cancel_path, "w", encoding="utf-8") as handle:
            handle.write("cancel\n")
    except OSError:
        pass


def _watch_cancel_file(cancel_path: str, cancel_event: threading.Event,
                       interval: float = 0.25) -> threading.Thread:
    """Set `cancel_event` once `cancel_path` appears (helper side)."""
    def watch() -> None:
        while not cancel_event.is_set():
            if os.path.exists(cancel_path):
                cancel_event.set()
                return
            time.sleep(interval)

    thread = threading.Thread(target=watch, name="cancel-watch", daemon=True)
    thread.start()
    return thread


def _stderr(message: str) -> None:
    """Best-effort stderr write (a windowed PyInstaller build has no console)."""
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr, flush=True)
        except Exception:
            pass


def _append_json_line(path: str, payload: dict) -> None:
    """Append one JSON event to `path`, flushing so the reader sees it soon."""
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
            handle.flush()
    except OSError:
        pass


def _read_status_updates(path: str, offset: int, on_event) -> int:
    """
    Read complete newline-delimited JSON events appended to `path`.

    Returns the new byte offset. An incomplete trailing line is left for the
    next poll, so the reader never sees a half-written event.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return offset
    if not data:
        return offset
    consumed = data.rfind(b"\n")
    if consumed == -1:
        return offset
    for raw in data[:consumed].split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            on_event(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            continue
    return offset + consumed + 1


def _dispatch_worker_event(event: dict, progress, status_cb, log) -> None:
    """Translate one helper event into the parent's callbacks."""
    kind = event.get("kind")
    if kind == "progress":
        progress(int(event.get("done") or 0),
                 int(event.get("total") or 0),
                 float(event.get("elapsed") or 0.0))
    elif kind == "status":
        status_cb(str(event.get("message") or ""))
    elif kind == "log":
        log(str(event.get("message") or ""))


def _read_worker_result(path: str | None) -> dict:
    """Read the helper's final result file, or a synthetic failure."""
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                result = json.load(handle)
            if isinstance(result, dict):
                return result
        except (OSError, ValueError):
            pass
    return {"ok": False,
            "error": "The elevated helper exited without reporting a result."}


def _privileged_write_main(request_path: str) -> int:
    """
    Entry point for the elevated helper process (no GUI).

    Reads a JSON request, performs the wipe + raw write, reports progress via
    the status file, and always writes a result file so the parent can tell
    success, failure and refusal apart.
    """
    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except Exception as exc:  # pragma: no cover - defensive
        _stderr(f"privileged worker: could not read request: {exc}")
        return 2

    status_path = request.get("status_file")
    log_path = request.get("log_file")
    result_path = request.get("result_file")
    cancel_event = threading.Event()
    cancel_path = request.get("cancel_file")
    if cancel_path:
        _watch_cancel_file(cancel_path, cancel_event)

    def emit(payload: dict) -> None:
        if status_path:
            _append_json_line(status_path, payload)

    def log(message: str) -> None:
        message = message.rstrip()
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(message + "\n")
            except OSError:
                pass
        emit({"kind": "log", "message": message})
        if sys.stdout is not None:
            try:
                print(message, flush=True)
            except Exception:
                pass

    def finish(ok: bool, **fields) -> int:
        if result_path:
            try:
                with open(result_path, "w", encoding="utf-8") as handle:
                    json.dump({"ok": ok, **fields}, handle)
            except OSError:
                pass
        return 0 if ok else 1

    try:
        if not is_elevated():
            log("Refusing to run: the helper process is not elevated.")
            return finish(False, error="The helper process is not elevated.")

        image_path = request["image"]
        device = request["device"]
        total = int(request.get("total") or 0)

        if device.get("protected"):
            log("Refusing to write to a protected/system disk.")
            return finish(False, error="Refusing to write to the system disk.")
        if not str(device.get("path", "")).startswith("\\\\.\\PhysicalDrive"):
            log(f"Refusing to write to an unexpected target: {device.get('path')}")
            return finish(False, error="Refusing to write to that target.")
        if not os.path.isfile(image_path):
            log(f"Image file is missing: {image_path}")
            return finish(False, error="The downloaded image file is missing.")

        log(f"Elevated helper started (PhysicalDrive{device.get('index')}).")
        emit({"kind": "status", "message": "Wiping target drive ..."})
        with WindowsRawAccessGuard(log):
            wipe_device(device, log)
            if cancel_event.is_set():
                raise Cancelled()
            emit({"kind": "status", "message": "Flashing image ..."})
            written = flash_device(
                image_path, device, total,
                lambda done, tot, elapsed: emit({
                    "kind": "progress", "done": done,
                    "total": tot, "elapsed": elapsed,
                }),
                cancel_event, log,
            )
        emit({"kind": "done", "written": written})
        return finish(True, written=written)
    except Cancelled:
        log("Cancelled.")
        return finish(False, error="Cancelled.", cancelled=True)
    except FlasherError as exc:
        log(f"ERROR: {exc}")
        return finish(False, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        import traceback
        log("UNEXPECTED ERROR:\n" + traceback.format_exc())
        return finish(False, error=f"Unexpected error: {exc}")


def _share_workdir_with_admins(path: str, log) -> None:
    """
    Make the hand-off folder usable by both sides of the UAC boundary.

    Python 3.13+ creates mkdtemp() folders with an owner-only ACL. UAC may
    elevate as a *different* admin account (over-the-shoulder credentials),
    whose files the GUI user then cannot read back. Grant this user and the
    Administrators group inheritable access so status/result files written
    by the helper stay readable. Best-effort; same-account UAC needs none.
    """
    if not IS_WINDOWS:
        return
    grants = ["*S-1-5-32-544:(OI)(CI)F"]          # BUILTIN\\Administrators
    try:
        out = _hidden_run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout
        sid = out.strip().split(",")[-1].strip().strip('"')
        if sid.startswith("S-1-"):
            grants.append(f"*{sid}:(OI)(CI)F")
    except Exception:
        pass
    cmd = ["icacls", path]
    for grant in grants:
        cmd += ["/grant", grant]
    cmd += ["/T", "/Q"]
    try:
        if _hidden_run(cmd).returncode != 0:
            log("  (could not adjust temp folder permissions)")
    except Exception as exc:
        log(f"  (could not adjust temp folder permissions: {exc})")


def _spawn_elevated_write(request_path: str, status_path: str,
                          result_path: str, progress, status_cb, log,
                          cancel: threading.Event,
                          cancel_path: str | None = None) -> int:
    """
    Launch the privileged helper via UAC and relay its progress.

    Returns the number of bytes written. Raises FlasherError when the helper
    fails (including a dismissed UAC prompt) and Cancelled when the user
    cancels mid-write.
    """
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SW_HIDE = 0
    WAIT_OBJECT_0 = 0x00000000

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    params = [PRIVILEGED_WRITE_FLAG, request_path]
    if not getattr(sys, "frozen", False):
        # Running from source: relaunch the interpreter on this same script.
        params.insert(0, os.path.abspath(__file__))

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline(params)
    info.lpDirectory = None
    info.nShow = SW_HIDE

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise FlasherError(
                "Administrator rights are required to write the card, but the "
                "UAC prompt was dismissed. Nothing was written."
            )
        raise FlasherError(
            f"Could not start the elevated helper (Windows error {err})."
        )

    process = info.hProcess
    offset = 0
    cancel_deadline: float | None = None
    try:
        while True:
            if cancel.is_set() and cancel_deadline is None:
                if not cancel_path:
                    # No channel to ask it to stop; killing is all we have.
                    if not kernel32.TerminateProcess(process, 1):
                        log("  (could not terminate the elevated helper; it "
                            "will stop on its own)")
                    raise Cancelled()
                # Ask the helper to stop cleanly. It is never killed: only it
                # can restore automount and Shell Hardware Detection (killing
                # it would leave Windows with automount off, persistently).
                _request_cancel(cancel_path)
                cancel_deadline = time.monotonic() + HELPER_CANCEL_GRACE_S
            if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                log("  Still waiting for the elevated helper to finish its "
                    "current write and restore Windows settings ...")
                cancel_deadline = float("inf")
            offset = _read_status_updates(
                status_path, offset,
                lambda event: _dispatch_worker_event(
                    event, progress, status_cb, log),
            )
            if kernel32.WaitForSingleObject(process, 100) == WAIT_OBJECT_0:
                break
        # Drain anything the helper wrote just before exiting.
        _read_status_updates(
            status_path, offset,
            lambda event: _dispatch_worker_event(
                event, progress, status_cb, log),
        )
    finally:
        kernel32.CloseHandle(process)

    result = _read_worker_result(result_path)
    if result.get("cancelled") or (cancel.is_set() and not result.get("ok")):
        raise Cancelled()
    if not result.get("ok"):
        raise FlasherError(result.get("error") or "The elevated helper failed.")
    return int(result.get("written") or 0)


# ===========================================================================
#  Background worker -- runs the whole download/wipe/flash pipeline
# ===========================================================================
class FlashWorker(threading.Thread):
    """Executes the flashing pipeline and reports through a queue."""

    def __init__(self, app: "BananaFlasherApp", model: str, device: dict,
                 release: dict, keep_image: bool):
        super().__init__(name="flash-worker", daemon=True)
        self.app = app
        self.model = model
        self.asset_name = MODELS[model]
        self.device = device
        self.release = release
        self.keep_image = keep_image
        self.cancel_event = threading.Event()
        self._cancel_path: str | None = None
        self._emit = app.emit

    def cancel(self) -> None:
        """Stop the pipeline, including an elevated helper if one is running."""
        self.cancel_event.set()
        # Written synchronously so it still reaches the helper when the GUI is
        # closing and this daemon thread is about to be torn down.
        _request_cancel(self._cancel_path)

    # -- reporting helpers -------------------------------------------------
    def log(self, message: str) -> None:
        self._emit(("log", message))

    def status(self, message: str) -> None:
        self._emit(("status", message))

    def progress(self, fraction: float | None, detail: str = "") -> None:
        self._emit(("progress", fraction, detail))

    # -- pipeline ----------------------------------------------------------
    def run(self) -> None:
        tmpdir = None
        image_path = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="banana-flasher_")
            self._workdir = tmpdir
            image_path = os.path.join(tmpdir, self.asset_name)

            # ---- 1. Download -------------------------------------------
            self.status("Downloading image ...")
            repo = self.release.get("repo") or repo_for_model(self.model)
            self.log(
                f"Downloading {self.asset_name} ({self.release.get('tag')}) "
                f"from GitHub ({repo}) ..."
            )
            if self.release.get("size"):
                self.log(f"  Asset size: {format_bytes(self.release['size'])}")
            download_asset(
                self.release["url"], image_path, self._download_progress,
                self.cancel_event,
            )
            self.log(f"  Saved to {image_path}")

            # ---- 1b. Integrity check -----------------------------------
            expected = self.release.get("checksum")
            self.log("Verifying SHA-256 checksum ...")
            self.status("Verifying checksum ...")
            try:
                actual = sha256_file(image_path, self.cancel_event)
            except Cancelled:
                raise
            if expected:
                if actual != expected:
                    raise FlasherError(
                        "Checksum mismatch!\n\n"
                        f"  expected: {expected}\n"
                        f"  actual  : {actual}\n\n"
                        "The download is corrupt or has been tampered with. "
                        "The card was NOT modified."
                    )
                self.log("  Checksum OK.")
            else:
                self.log(f"  No published checksum; computed {actual[:16]}…")

            # ---- 2. Privileges -----------------------------------------
            # On Windows the write is elevated on demand (see _flash_elevated);
            # elsewhere sudo must already be cached.
            if not is_elevated() and not SUDO_PREFIX and not IS_WINDOWS:
                raise FlasherError("Elevated privileges are required to flash.")

            # ---- 3. Wipe + 4. Flash ------------------------------------
            self.status("Wiping target drive ...")
            self.progress(0.0, "Wiping partition table and boot area")

            total = gzip_uncompressed_size(image_path)
            compressed = os.path.getsize(image_path)
            if total and total < compressed:
                # The gzip ISIZE trailer is stored modulo 2**32, so an image
                # larger than 4 GiB reports a wrapped (too small) value. Fall
                # back to an indeterminate bar rather than lying to the user.
                self.log("  Uncompressed size exceeds 4 GiB; progress is "
                         "shown as an estimate.")
                total = 0

            # The download can take minutes: make sure the selected card is
            # still the one plugged in, and that the image fits on it.
            self.log("Re-checking the target drive ...")
            self.device = revalidate_device(self.device)
            check_image_fits(total, self.device)
            if self.cancel_event.is_set():
                raise Cancelled()

            if IS_WINDOWS and not is_elevated():
                written, elapsed = self._flash_elevated(image_path, total)
            else:
                # On Windows, hold off automatic volume mounting for the whole
                # destructive step so the freshly written Linux partitions
                # cannot be grabbed (and the "format this disk" prompt cannot
                # fire) while we are still streaming. The guard restores things
                # afterwards.
                with WindowsRawAccessGuard(self.log):
                    wipe_device(self.device, self.log)
                    if self.cancel_event.is_set():
                        raise Cancelled()
                    self.status("Flashing image ...")
                    self.log(
                        "Streaming decompressed image directly to "
                        f"{self.device['path']} ..."
                    )
                    start = time.monotonic()
                    written = flash_device(
                        image_path, self.device, total,
                        self._flash_progress, self.cancel_event, self.log,
                    )
                    elapsed = time.monotonic() - start

            rate = (written / elapsed) if elapsed > 0 else 0
            summary = (f"  Wrote {format_bytes(written)} in "
                       f"{format_duration(elapsed)}")
            if rate:
                summary += f"  ({format_bytes(rate)}/s)"
            self.log(summary + ".")

            self.progress(1.0, "Done")
            self.status("Finished.")
            self._emit(("done", {
                "model": self.model,
                "device": self.device,
                "written": written,
                "elapsed": elapsed,
                "tag": self.release.get("tag"),
            }))

        except Cancelled:
            self.log("Operation cancelled by the user.")
            self.status("Cancelled.")
            self._emit(("cancelled", None))
        except FlasherError as exc:
            self.log(f"ERROR: {exc}")
            self.status("Failed.")
            self._emit(("error", str(exc)))
        except Exception as exc:  # pragma: no cover - defensive
            import traceback
            self.log("UNEXPECTED ERROR:\n" + traceback.format_exc())
            self.status("Failed.")
            self._emit(("error", f"Unexpected error: {exc}"))
        finally:
            # Clean up the downloaded image unless the user asked to keep it.
            if tmpdir:
                if self.keep_image and image_path and os.path.exists(image_path):
                    self.log(f"Keeping the downloaded image at: {image_path}")
                else:
                    shutil.rmtree(tmpdir, ignore_errors=True)

    def _flash_elevated(self, image_path: str, total: int):
        """
        Wipe + write through an elevated helper process.

        The GUI (this process) is unprivileged; the helper is launched through
        the UAC "runas" verb, performs the destructive steps, and reports
        progress back through a status file. Returns (written, elapsed).
        """
        request_path = os.path.join(self._workdir, "request.json")
        status_path = os.path.join(self._workdir, "status.ndjson")
        result_path = os.path.join(self._workdir, "result.json")
        self._cancel_path = os.path.join(self._workdir, "cancel")
        request = {
            "image": image_path,
            "device": self.device,
            "total": total,
            "status_file": status_path,
            "result_file": result_path,
            "cancel_file": self._cancel_path,
            "log_file": os.path.join(self._workdir, "worker.log"),
        }
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle)
        _share_workdir_with_admins(self._workdir, self.log)

        self.status("Waiting for administrator approval ...")
        self.log("Administrator rights are not held; requesting elevation for "
                 "the write only (a UAC prompt will appear).")

        start = time.monotonic()
        written = _spawn_elevated_write(
            request_path, status_path, result_path,
            self._flash_progress, self.status, self.log, self.cancel_event,
            cancel_path=self._cancel_path,
        )
        return written, time.monotonic() - start

    # -- progress adapters -------------------------------------------------
    def _download_progress(self, done: int, total: int) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()
        fraction = (done / total) if total else None
        detail = (
            f"{format_bytes(done)} / {format_bytes(total)}"
            if total else format_bytes(done)
        )
        self.progress(fraction, f"Downloading — {detail}")

    def _flash_progress(self, done: int, total: int, elapsed: float) -> None:
        fraction = (done / total) if total else None
        speed = done / elapsed if elapsed else 0
        detail = f"{format_bytes(done)} written"
        if total:
            detail += f" / {format_bytes(total)}"
        if speed:
            detail += f"  @  {format_bytes(speed)}/s"
            if total and speed:
                detail += f"  —  {format_duration((total - done) / speed)} left"
        self.progress(fraction, f"Flashing — {detail}")


# ===========================================================================
#  GUI
# ===========================================================================
class BananaFlasherApp(tk.Tk):
    POLL_MS = 80

    def __init__(self):
        register_bundled_fonts()
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(780, 700)
        self.geometry("860x820")
        self.configure(background=BG)

        # Window / taskbar icon (bundled next to the source, or via PyInstaller).
        self._icon_image: tk.PhotoImage | None = None
        self._sprite_image: tk.PhotoImage | None = None
        self._apply_window_icon()

        self.queue: queue.Queue = queue.Queue()
        self.privileges = Privileges(self.log)
        self.devices: list[dict] = []
        self.release_cache: dict[str, dict] = {}
        self.worker: FlashWorker | None = None
        self._releases_lock = threading.Lock()
        self._closing = False
        self._indeterminate = False

        # Tk variables
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.device_var = tk.StringVar()
        self.keep_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready.")
        self.detail_var = tk.StringVar(value="")
        self.release_var = tk.StringVar(value="Querying latest release ...")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._build_style()
        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self.POLL_MS, self._drain_queue)

        # Initial work
        self.refresh_devices()
        self._query_release_async()
        self._report_elevation_status()

    # --------------------------------------------------------------- branding
    def _apply_window_icon(self) -> None:
        """Set the window/taskbar icon from the bundled asset when present."""
        ico = resource_path(os.path.join("docs", "banana.ico"))
        png = resource_path(os.path.join("docs", "banana.png"))
        try:
            if IS_WINDOWS and os.path.exists(ico):
                self.iconbitmap(ico)
            if os.path.exists(png):
                self._icon_image = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_image)
        except Exception:
            # A missing or unreadable icon must never stop the GUI from starting.
            self._icon_image = None

    # ------------------------------------------------------------------ style
    def _build_style(self) -> None:
        """Configure a single, cohesive ttk theme (built on 'clam')."""
        style = ttk.Style(self)
        for theme in ("clam", "alt", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        # --- typography -----------------------------------------------------
        ui_family = pick_family(
            self,
            (BODY_FONT, "SF Pro Text", "Helvetica Neue", "Segoe UI Variable",
             "Segoe UI", "Inter", "Ubuntu", "Cantarell", "Noto Sans",
             "DejaVu Sans", "Helvetica", "Arial"),
            "Helvetica",
        )
        mono_family = pick_family(
            self,
            ("JetBrains Mono", "Fira Code", "SF Mono", "Menlo", "Cascadia Code",
             "Consolas", "Ubuntu Mono", "DejaVu Sans Mono", "Courier New"),
            "Courier",
        )
        # Press Start 2P is a bitmap-style face: headings and buttons only, at
        # small sizes. Without it, fall back to the bold body face.
        px_family = pick_family(self, (PIXEL_FONT,), "")
        self._font_ui = ui_family
        self._font_mono = mono_family
        self._font_base = (ui_family, 10)
        if px_family:
            self._font_px = lambda size: (px_family, size)
        else:
            self._font_px = lambda size: (ui_family, size + 3, "bold")

        f_sub = (ui_family, 10)
        f_body = (ui_family, 10)
        f_status = (ui_family, 13, "bold")
        f_small = (ui_family, 9)

        # --- base surfaces --------------------------------------------------
        style.configure(".", background=BG, foreground=INK, font=f_body)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("TLabel", background=BG, foreground=INK, font=f_body)
        style.configure("Card.TLabel", background=SURFACE, foreground=INK, font=f_body)
        style.configure("Status.TLabel", background=SURFACE, foreground=INK,
                        font=f_status)
        style.configure("Detail.TLabel", background=SURFACE, foreground=MUTED,
                        font=f_small)
        style.configure("Hint.TLabel", background=SURFACE, foreground=FAINT,
                        font=f_small)
        style.configure("Footer.TLabel", background=BG, foreground=MUTED,
                        font=f_small)
        style.configure("TSeparator", background=BORDER)

        # --- header slab ----------------------------------------------------
        style.configure("Header.TFrame", background=HEADER_BG)
        style.configure("HeaderTitle.TLabel", background=HEADER_BG,
                        foreground=HEADER_TX, font=self._font_px(14))
        style.configure("HeaderSub.TLabel", background=HEADER_BG,
                        foreground=HEADER_SUB, font=f_sub)
        style.configure("HeaderPill.TLabel", background=HEADER_BG,
                        foreground=LABEL, font=self._font_px(8))

        # --- inputs ---------------------------------------------------------
        style.configure("TRadiobutton", background=SURFACE, foreground=INK,
                        font=f_body, padding=(2, 3), indicatorsize=15,
                        indicatorbackground=BG, indicatorforeground=ACCENT,
                        upperbordercolor=BORDER, lowerbordercolor=BORDER)
        style.map(
            "TRadiobutton",
            foreground=[("selected", ACCENT), ("active", INK)],
            background=[("active", SURFACE)],
            indicatorbackground=[("selected", BG), ("active", "#121212")],
            indicatorforeground=[("selected", ACCENT)],
        )
        style.configure("TCheckbutton", background=SURFACE, foreground=MUTED,
                        font=f_small, padding=(2, 4), indicatorsize=14,
                        indicatorbackground=BG, indicatorforeground=CYAN,
                        upperbordercolor=BORDER, lowerbordercolor=BORDER)
        style.map(
            "TCheckbutton",
            background=[("active", SURFACE)],
            foreground=[("active", INK)],
        )
        style.configure("TCombobox", fieldbackground=LOG_BG, background=LOG_BG,
                        foreground=INK, bordercolor=BORDER, arrowcolor=CYAN,
                        lightcolor=SURFACE, darkcolor=SURFACE,
                        padding=(10, 7), relief="flat")
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", LOG_BG), ("disabled", "#121212")],
            foreground=[("readonly", INK), ("disabled", FAINT)],
            background=[("readonly", LOG_BG), ("active", LOG_BG)],
            selectbackground=[("readonly", LOG_BG)],
            selectforeground=[("readonly", INK)],
            bordercolor=[("focus", CYAN), ("hover", MUTED)],
            arrowcolor=[("disabled", FAINT)],
        )
        style.configure("TEntry", fieldbackground=LOG_BG, foreground=INK,
                        insertcolor=INK,
                        bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE,
                        padding=(8, 7), relief="flat")
        style.map("TEntry", bordercolor=[("focus", CYAN)])

        # --- progress -------------------------------------------------------
        style.configure("Horizontal.TProgressbar", troughcolor=LOG_BG,
                        background=ACCENT, bordercolor=BORDER,
                        lightcolor=ACCENT, darkcolor=ACCENT, thickness=16)
        style.configure("Done.Horizontal.TProgressbar", troughcolor=LOG_BG,
                        background=CYAN, bordercolor=BORDER,
                        lightcolor=CYAN, darkcolor=CYAN, thickness=16)

        # --- dark scrollbar for the log ------------------------------------
        style.configure("Log.Vertical.TScrollbar", background="#242424",
                        troughcolor=LOG_BG, bordercolor=LOG_BG,
                        arrowcolor=LOG_DIM, lightcolor="#242424",
                        darkcolor="#242424", relief="flat")
        style.map("Log.Vertical.TScrollbar",
                  background=[("active", "#494949")])

        # Keep the combobox dropdown list in theme.
        self.option_add("*TCombobox*Listbox.background", LOG_BG)
        self.option_add("*TCombobox*Listbox.foreground", INK)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TX)
        self.option_add("*TCombobox*Listbox.font", f_body)

    # --- small reusable builders ------------------------------------------
    def _card(self, parent, step: int, title: str):
        """An outlined panel with a pink step badge and a yellow pixel heading."""
        outer = tk.Frame(parent, bg=SURFACE, bd=0,
                         highlightbackground=BORDER, highlightthickness=2)
        outer.pack(fill="x", pady=(0, 10))
        inner = tk.Frame(outer, bg=SURFACE, padx=18, pady=11)
        inner.pack(fill="both", expand=True)

        head = tk.Frame(inner, bg=SURFACE)
        head.pack(fill="x", anchor="w", pady=(0, 4))
        tk.Label(head, text=str(step), bg=ACCENT, fg=ACCENT_TX,
                 font=self._font_px(9), padx=7, pady=4).pack(side="left")
        tk.Label(head, text=title.upper(), bg=SURFACE, fg=LABEL,
                 font=self._font_px(9)).pack(side="left", padx=(12, 0))
        return inner

    def _hint(self, parent, text: str, wrap: int):
        tk.Label(parent, text=text, bg=SURFACE, fg=FAINT,
                 font=(self._font_ui, 9), justify="left", wraplength=wrap,
                 anchor="w").pack(fill="x", anchor="w", pady=(8, 0))

    def _button(self, parent, text: str, command, variant: str = "main",
                state: str = "normal", bg: str = SURFACE) -> PixelButton:
        return PixelButton(parent, text, command=command, variant=variant,
                           font=self._font_px(8), bg=bg, state=state)

    def _chrome_rule(self, parent, height: int = 7) -> None:
        """The site's chrome gradient, stacked as 1px bands under the header."""
        rule = tk.Canvas(parent, height=height, bg=BG, highlightthickness=0, bd=0)
        rule.pack(fill="x")
        last = len(CHROME_STOPS) - 1
        for row in range(height):
            colour = CHROME_STOPS[round(row * last / max(height - 1, 1))]
            rule.create_line(0, row, 4000, row, fill=colour)

    def _load_sprite(self) -> tk.PhotoImage | None:
        path = resource_path(os.path.join("docs", "banana-sprite.png"))
        try:
            return tk.PhotoImage(file=path).zoom(3)
        except Exception:
            return None

    # --------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        # ---- header slab ---------------------------------------------------
        header = tk.Frame(self, bg=HEADER_BG)
        header.pack(fill="x")
        header_inner = tk.Frame(header, bg=HEADER_BG, padx=24, pady=12)
        header_inner.pack(fill="x")

        self._sprite_image = self._load_sprite()
        if self._sprite_image is not None:
            tk.Label(header_inner, image=self._sprite_image, bg=HEADER_BG,
                     bd=0).pack(side="left", padx=(0, 16))

        left = tk.Frame(header_inner, bg=HEADER_BG)
        left.pack(side="left", anchor="w")
        tk.Label(left, text=APP_NAME.upper(), bg=HEADER_BG, fg=HEADER_TX,
                 font=self._font_px(14)).pack(anchor="w")
        tk.Label(left, text="Flash Banana DS or Banana Pocket to a microSD card.",
                 bg=HEADER_BG, fg=HEADER_SUB,
                 font=(self._font_ui, 10)).pack(anchor="w", pady=(8, 0))

        tk.Label(header_inner, text=f"V{APP_VERSION}", bg=HEADER_BG, fg=LABEL,
                 font=self._font_px(8)).pack(side="right", anchor="n")
        self._chrome_rule(self)

        # ---- body ----------------------------------------------------------
        body = tk.Frame(self, bg=BG, padx=24, pady=12)
        body.pack(fill="both", expand=True)

        # ---- step 1 : model ------------------------------------------------
        model_box = self._card(body, 1, "Choose your handheld")
        for name in MODELS:
            ttk.Radiobutton(
                model_box, text=name, value=name,
                variable=self.model_var, command=self._on_model_change,
            ).pack(anchor="w")
        ttk.Label(model_box, textvariable=self.release_var,
                  style="Detail.TLabel").pack(anchor="w", pady=(8, 0))

        # ---- step 2 : target drive ----------------------------------------
        drive_box = self._card(body, 2, "Select the target drive")
        drive_row = ttk.Frame(drive_box, style="Card.TFrame")
        drive_row.pack(fill="x")
        self.device_combo = ttk.Combobox(
            drive_row, textvariable=self.device_var, state="readonly",
            font=self._font_base,
        )
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.refresh_btn = self._button(drive_row, "Refresh",
                                        self.refresh_devices, variant="ghost")
        self.refresh_btn.pack(side="left", padx=(10, 0))
        self._hint(drive_box,
                   "Only physical drives are listed. The system/OS disk is "
                   "detected and cannot be flashed.", 740)

        # ---- step 3 : wipe and flash --------------------------------------
        action_box = self._card(body, 3, "Wipe and flash")
        self.status_lbl = ttk.Label(action_box, textvariable=self.status_var,
                                    style="Status.TLabel")
        self.status_lbl.pack(anchor="w")
        self.progress = ttk.Progressbar(
            action_box, variable=self.progress_var, maximum=100.0,
            mode="determinate", style="Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(8, 6))
        ttk.Label(action_box, textvariable=self.detail_var,
                  style="Detail.TLabel").pack(anchor="w")

        btn_row = ttk.Frame(action_box, style="Card.TFrame")
        btn_row.pack(fill="x", pady=(8, 0))
        self.start_btn = self._button(btn_row, "Download & Flash",
                                      self.start_flash)
        self.start_btn.pack(side="left")
        self.cancel_btn = self._button(btn_row, "Cancel", self.cancel_flash,
                                       variant="ghost", state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(btn_row, text="Keep downloaded image",
                        variable=self.keep_var).pack(side="right")

        # ---- activity log --------------------------------------------------
        log_outer = tk.Frame(body, bg=SURFACE, bd=0,
                             highlightbackground=BORDER, highlightthickness=2)
        log_outer.pack(fill="both", expand=True, pady=(0, 4))
        log_head = tk.Frame(log_outer, bg=SURFACE, padx=18, pady=0)
        log_head.pack(fill="x", pady=(11, 0))
        tk.Label(log_head, text="ACTIVITY LOG", bg=SURFACE, fg=LABEL,
                 font=self._font_px(9)).pack(side="left")

        log_area = tk.Frame(log_outer, bg=LOG_BG, padx=1, pady=1)
        log_area.pack(fill="both", expand=True, padx=18, pady=(8, 14))
        self.log_widget = tk.Text(
            log_area, height=6, wrap="word", state="disabled",
            font=(self._font_mono, 9), background=LOG_BG, foreground=LOG_TX,
            insertbackground=LOG_TX, selectbackground=ACCENT,
            selectforeground=ACCENT_TX,
            relief="flat", borderwidth=0, padx=12, pady=10,
            highlightthickness=0,
        )
        log_scroll = ttk.Scrollbar(log_area, orient="vertical",
                                   command=self.log_widget.yview,
                                   style="Log.Vertical.TScrollbar")
        self.log_widget.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_widget.pack(side="left", fill="both", expand=True)

        # ---- footer --------------------------------------------------------
        tk.Label(
            self,
            text="⚠   Writing to a drive erases everything on it — double-check "
                 "the device above. BananaLinux and BananaOS are community "
                 "projects; this tool is unaffiliated with Anbernic or Retroid.",
            bg=BG, fg=MUTED, font=(self._font_ui, 9), justify="left",
            wraplength=800, anchor="w", padx=24, pady=8,
        ).pack(fill="x", side="bottom", before=body)

    # ------------------------------------------------------------------ utils
    def log(self, message: str) -> None:
        """Thread-safe logging (also used directly during startup)."""
        self.queue.put(("log", message))

    def emit(self, event) -> None:
        self.queue.put(event)

    def _append_log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", message.rstrip() + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    # -------------------------------------------------------------- devices
    def refresh_devices(self) -> None:
        self.status_var.set("Scanning drives ...")
        self.update_idletasks()
        try:
            self.devices = list_devices()
        except Exception as exc:
            self.devices = []
            self.log(f"Failed to enumerate drives: {exc}")

        values = [d["label"] for d in self.devices]
        self.device_combo.configure(values=values)
        if values and self.device_var.get() not in values:
            # Prefer the first non-protected device.
            default = next((d["label"] for d in self.devices if not d["protected"]), values[0])
            self.device_var.set(default)
        elif not values:
            self.device_var.set("")
            self.log("No physical drives detected. Insert your microSD card and press Refresh.")

        self.log(f"Detected {len(self.devices)} physical drive(s).")
        self.status_var.set("Ready.")

    def _selected_device(self) -> dict | None:
        label = self.device_var.get()
        return next((d for d in self.devices if d["label"] == label), None)

    # -------------------------------------------------------------- release
    def _on_model_change(self) -> None:
        self._update_release_label()
        self._query_release_async()

    def _update_release_label(self) -> None:
        asset = MODELS[self.model_var.get()]
        info = self.release_cache.get(asset)
        if info:
            self.release_var.set(
                f"Asset: {asset}   ·   release {info['tag']}   ·   "
                f"{format_bytes(info['size']) if info.get('size') else 'size unknown'}"
            )
        else:
            self.release_var.set(f"Asset: {asset}   ·   checking GitHub ...")

    def _query_release_async(self) -> None:
        model = self.model_var.get()
        asset = MODELS[model]
        repo = repo_for_model(model)

        with self._releases_lock:
            if asset in self.release_cache:
                self._update_release_label()
                return

        def worker() -> None:
            try:
                info = resolve_release_asset(asset, repo=repo)
                self.release_cache[asset] = info
                self.emit(("release", asset, info, None))
            except FlasherError as exc:
                self.emit(("release", asset, None, str(exc)))
            except Exception as exc:  # pragma: no cover
                self.emit(("release", asset, None, str(exc)))

        threading.Thread(target=worker, name="release-lookup", daemon=True).start()

    # ---------------------------------------------------------------- flash
    def start_flash(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        model = self.model_var.get()
        asset = MODELS[model]
        device = self._selected_device()

        if device is None:
            self._show_error(
                "No drive selected",
                "Please insert a microSD card, press Refresh, and select it from "
                "the dropdown.",
            )
            return

        if device["protected"]:
            self._show_error(
                "Refusing to flash the system disk",
                "The selected drive looks like the disk your operating system is "
                "running from (or it holds mounted volumes). Flashing it would "
                "destroy your computer. Please pick a different device.",
            )
            return

        release = self.release_cache.get(asset)
        if release is None:
            self._show_error(
                "Release not resolved yet",
                f"The latest release information for '{asset}' is not available. "
                "Check your internet connection and try again.",
            )
            return

        if not self._confirm_flash(model, device, release):
            return

        # Elevation up front (Windows relaunch / POSIX sudo password).
        if not self._ensure_privileges():
            return

        self.progress_var.set(0.0)
        self.progress.configure(style="Horizontal.TProgressbar")
        self.detail_var.set("")
        self._set_running(True)
        repo = release.get("repo") or repo_for_model(model)
        self.log("—" * 60)
        self.log(f"Selected handheld : {model} ({repo})")
        self.log(f"Selected drive    : {device['label']}")
        self.log(f"Release           : {release['tag']}")

        self.worker = FlashWorker(
            self, model, device, release, keep_image=self.keep_var.get(),
        )
        self.worker.start()

    def _confirm_flash(self, model: str, device: dict, release: dict) -> bool:
        from tkinter import messagebox

        size = format_bytes(device["size"]) if device["size"] else "unknown size"
        if release.get("checksum"):
            checksum_line = f"\nSHA-256: {release['checksum'][:16]}… (will be verified)\n"
        else:
            checksum_line = ("\n⚠ This release publishes no checksum, so the "
                             "download cannot be verified.\n")
        repo = release.get("repo") or repo_for_model(model)
        message = (
            f"You are about to ERASE and overwrite:\n\n"
            f"    {device['path']}\n"
            f"    {device['label']}\n"
            f"    capacity: {size}\n\n"
            f"with:\n\n"
            f"    {MODELS[model]}  (release {release['tag']} from {repo})\n"
            f"{checksum_line}\n"
            "ALL data on that drive will be permanently destroyed, including its "
            "partition table.\n\nDo you want to continue?"
        )
        return messagebox.askyesno(
            "Confirm destructive write", message, icon="warning", default="no",
        )

    def _report_elevation_status(self) -> None:
        """Log whether we currently have the rights to write raw devices."""
        if is_elevated():
            self.log("Elevated privileges detected.")
            return
        if IS_WINDOWS:
            self.log("Running as a standard user. Administrator rights (UAC) "
                     "are requested only when you start a flash.")
        else:
            self.log("Not running as root; you will be asked for your sudo "
                     "password when you start a flash.")

    def _ensure_privileges(self) -> bool:
        if IS_WINDOWS:
            # The GUI intentionally stays unprivileged: the destructive step
            # elevates itself on demand through a UAC prompt when the flash
            # starts, so there is nothing to prepare here.
            if not is_elevated():
                self.log("Running without Administrator rights; the write will "
                         "request elevation via UAC when it starts.")
            return True

        if is_elevated():
            global SUDO_PREFIX
            SUDO_PREFIX = []
            return True

        password = self._ask_sudo_password()
        if password is None:
            return False
        self.status_var.set("Validating privileges ...")
        if self.privileges.cache_sudo(password):
            self.log("Sudo credentials cached; elevated commands are ready.")
            self.status_var.set("Ready.")
            return True

        self.status_var.set("Ready.")
        self._show_error(
            "Authentication failed",
            "sudo rejected the password. Please try again, or run the tool as "
            "root.",
        )
        return False

    def _ask_sudo_password(self) -> str | None:
        """Modal password prompt; returns the password or None if cancelled."""
        dialog = tk.Toplevel(self)
        dialog.title("sudo password required")
        dialog.configure(background=SURFACE, highlightbackground=BORDER,
                         highlightthickness=2)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.grab_set()

        wrap = tk.Frame(dialog, bg=SURFACE, padx=22, pady=18)
        wrap.pack(fill="both", expand=True)

        head = tk.Frame(wrap, bg=SURFACE)
        head.pack(fill="x")
        tk.Label(head, text="🔒", bg=SURFACE, fg=ACCENT,
                 font=(self._font_ui, 14)).pack(side="left")
        tk.Label(head, text="ELEVATED RIGHTS REQUIRED", bg=SURFACE, fg=LABEL,
                 font=self._font_px(9)).pack(side="left", padx=(10, 0))

        tk.Label(
            wrap,
            text="Wiping and writing raw block devices needs sudo.\n"
                 "Your password is used only to cache a sudo session.",
            bg=SURFACE, fg=MUTED, font=(self._font_ui, 9), justify="left",
        ).pack(anchor="w", pady=(8, 12))

        entry_var = tk.StringVar()
        entry = ttk.Entry(wrap, textvariable=entry_var, show="•", width=38,
                          font=self._font_base)
        entry.pack(fill="x")
        entry.focus_set()

        result: dict = {"value": None}

        def accept(event=None) -> None:
            result["value"] = entry_var.get()
            dialog.destroy()

        def cancel(event=None) -> None:
            result["value"] = None
            dialog.destroy()

        buttons = tk.Frame(wrap, bg=SURFACE)
        buttons.pack(fill="x", pady=(16, 0))
        self._button(buttons, "Unlock", accept).pack(side="right")
        self._button(buttons, "Cancel", cancel,
                     variant="ghost").pack(side="right", padx=(0, 8))
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", cancel)

        self._center(dialog)
        dialog.after(20, entry.focus_force)
        self.wait_window(dialog)
        return result["value"]

    def _center(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        width = window.winfo_width() or window.winfo_reqwidth()
        height = window.winfo_height() or window.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 3
        window.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def cancel_flash(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.cancel()
            self.status_var.set("Cancelling ...")
            self.log("Cancel requested; stopping after the current block ...")

    # ----------------------------------------------------------- state mgmt
    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_btn.configure(state=state)
        self.refresh_btn.configure(state=state)
        self.device_combo.configure(state="disabled" if running else "readonly")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def _reset_progress(self) -> None:
        if self._indeterminate:
            self.progress.stop()
            self._indeterminate = False
        self.progress.configure(mode="determinate",
                                style="Horizontal.TProgressbar")

    def _show_error(self, title: str, message: str) -> None:
        from tkinter import messagebox
        messagebox.showerror(title, message)

    # ------------------------------------------------------------ queue pump
    def _drain_queue(self) -> None:
        # One bad event must never kill the UI pump, hence the inner try/except
        # and the finally-reschedule.
        try:
            while True:
                try:
                    kind, *payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(kind, payload)
                except Exception as exc:  # pragma: no cover - defensive
                    self._append_log(f"[ui] error while handling '{kind}': {exc}")
        finally:
            if not self._closing:
                self.after(self.POLL_MS, self._drain_queue)

    def _handle_event(self, kind: str, payload: list) -> None:
        if kind == "log":
            self._append_log(payload[0])
        elif kind == "status":
            self.status_var.set(payload[0])
        elif kind == "progress":
            fraction, detail = payload
            if fraction is None:
                # Indeterminate fallback: animate a sweeping bar (start once).
                if not self._indeterminate:
                    self._indeterminate = True
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(12)
            else:
                if self._indeterminate:
                    self._indeterminate = False
                    self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress_var.set(max(0.0, min(1.0, fraction)) * 100.0)
            self.detail_var.set(detail)
        elif kind == "release":
            asset, info, error = payload
            if info:
                self.release_cache[asset] = info
                repo = info.get("repo") or repo_for_asset(asset)
                self.log(f"Latest release {info['tag']} ({repo}): {info['name']} "
                         f"({format_bytes(info['size']) if info['size'] else '?'})")
            else:
                self.log(f"Could not resolve release asset: {error}")
            if MODELS.get(self.model_var.get()) == asset:
                self._update_release_label()
        elif kind == "done":
            self._on_done(payload[0])
        elif kind == "cancelled":
            self._set_running(False)
            self._reset_progress()
            self.detail_var.set("")
            self.status_var.set("Cancelled — the drive may be left blank.")
        elif kind == "error":
            self._set_running(False)
            self._reset_progress()
            self.status_var.set("Failed.")
            self._show_error("Flashing failed", payload[0])

    def _on_done(self, result: dict) -> None:
        from tkinter import messagebox

        self._set_running(False)
        self._reset_progress()
        self.progress.configure(style="Done.Horizontal.TProgressbar")
        self.progress_var.set(100.0)
        self.detail_var.set(f"Wrote {format_bytes(result['written'])} "
                            f"in {format_duration(result['elapsed'])}")
        self.status_var.set("Finished — you can remove the card.")

        messagebox.showinfo(
            "Flashing complete",
            f"BananaLinux was written to the microSD card.\n\n"
            f"Handheld : {result['model']}\n"
            f"Drive    : {result['device']['path']}\n"
            f"Release  : {result['tag']}\n"
            f"Written  : {format_bytes(result['written'])}\n"
            f"Time     : {format_duration(result['elapsed'])}\n\n"
            "It is now safe to remove the card and insert it into your device.",
        )

    # ----------------------------------------------------------------- close
    def _on_close(self, silent: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            from tkinter import messagebox
            if not silent and not messagebox.askyesno(
                "Quit while flashing?",
                "A flash operation is still running. Quitting now will leave the "
                "card in an inconsistent state.\n\nQuit anyway?",
                icon="warning",
            ):
                return
            self.worker.cancel()
            if not silent:
                # The worker is a daemon thread: destroying now would kill it
                # before its cleanup (temp image, dd, Windows automount) runs.
                self.status_var.set("Cancelling — cleaning up before exit ...")
                self.withdraw()
                self._finish_close(time.monotonic() + CLOSE_WAIT_S)
                return

        self._closing = True
        self.privileges.shutdown()
        if not silent:
            self.destroy()

    def _finish_close(self, deadline: float) -> None:
        """Poll until the cancelled worker has unwound (or time runs out)."""
        worker = self.worker
        if worker and worker.is_alive() and time.monotonic() < deadline:
            self.after(100, self._finish_close, deadline)
            return
        if worker and worker.is_alive():
            # Gave up waiting: at least drop the multi-GB download.
            workdir = getattr(worker, "_workdir", None)
            if workdir and not worker.keep_image:
                shutil.rmtree(workdir, ignore_errors=True)
        self._closing = True
        self.privileges.shutdown()
        self.destroy()


# ===========================================================================
#  Entry point
# ===========================================================================
def main() -> int:
    # Elevated helper mode: no GUI, just the privileged wipe + write. The GUI
    # spawns us this way via the UAC "runas" verb (see _spawn_elevated_write).
    if len(sys.argv) >= 3 and sys.argv[1] == PRIVILEGED_WRITE_FLAG:
        return _privileged_write_main(sys.argv[2])

    try:
        app = BananaFlasherApp()
    except tk.TclError as exc:
        print(
            f"{APP_NAME}: could not start the GUI ({exc}).\n"
            "A graphical display is required. On a headless server, run the "
            "tools directly or install Tk.",
            file=sys.stderr,
        )
        return 2

    try:
        app.mainloop()
    finally:
        app.privileges.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())