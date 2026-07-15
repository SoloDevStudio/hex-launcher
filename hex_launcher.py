#!/usr/bin/env python3
"""Hex Launcher -- install, update, and play Hex (uohex.com) in one window.

What it does, in order:
  1. Downloads/verifies the freely-distributed UO client files, straight from
     the publisher's official patch servers (via the vendored MIT-licensed
     uo-patcher core -- https://github.com/andrezaiats/uo-patcher). The
     launcher never redistributes game assets: every player downloads their
     own copy from the official source, exactly as the official installer
     would.
  2. Downloads the open-source ClassicUO client (official GitHub release).
  3. First run: creates your account right in the launcher -- email once,
     password twice, verification code from your inbox -- all before the
     game opens.
  4. Launches the game pointed at Hex, with your email pre-filled.

Re-running the launcher verifies/updates everything and plays again --
it is the patcher from then on. Your saved login is never overwritten.

A quiet "Server:" toggle at the bottom of the window switches between the live
Hex shard and the Hex Beta shard (separate accounts and characters); the choice
is remembered, and --server hex|beta forces it for a single run.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

import uo_patcher

# ---------------------------------------------------------------------------
# Shard configuration -- the only lines that make this "the Hex launcher"
# ---------------------------------------------------------------------------

SHARD_NAME = "Hex"
WEBSITE = "uohex.com"

# Endpoint profiles: the launcher can point at the live shard or the beta shard.
# Each profile is (display name, server host, server port, account-API base URL).
PROFILES = {
    "hex":  (SHARD_NAME, "play.uohex.com", 2593, f"https://{WEBSITE}/api"),
    "beta": ("Hex Beta", "beta-play.uohex.com", 2594, f"https://{WEBSITE}/api-beta"),
}
DEFAULT_SERVER = "hex"

# The active profile key. Module-level so the console path and the GUI's server
# picker share it; every endpoint reader below resolves it at CALL time, so
# toggling the picker takes effect immediately for later API calls and PLAY.
_active_server = DEFAULT_SERVER


def set_active_server(key: str | None) -> None:
    global _active_server
    _active_server = key if key in PROFILES else DEFAULT_SERVER


def active_server() -> str:
    return _active_server


# HEX_SERVER / HEX_PORT / HEX_API env vars override the active profile's
# endpoints (dev/test use) -- they win regardless of which profile is active.
_ENV_HOST = os.environ.get("HEX_SERVER")
_ENV_PORT = os.environ.get("HEX_PORT")
_ENV_API = os.environ.get("HEX_API")
# HEX_INVITE, if set, is sent automatically as the beta invite code (dev/test use).
INVITE_CODE = os.environ.get("HEX_INVITE", "")


def server_name() -> str:
    return PROFILES[_active_server][0]


def server_host() -> str:
    return _ENV_HOST or PROFILES[_active_server][1]


def server_port() -> int:
    return int(_ENV_PORT) if _ENV_PORT else PROFILES[_active_server][2]


def api_url() -> str:
    return _ENV_API or PROFILES[_active_server][3]


CLIENT_VERSION = "7.0.116.0"

# Hex's own client build (email accounts on the login screen, 30-char fields),
# hosted on our website -- players never need GitHub.
CUO_DOWNLOAD = "https://uohex.com/downloads/{asset}"
CUO_ASSETS = {
    "Windows": "ClassicUO-win-x64-release.zip",
    "Darwin": "ClassicUO-osx-x64-release.zip",
    "Linux": "ClassicUO-linux-x64-release.zip",
}

BANNER = rf"""
 _   _  _____ __  __
| | | || ____|\ \/ /
| |_| ||  _|   \  /    {WEBSITE}
|  _  || |___  /  \    Ultima Online -- Renaissance era, Felucca only
|_| |_||_____|/_/\_\
"""


def install_root(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path.home()
    return base / "HexUO"


# The player's chosen install folder is remembered here (their home folder),
# so the launcher itself stays portable and multi-drive installs stick.
def _launcher_config_path() -> Path:
    return Path.home() / ".hexuo-launcher.json"


def _load_config() -> dict:
    try:
        data = json.loads(_launcher_config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(**updates) -> None:
    # Merge into whatever is already there so we never clobber sibling keys
    # (install_root, server, or anything a future version adds).
    data = _load_config()
    data.update(updates)
    try:
        _launcher_config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # non-fatal: worst case a picker shows again next run


def load_saved_root() -> Path | None:
    saved = _load_config().get("install_root")
    return Path(saved) if saved else None


def save_root(root: Path) -> None:
    _save_config(install_root=str(root))


def load_saved_server() -> str:
    key = _load_config().get("server")
    return key if key in PROFILES else DEFAULT_SERVER


def save_server(key: str) -> None:
    _save_config(server=key if key in PROFILES else DEFAULT_SERVER)


# ---------------------------------------------------------------------------
# Step 1: game files (delegates entirely to the uo-patcher core)
# ---------------------------------------------------------------------------

def ensure_game_files(client_dir: Path, skip: bool) -> None:
    if skip:
        print("[*] Skipping game-file check (--skip-patch).")
        return
    print(f"[*] Game files: {client_dir}")
    print("[*] Downloading/verifying from the official patch servers.")
    print("    First install is ~1.6 GB -- later runs only fetch what changed.\n")
    # 16 workers: the patch tail is thousands of tiny files, latency-bound.
    uo_patcher.run_patch(str(client_dir), workers=16)


# ---------------------------------------------------------------------------
# Step 2: ClassicUO
# ---------------------------------------------------------------------------

def _remote_size(url: str) -> int:
    """Content-Length via HEAD, or 0 if the server won't say."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "HexLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(resp.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _download_with_progress(url: str, dest: Path, attempts: int = 3,
                            on_bytes=None, on_total=None) -> None:
    last_err = None
    for attempt in range(1, attempts + 1):
        done = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HexLauncher/1.0"})
            # timeout guards against a stalled connection hanging setup forever.
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                if total and on_total:
                    on_total(total)
                next_mark = 10
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
                    if total and done * 100 // total >= next_mark:
                        print(f"    {done * 100 // total}%")
                        next_mark += 10
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            if done and on_bytes:
                on_bytes(-done)  # roll back so the retry doesn't double-count
            print(f"[!] Download attempt {attempt} failed: {e}")
            dest.unlink(missing_ok=True)
    raise RuntimeError(f"Could not download after {attempts} tries: {last_err}")


def find_cuo_executable(cuo_dir: Path) -> Path | None:
    names = ["ClassicUO.exe"] if platform.system() == "Windows" else ["ClassicUO", "ClassicUO.bin"]
    for name in names:
        hits = sorted(cuo_dir.rglob(name))
        if hits:
            return hits[0]
    return None


def ensure_classicuo(cuo_dir: Path, force: bool, progress=None) -> Path:
    exe = find_cuo_executable(cuo_dir)
    if exe and not force:
        print(f"[*] ClassicUO already installed: {exe}")
        return exe

    system = platform.system()
    asset = CUO_ASSETS.get(system)
    if asset is None:
        raise RuntimeError(f"Unsupported platform: {system}")

    url = CUO_DOWNLOAD.format(asset=asset)
    cuo_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cuo_dir / asset
    print(f"[*] Downloading ClassicUO ({system}) ...")
    if progress:
        progress.set_phase("Downloading the game client")
    _download_with_progress(
        url, zip_path,
        on_bytes=progress.add_bytes if progress else None,
        on_total=progress.ensure_cuo_total if progress else None,
    )

    print("[*] Extracting ...")
    if progress:
        progress.set_phase("Unpacking the game client")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(cuo_dir)
    zip_path.unlink()

    exe = find_cuo_executable(cuo_dir)
    if exe is None:
        raise RuntimeError(f"ClassicUO executable not found under {cuo_dir}")

    if platform.system() != "Windows":
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[*] ClassicUO ready: {exe}")
    return exe


# ---------------------------------------------------------------------------
# Step 3: settings -- create on first run, then only re-pin the shard fields
# so a player's saved username/password is never clobbered.
# ---------------------------------------------------------------------------

def write_settings(cuo_exe: Path, client_dir: Path) -> Path:
    settings_path = cuo_exe.parent / "settings.json"

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("[!] Existing settings.json unreadable -- rewriting it fresh.")
            settings = {}

    settings.setdefault("username", "")
    settings.setdefault("password", "")
    settings.setdefault("saveaccount", True)
    settings.setdefault("autologin", False)
    settings.setdefault("lang", "ENU")
    settings.setdefault("encryption", 0)
    settings.update(
        {
            "ip": server_host(),
            "port": server_port(),
            "ultimaonlinedirectory": str(client_dir),
            "clientversion": CLIENT_VERSION,
            "last_server_name": server_name(),
        }
    )

    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    print(f"[*] Shard settings written: {settings_path}")
    return settings_path


def _cuo_machine_name() -> str:
    """Mirrors .NET Environment.MachineName: COMPUTERNAME on Windows,
    hostname trimmed at the first dot elsewhere (verified against ClassicUO)."""
    import socket

    if platform.system() == "Windows":
        return os.environ.get("COMPUTERNAME") or socket.gethostname().split(".")[0]
    return socket.gethostname().split(".")[0]


def _cuo_encrypt(source: str) -> str:
    """Port of ClassicUO.Utility.Crypter.Encrypt -- XOR vs machine name, hex, '1-' prefix.
    Output verified byte-identical to the C# implementation."""
    if not source:
        return ""
    key = _cuo_machine_name()
    if not key:
        return ""
    return "1-" + "".join(
        f"{b ^ ord(key[i % len(key)]):02X}" for i, b in enumerate(source.encode("ascii", "ignore"))
    )


def save_login(settings_path: Path, email: str, password: str | None = None) -> None:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        settings = {}
    settings["username"] = email
    if password:
        settings["password"] = _cuo_encrypt(password)
        settings["saveaccount"] = True
        settings["autologin"] = True
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def save_username(settings_path: Path, email: str) -> None:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        settings = {}
    settings["username"] = email
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def saved_username(settings_path: Path) -> str:
    try:
        return json.loads(settings_path.read_text(encoding="utf-8")).get("username", "")
    except (json.JSONDecodeError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Account API (signup/verify happen HERE, before the game ever opens)
# ---------------------------------------------------------------------------

def _api_post(endpoint: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{api_url()}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "HexLauncher/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "message": f"Server error ({e.code})."}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach the server ({e})."}


def _looks_like_email(s: str) -> bool:
    at = s.find("@")
    return 0 < at == s.rfind("@") and "." in s[at + 2 :] and " " not in s


# ---------------------------------------------------------------------------
# Step 4: play
# ---------------------------------------------------------------------------

def launch(cuo_exe: Path) -> None:
    print(f"\n[*] Launching {server_name()} ...")
    cmd = [str(cuo_exe)]
    # With saved credentials, skip the classic login screen entirely.
    try:
        settings = json.loads((cuo_exe.parent / "settings.json").read_text(encoding="utf-8"))
        if settings.get("username") and settings.get("password"):
            cmd.append("-skiploginscreen")
    except (OSError, json.JSONDecodeError):
        pass
    subprocess.Popen(cmd, cwd=str(cuo_exe.parent))


# ---------------------------------------------------------------------------
# Graphical launcher (default)
# ---------------------------------------------------------------------------

class _SetupProgress:
    """Single source of truth for the setup progress bar.

    One grand byte total covers the whole install -- game files (fed by
    uo_patcher's progress hook) plus the ClassicUO zip -- so the bar moves
    0 -> 100% exactly once, with real speed and time-remaining.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.phase = ""
        self.done = 0
        self.total = 0
        self.files = (0, 0)      # (done, total) for tiny-file phases
        self.files_planned = 0   # tiny-file stretch not yet started: N queued
        self._cuo_total = 0
        # Sample deques are touched by the GUI thread only.
        self.samples: deque = deque()   # (monotonic_time, done_bytes)
        self.fsamples: deque = deque()  # (monotonic_time, files_done)

    def hook(self, event, value=None):  # uo_patcher.progress_hook signature
        with self.lock:
            if event == "phase":
                self.phase = value or ""
                self.files = (0, 0)
            elif event == "total":
                self.total += int(value or 0)
            elif event == "bytes":
                self.done += int(value or 0)
            elif event == "files":
                self.files = tuple(value) if value else (0, 0)
                # Once the stretch has fully arrived, stop advertising it as
                # "still ahead" (the ClassicUO download comes after it).
                if self.files[1] > 0 and self.files[0] >= self.files[1]:
                    self.files_planned = 0
            elif event == "files_planned":
                self.files_planned = int(value or 0)

    def set_phase(self, name: str) -> None:
        self.hook("phase", name)

    def add_bytes(self, n: int) -> None:
        self.hook("bytes", n)

    def add_cuo_total(self, n: int) -> None:
        with self.lock:
            self._cuo_total = n
            self.total += n

    def ensure_cuo_total(self, n: int) -> None:
        # Called with the GET Content-Length; only counts if the HEAD probe failed.
        with self.lock:
            if self._cuo_total == 0:
                self._cuo_total = n
                self.total += n

    def reset(self) -> None:
        with self.lock:
            self.phase = ""
            self.done = 0
            self.total = 0
            self.files = (0, 0)
            self.files_planned = 0
            self._cuo_total = 0
        self.samples.clear()
        self.fsamples.clear()

    def snapshot(self):
        with self.lock:
            return self.phase, self.done, self.total, self.files, self.files_planned


class _StdoutQueue:
    """Redirects print() output from worker threads into the UI log."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, text: str) -> None:
        if text.strip():
            self.q.put(text.rstrip("\n"))

    def flush(self) -> None:
        pass


def run_gui(args) -> int:
    import tkinter as tk
    from tkinter import ttk

    BG, PANEL, FG, ACCENT, DIM = "#161219", "#221b26", "#e8e0d8", "#c9a227", "#8a8078"
    BETA_ACCENT = "#8fbf6f"  # colder green-gold -- signals "you're on the beta shard"

    root = tk.Tk()
    root.title(f"{SHARD_NAME} Launcher — {WEBSITE}")
    root.configure(bg=BG)
    root.geometry("560x700")
    root.minsize(540, 640)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TButton", font=("Georgia", 13, "bold"))

    log_q: queue.Queue = queue.Queue()
    sys.stdout = _StdoutQueue(log_q)
    sys.stderr = _StdoutQueue(log_q)

    state = {"cuo_exe": None, "client_dir": None, "settings_path": None,
             "email": "", "password": None, "setup_running": False}
    prog = _SetupProgress()
    uo_patcher.progress_hook = prog.hook

    # ----- header -----
    header_label = tk.Label(root, text="HEX", font=("Georgia", 40, "bold"), fg=ACCENT, bg=BG)
    header_label.pack(pady=(18, 0))
    tk.Label(root, text=f"{WEBSITE}  ·  Ultima Online, Renaissance era", font=("Georgia", 13),
             fg=DIM, bg=BG).pack()

    # ----- status: one friendly line for players -----
    status_var = tk.StringVar(value="Checking your installation ...")
    tk.Label(root, textvariable=status_var, font=("Georgia", 14), fg=FG, bg=BG,
             wraplength=500, justify="center").pack(pady=(16, 4), padx=24)

    def set_status(msg: str) -> None:
        root.after(0, status_var.set, msg)

    # Technical output goes to a quiet log file for support -- no UI for it.
    # The progress bar is driven by _SetupProgress (polled below), not by
    # parsing log lines.
    log_file = Path.home() / ".hexuo-launcher.log"

    def pump_log() -> None:
        lines = []
        try:
            while True:
                lines.append(log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError:
                pass
        root.after(250, pump_log)

    pump_log()

    # ----- account panel (hidden until setup finishes) -----
    acct = tk.Frame(root, bg=BG)

    def field(parent, label, show=None):
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x")
        label_var = tk.StringVar(value=label)
        tk.Label(box, textvariable=label_var, fg=FG, bg=BG, font=("Georgia", 12)).pack(anchor="w", padx=24)
        e = tk.Entry(box, show=show, font=("Menlo", 13), bg=PANEL, fg=FG,
                     insertbackground=FG, bd=0, highlightthickness=1,
                     highlightbackground=DIM, highlightcolor=ACCENT)
        e.pack(fill="x", padx=24, pady=(2, 8), ipady=5)
        return box, label_var, e

    signup_frame = tk.Frame(acct, bg=BG)
    email_box, _, email_e = field(signup_frame, "Email address (this is your account name)")
    pw_box, pw_label, pw_e = field(signup_frame, "Choose a password", show="●")
    pw2_box, pw2_label, pw2_e = field(signup_frame, "Type the password again", show="●")
    invite_box, _, invite_e = field(signup_frame, "Invite code (leave blank if none)")

    verify_frame = tk.Frame(acct, bg=BG)
    _, code_label, code_e = field(verify_frame, "6-digit code from your inbox")

    btn_row = tk.Frame(acct, bg=BG)
    btn_row.pack(side="bottom", fill="x", padx=24, pady=(0, 8))

    action_btn = ttk.Button(btn_row, text="Create Account")
    action_btn.pack(side="left")
    alt_btn = ttk.Button(btn_row, text="I already have an account")
    alt_btn.pack(side="right")

    forgot_btn = tk.Button(acct, text="Forgot password?", font=("Georgia", 11, "underline"),
                           fg=DIM, bg=BG, bd=0, activebackground=BG, activeforeground=FG,
                           highlightthickness=0, cursor="hand2")

    # ----- install-location panel (first run only) -----
    install_panel = tk.Frame(root, bg=BG)
    tk.Label(install_panel, text="Where should Hex be installed?", fg=FG, bg=BG,
             font=("Georgia", 12)).pack(anchor="w", padx=24)
    path_var = tk.StringVar()
    tk.Label(install_panel, textvariable=path_var, fg=ACCENT, bg=PANEL,
             font=("Menlo", 12), anchor="w", padx=8, pady=6).pack(fill="x", padx=24, pady=(2, 8))
    install_row = tk.Frame(install_panel, bg=BG)
    install_row.pack(fill="x", padx=24, pady=(0, 8))
    install_btn = ttk.Button(install_row, text="Install here")
    install_btn.pack(side="left")
    change_btn = ttk.Button(install_row, text="Change folder ...")
    change_btn.pack(side="right")

    def pick_folder() -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(initialdir=str(Path(path_var.get()).parent),
                                         title="Choose where to install Hex")
        if chosen:
            chosen_path = Path(chosen)
            if chosen_path.name != "HexUO":
                chosen_path = chosen_path / "HexUO"
            path_var.set(str(chosen_path))

    change_btn.configure(command=pick_folder)

    # ----- play button -----
    play_btn = ttk.Button(root, text="▶  PLAY", state="disabled")
    play_btn.pack(pady=(0, 18), ipadx=30, ipady=6)

    # ----- progress bar -----
    # An indeterminate "working" sweep only while the totals are still unknown
    # (contacting the patch server); a single real 0-100% bar for everything
    # after that, with speed and time-remaining on the detail line below it.
    progress_bar = ttk.Progressbar(root, mode="determinate", maximum=100)
    detail_var = tk.StringVar(value="")
    detail_label = tk.Label(root, textvariable=detail_var, font=("Georgia", 11),
                            fg=DIM, bg=BG)
    _progress_state = ["hidden"]  # hidden | busy | determinate

    def _ensure_shown() -> None:
        if _progress_state[0] == "hidden":
            progress_bar.pack(fill="x", padx=40, pady=(0, 2), before=play_btn)
            detail_label.pack(pady=(0, 8), before=play_btn)

    def show_busy() -> None:
        _ensure_shown()
        if _progress_state[0] != "busy":
            progress_bar.configure(mode="indeterminate")
            progress_bar.start(12)
            _progress_state[0] = "busy"

    def set_progress(pct) -> None:
        _ensure_shown()
        if _progress_state[0] != "determinate":
            progress_bar.stop()
            progress_bar.configure(mode="determinate")
            _progress_state[0] = "determinate"
        progress_bar["value"] = pct

    def hide_progress() -> None:
        if _progress_state[0] != "hidden":
            progress_bar.stop()
            progress_bar.pack_forget()
            detail_label.pack_forget()
            detail_var.set("")
            _progress_state[0] = "hidden"

    # ----- live progress: one overall bar + speed + time remaining -----
    _DOWNLOAD_PHASES = ("Downloading game files", "Downloading the game client")
    _SLOW_PHASES = ("Scanning existing game files", "Assembling game archives",
                    "Verifying game files", "Unpacking the game client")

    def _fmt_eta(sec: float) -> str:
        sec = int(sec)
        if sec >= 3600:
            return f"{sec // 3600}h {sec % 3600 // 60}m"
        if sec >= 60:
            return f"{sec // 60}m {sec % 60:02d}s"
        return f"{sec}s"

    def poll_progress() -> None:
        if state["setup_running"]:
            phase, done, total, (f_done, f_total), f_planned = prog.snapshot()
            now = time.monotonic()
            prog.samples.append((now, done))
            while prog.samples and now - prog.samples[0][0] > 12:
                prog.samples.popleft()

            if phase in _DOWNLOAD_PHASES and total > 0:
                if f_total > 0 and f_done < f_total:
                    # Tiny-file stretch: files-done is the honest unit for bar,
                    # headline, and ETA alike -- the byte bar would sit at ~9x%
                    # for minutes (latency-bound, not bandwidth-bound), and byte
                    # speed reads near-zero even when it's going fast.
                    # Wider window than the byte-speed one: per-file rates vary
                    # a lot second-to-second, and a twitchy ETA reads as broken.
                    fpct = f_done / f_total * 100
                    set_progress(fpct)
                    status_var.set(f"{phase} -- {f_done:,} of {f_total:,} "
                                   f"small files ({fpct:.0f}%)")
                    prog.fsamples.append((now, f_done))
                    while prog.fsamples and now - prog.fsamples[0][0] > 30:
                        prog.fsamples.popleft()
                    ft0, ff0 = prog.fsamples[0]
                    if now - ft0 >= 1.0 and f_done > ff0:
                        frate = (f_done - ff0) / (now - ft0)
                        detail_var.set(f"{frate:,.0f} files/s · about "
                                       f"{_fmt_eta((f_total - f_done) / frate)} left")
                    else:
                        detail_var.set("")
                else:
                    shown = min(done, total)
                    pct = shown / total * 100
                    set_progress(pct)
                    status_var.set(f"{phase} -- {shown / 1048576:,.0f} of "
                                   f"{total / 1048576:,.0f} MB ({pct:.0f}%)")
                    prog.fsamples.clear()
                    t0, b0 = prog.samples[0]
                    if now - t0 >= 1.0 and done > b0:
                        speed = (done - b0) / (now - t0)
                        remaining = max(total - done, 0)
                        eta = _fmt_eta(remaining / speed)
                        if f_planned > 0 and f_total == 0:
                            # A latency-bound tiny-file stage still follows;
                            # a bytes-only ETA would promise "2s left" here.
                            detail_var.set(f"{speed / 1048576:.1f} MB/s · about "
                                           f"{eta} left, then {f_planned:,} small files")
                        else:
                            detail_var.set(f"{speed / 1048576:.1f} MB/s · "
                                           f"about {eta} left")
                    else:
                        detail_var.set("")
            elif phase:
                status_var.set(phase + " ...")
                if total > 0:
                    set_progress(min(done, total) / total * 100)
                else:
                    show_busy()
                detail_var.set("This can take a minute -- the bar holds still here."
                               if phase in _SLOW_PHASES else "")
        root.after(250, poll_progress)

    poll_progress()

    def busy(b: bool) -> None:
        action_btn.configure(state="disabled" if b else "normal")
        alt_btn.configure(state="disabled" if b else "normal")

    def _clear_form() -> None:
        verify_frame.pack_forget()
        signup_frame.pack_forget()
        forgot_btn.pack_forget()
        for box in (email_box, pw_box, pw2_box, invite_box):
            box.pack_forget()

    def _show_acct() -> None:
        acct.pack(fill="x", pady=(0, 4), before=play_btn)

    def show_signup() -> None:
        _clear_form()
        pw_label.set("Choose a password")
        pw2_label.set("Type the password again")
        email_box.pack(fill="x")
        pw_box.pack(fill="x")
        pw2_box.pack(fill="x")
        invite_box.pack(fill="x")
        signup_frame.pack(fill="x")
        _show_acct()
        action_btn.configure(text="Create Account", command=do_signup)
        alt_btn.configure(text="I already have an account", command=show_existing)

    def show_existing() -> None:
        _clear_form()
        pw_label.set("Password")
        email_box.pack(fill="x")
        pw_box.pack(fill="x")
        signup_frame.pack(fill="x")
        forgot_btn.configure(command=show_forgot)
        forgot_btn.pack(side="bottom", pady=(0, 2))
        _show_acct()
        action_btn.configure(text="Log In", command=do_existing)
        alt_btn.configure(text="Create a new account", command=show_signup)
        set_status("Enter your email and password, then press Log In.")

    # (Log In is the starting page; "Create a new account" is one click away.)

    def show_verify() -> None:
        _clear_form()
        code_label.set("6-digit code from your inbox")
        verify_frame.pack(fill="x")
        _show_acct()
        action_btn.configure(text="Verify", command=do_verify)
        alt_btn.configure(text="Resend code", command=do_resend)

    def show_forgot() -> None:
        _clear_form()
        email_box.pack(fill="x")
        signup_frame.pack(fill="x")
        _show_acct()
        action_btn.configure(text="Send reset code", command=do_forgot)
        alt_btn.configure(text="Back to log in", command=show_existing)
        set_status("Type your account email, then press Send reset code.")

    def show_reset() -> None:
        _clear_form()
        pw_label.set("New password")
        pw2_label.set("Type the new password again")
        code_label.set("Reset code from your inbox")
        verify_frame.pack(fill="x")
        pw_box.pack(fill="x")
        pw2_box.pack(fill="x")
        signup_frame.pack(fill="x")
        _show_acct()
        action_btn.configure(text="Set new password", command=do_reset)
        alt_btn.configure(text="Resend code", command=lambda: api_call("/forgot", {"email": state["email"]}, lambda _: None))

    def account_done() -> None:
        acct.pack_forget()
        play_btn.configure(state="normal")
        set_status("Ready! Press PLAY -- you'll go straight into the game.")

    def api_call(endpoint: str, payload: dict, on_ok, on_fail=None) -> None:
        busy(True)

        def work():
            result = _api_post(endpoint, payload)
            def finish():
                busy(False)
                set_status(result.get("message", ""))
                if result.get("ok"):
                    on_ok(result)
                elif on_fail:
                    on_fail(result)
            root.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    def do_signup() -> None:
        email = email_e.get().strip()
        pw, pw2 = pw_e.get(), pw2_e.get()
        if not _looks_like_email(email):
            set_status("That doesn't look like an email address -- check it and try again.")
            return
        if len(email) > 30:
            set_status("Email must be 30 characters or fewer.")
            return
        if not pw or pw != pw2:
            set_status("Passwords don't match (or were empty) -- try again.")
            return
        state["email"] = email
        state["password"] = pw
        invite = INVITE_CODE or invite_e.get().strip()

        def ok(_):
            show_verify()

        def fail(result):
            if "already exists" in result.get("message", ""):
                save_login(state["settings_path"], email, pw)
                account_done()

        payload = {"email": email, "password": pw}
        if invite:
            payload["inviteCode"] = invite
        api_call("/signup", payload, ok, fail)

    def do_verify() -> None:
        code = code_e.get().strip()
        if not code:
            set_status("Type the 6-digit code from your inbox.")
            return

        def ok(_):
            save_login(state["settings_path"], state["email"], state.get("password"))
            account_done()

        api_call("/verify", {"email": state["email"], "code": code}, ok)

    def do_resend() -> None:
        api_call("/resend", {"email": state["email"]}, lambda _: None)

    def do_existing() -> None:
        email = email_e.get().strip()
        pw = pw_e.get()
        if not _looks_like_email(email):
            set_status("Type your account email in the top field first.")
            return
        if not pw:
            set_status("Type your password in the first password box too.")
            return
        save_login(state["settings_path"], email, pw)
        account_done()

    def do_forgot() -> None:
        email = email_e.get().strip()
        if not _looks_like_email(email):
            set_status("Type your account email first.")
            return
        state["email"] = email
        api_call("/forgot", {"email": email}, lambda _: show_reset())

    def do_reset() -> None:
        code = code_e.get().strip()
        pw, pw2 = pw_e.get(), pw2_e.get()
        if not code:
            set_status("Type the reset code from your inbox.")
            return
        if not pw or pw != pw2:
            set_status("New passwords don't match (or were empty) -- try again.")
            return

        def ok(_):
            save_login(state["settings_path"], state["email"], pw)
            account_done()

        api_call("/reset", {"email": state["email"], "code": code, "newPassword": pw}, ok)

    def do_play() -> None:
        # Re-pin settings.json to the active profile's host/port before launch --
        # the server picker may have changed the shard since setup wrote it.
        if state.get("cuo_exe") and state.get("client_dir"):
            write_settings(state["cuo_exe"], state["client_dir"])
        launch(state["cuo_exe"])
        root.after(1200, on_close)

    def do_uninstall() -> None:
        from tkinter import messagebox
        import shutil

        target = state.get("settings_path")
        root_dir = load_saved_root() or (Path(args.dir).expanduser() if args.dir else None)
        if root_dir is None and target is not None:
            root_dir = Path(target).parent.parent
        if root_dir is None:
            set_status("Nothing to uninstall.")
            return

        if not messagebox.askyesno(
            "Uninstall Hex",
            f"Delete the game and all downloaded files?\n\n{root_dir}\n\nYour account stays safe on the server.",
        ):
            return

        try:
            shutil.rmtree(root_dir, ignore_errors=True)
            _launcher_config_path().unlink(missing_ok=True)
            (Path.home() / ".hexuo-launcher.log").unlink(missing_ok=True)
        finally:
            on_close()

    uninstall_btn = tk.Button(root, text="Uninstall", command=do_uninstall,
                              font=("Georgia", 9), fg=DIM, bg=BG, bd=0,
                              activebackground=BG, activeforeground=FG,
                              highlightthickness=0, cursor="hand2")
    uninstall_btn.pack(side="bottom", pady=(0, 4))

    # Always-available: repoint the install folder at any time, not just first run.
    def change_folder() -> None:
        acct.pack_forget()
        play_btn.configure(state="disabled")
        current = load_saved_root() or install_root(args.dir)
        path_var.set(str(current))
        install_panel.pack(fill="x", pady=(0, 4), before=play_btn)
        set_status("Choose where Hex should live, then Install here.")

    change_folder_btn = tk.Button(root, text="Change install folder", command=change_folder,
                                  font=("Georgia", 9), fg=DIM, bg=BG, bd=0,
                                  activebackground=BG, activeforeground=FG,
                                  highlightthickness=0, cursor="hand2")
    change_folder_btn.pack(side="bottom", pady=(0, 2))

    # ----- server picker (always visible; toggles live shard <-> beta) -----
    def _refresh_server_ui() -> None:
        beta = active_server() == "beta"
        server_btn.configure(text=f"Server: {server_name()}")
        header_label.configure(text="HEX BETA" if beta else "HEX",
                               fg=BETA_ACCENT if beta else ACCENT)
        root.title(f"Hex Launcher — BETA — {WEBSITE}" if beta
                   else f"{SHARD_NAME} Launcher — {WEBSITE}")

    def toggle_server() -> None:
        new = "beta" if active_server() == "hex" else "hex"
        set_active_server(new)
        save_server(new)
        _refresh_server_ui()
        # settings.json is one shared file, so the username saved in it may
        # belong to the other shard. If setup already finished, make the player
        # consciously log in for the shard they just picked (their saved creds
        # stay in the fields -- same creds is one press of Log In). Host/port are
        # re-pinned to the active profile at PLAY time (do_play -> write_settings).
        if state.get("settings_path"):
            show_existing()
            if new == "beta":
                set_status("Connected to the BETA shard — separate accounts and characters.")

    server_btn = tk.Button(root, text=f"Server: {server_name()}", command=toggle_server,
                           font=("Georgia", 9), fg=DIM, bg=BG, bd=0,
                           activebackground=BG, activeforeground=FG,
                           highlightthickness=0, cursor="hand2")
    server_btn.pack(side="bottom", pady=(0, 2))
    _refresh_server_ui()

    play_btn.configure(command=do_play)

    # ----- background setup -----
    def setup(root_dir: Path) -> None:
        try:
            client_dir = root_dir / "client"
            cuo_dir = root_dir / "classicuo"
            print(f"[*] Install folder: {root_dir}")

            set_status("Checking game files -- first install downloads ~1.6 GB ...")
            root.after(0, show_busy)

            # Fold the client zip into the grand total up front, so the bar
            # covers the entire setup and only ever reaches 100% once.
            if args.update_cuo or find_cuo_executable(cuo_dir) is None:
                asset = CUO_ASSETS.get(platform.system())
                if asset:
                    size = _remote_size(CUO_DOWNLOAD.format(asset=asset))
                    if size:
                        prog.add_cuo_total(size)

            ensure_game_files(client_dir, skip=args.skip_patch)
            cuo_exe = ensure_classicuo(cuo_dir, force=args.update_cuo, progress=prog)
            settings_path = write_settings(cuo_exe, client_dir)

            state["cuo_exe"] = cuo_exe
            state["client_dir"] = client_dir
            state["settings_path"] = settings_path

            def finish():
                state["setup_running"] = False
                hide_progress()
                if saved_username(settings_path):
                    account_done()
                else:
                    show_existing()

            root.after(0, finish)
        except Exception as exc:  # noqa: BLE001 -- show players a message, not a traceback
            state["setup_running"] = False
            print(f"[!] {exc}")
            set_status(f"Something went wrong: {exc} -- ask for help at {WEBSITE}.")

    def begin_setup(root_dir: Path) -> None:
        install_panel.pack_forget()
        save_root(root_dir)
        prog.reset()
        state["setup_running"] = True
        threading.Thread(target=setup, args=(root_dir,), daemon=True).start()

    install_btn.configure(command=lambda: begin_setup(Path(path_var.get())))

    def on_close() -> None:
        root.destroy()
        os._exit(0)  # downloader worker threads must never keep the process alive

    root.protocol("WM_DELETE_WINDOW", on_close)

    preset = Path(args.dir).expanduser() if args.dir else load_saved_root()
    if preset and not args.dir and not preset.exists():
        # The remembered folder is gone (moved drive, manual delete) --
        # ask again instead of silently reinstalling into the old path.
        set_status("Your old install folder is gone. Pick where to install.")
        path_var.set(str(preset))
        install_panel.pack(fill="x", pady=(0, 4), before=play_btn)
    elif preset:
        begin_setup(preset)
    else:
        set_status("Pick an install folder to get started.")
        path_var.set(str(install_root(None)))
        install_panel.pack(fill="x", pady=(0, 4), before=play_btn)

    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# Console fallback (--console)
# ---------------------------------------------------------------------------

def ensure_account_console(settings_path: Path) -> None:
    if saved_username(settings_path):
        return

    import getpass

    print(f"\n=== Set up your {server_name()} account ===")
    print("    Your EMAIL ADDRESS is your account name.\n")

    if input("    Already have an account? [y/N] ").strip().lower() == "y":
        save_username(settings_path, input("    Email address: ").strip())
        print("    OK -- type your password at the game's login screen.")
        return

    while True:
        email = input("    Email address: ").strip()
        if not _looks_like_email(email):
            print("    That doesn't look like an email address -- try again.")
        elif len(email) > 30:
            print("    Must be 30 characters or fewer (game client limit).")
        else:
            break

    while True:
        pw = getpass.getpass("    Choose a password: ")
        pw2 = getpass.getpass("    Type it again: ")
        if pw and pw == pw2:
            break
        print("    Passwords don't match (or were empty) -- try again.")

    invite = INVITE_CODE or input("    Invite code (Enter if none): ").strip()

    payload = {"email": email, "password": pw}
    if invite:
        payload["inviteCode"] = invite
    result = _api_post("/signup", payload)
    print(f"    {result.get('message', '')}")
    if not result.get("ok"):
        if "already exists" in result.get("message", ""):
            save_username(settings_path, email)
        return

    while True:
        code = input("    6-digit code from your inbox (or R to resend): ").strip()
        if code.lower() == "r":
            print(f"    {_api_post('/resend', {'email': email}).get('message', '')}")
            continue
        result = _api_post("/verify", {"email": email, "code": code})
        print(f"    {result.get('message', '')}")
        if result.get("ok"):
            break

    save_username(settings_path, email)
    print("\n    Account ready and verified! Type your password at the login screen")
    print("    and check 'Save Account' so you never type it again.\n")


def run_console(args) -> int:
    print(BANNER)
    root = install_root(args.dir)
    client_dir = root / "client"
    cuo_dir = root / "classicuo"
    print(f"[*] Install folder: {root}\n")

    ensure_game_files(client_dir, skip=args.skip_patch)
    cuo_exe = ensure_classicuo(cuo_dir, force=args.update_cuo)
    settings_path = write_settings(cuo_exe, client_dir)
    ensure_account_console(settings_path)

    if args.no_play:
        print("\n[*] Install/update complete (--no-play).")
        return 0

    launch(cuo_exe)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{SHARD_NAME} launcher ({WEBSITE})")
    parser.add_argument("--dir", help="install folder (default: HexUO in your user folder)")
    parser.add_argument("--skip-patch", action="store_true", help="skip the game-file check")
    parser.add_argument("--update-cuo", action="store_true", help="force re-download of ClassicUO")
    parser.add_argument("--no-play", action="store_true", help="install/update only, don't launch")
    parser.add_argument("--console", action="store_true", help="text mode instead of the window")
    parser.add_argument("--server", choices=("hex", "beta"),
                        help="which shard to use (overrides the saved choice; saves nothing)")
    parser.add_argument("--signup-test", metavar="SETTINGS", help=argparse.SUPPRESS)  # dev only
    args = parser.parse_args()

    # Active shard: an explicit --server wins and is not persisted; otherwise
    # honor the saved choice from the launcher config.
    set_active_server(args.server if args.server else load_saved_server())

    if args.signup_test:
        ensure_account_console(Path(args.signup_test).expanduser())
        return 0

    if args.console:
        return run_console(args)

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("[!] No display toolkit available -- falling back to text mode.")
        return run_console(args)

    return run_gui(args)


if __name__ == "__main__":
    # PyInstaller --windowed apps have no console: stdout/stderr are None on
    # Windows and any stray print() would crash invisibly. Give them a sink.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    try:
        code = main()
    except KeyboardInterrupt:
        print("\n[!] Cancelled.")
        code = 1
    except Exception as exc:  # noqa: BLE001 -- show players a message, not a traceback
        print(f"\n[!] Something went wrong: {exc}")
        print(f"    Ask for help at {WEBSITE} -- include a screenshot of this window.")
        code = 1
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input("\nPress Enter to close this window ...")
            except EOFError:
                pass
    sys.exit(code)
