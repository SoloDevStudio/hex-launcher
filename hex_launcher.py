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
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import uo_patcher

# ---------------------------------------------------------------------------
# Shard configuration -- the only lines that make this "the Hex launcher"
# ---------------------------------------------------------------------------

SHARD_NAME = "Hex"
WEBSITE = "uohex.com"
# HEX_SERVER / HEX_PORT / HEX_API env vars override the shard endpoints (dev/test use).
SERVER_HOST = os.environ.get("HEX_SERVER", "play.uohex.com")
SERVER_PORT = int(os.environ.get("HEX_PORT", "2593"))
API_URL = os.environ.get("HEX_API", f"https://{WEBSITE}/api")
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


def load_saved_root() -> Path | None:
    try:
        saved = json.loads(_launcher_config_path().read_text(encoding="utf-8")).get("install_root")
        return Path(saved) if saved else None
    except (OSError, json.JSONDecodeError):
        return None


def save_root(root: Path) -> None:
    try:
        _launcher_config_path().write_text(
            json.dumps({"install_root": str(root)}, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # non-fatal: worst case the picker shows again next run


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
    uo_patcher.run_patch(str(client_dir))


# ---------------------------------------------------------------------------
# Step 2: ClassicUO
# ---------------------------------------------------------------------------

def _download_with_progress(url: str, dest: Path, attempts: int = 3) -> None:
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HexLauncher/1.0"})
            # timeout guards against a stalled connection hanging setup forever.
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                next_mark = 10
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total and done * 100 // total >= next_mark:
                        print(f"    {done * 100 // total}%")
                        next_mark += 10
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
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


def ensure_classicuo(cuo_dir: Path, force: bool) -> Path:
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
    _download_with_progress(url, zip_path)

    print("[*] Extracting ...")
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
            "ip": SERVER_HOST,
            "port": SERVER_PORT,
            "ultimaonlinedirectory": str(client_dir),
            "clientversion": CLIENT_VERSION,
            "last_server_name": SHARD_NAME,
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
        f"{API_URL}{endpoint}",
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
    print(f"\n[*] Launching {SHARD_NAME} ...")
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

    state = {"cuo_exe": None, "settings_path": None, "email": "", "password": None}

    # ----- header -----
    tk.Label(root, text="HEX", font=("Georgia", 40, "bold"), fg=ACCENT, bg=BG).pack(pady=(18, 0))
    tk.Label(root, text=f"{WEBSITE}  ·  Ultima Online, Renaissance era", font=("Georgia", 13),
             fg=DIM, bg=BG).pack()

    # ----- status: one friendly line for players -----
    status_var = tk.StringVar(value="Checking your installation ...")
    tk.Label(root, textvariable=status_var, font=("Georgia", 14), fg=FG, bg=BG,
             wraplength=500, justify="center").pack(pady=(16, 4), padx=24)

    def set_status(msg: str) -> None:
        root.after(0, status_var.set, msg)

    # Technical output goes to a quiet log file for support -- no UI for it,
    # except download-progress lines, which drive the progress bar below.
    log_file = Path.home() / ".hexuo-launcher.log"

    _patch_progress = re.compile(r"\[(\d+)/(\d+) files\] \[([\d.]+)/([\d.]+) MB\] \[([\d.]+)%\]")
    _cuo_progress = re.compile(r"^\s*(\d{1,3})%\s*$")

    def pump_log() -> None:
        lines = []
        try:
            while True:
                lines.append(log_q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            for line in lines:
                m = _patch_progress.search(line)
                if m:
                    total_mb = float(m.group(4))
                    if total_mb <= 0:
                        status_var.set("Verifying game files ...")
                        show_busy()
                        continue
                    set_progress(float(m.group(5)))
                    status_var.set(
                        f"Downloading game files -- {float(m.group(3)):.0f} of {total_mb:.0f} MB ({float(m.group(5)):.0f}%)"
                    )
                    continue
                m = _cuo_progress.match(line)
                if m:
                    set_progress(int(m.group(1)))
                    status_var.set(f"Downloading the game client -- {m.group(1)}%")
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
    # Two modes: an indeterminate "working" sweep while checking/verifying (no byte
    # counts yet), and a real percentage bar once downloads report progress.
    progress_bar = ttk.Progressbar(root, mode="determinate", maximum=100)
    _progress_state = ["hidden"]  # hidden | busy | determinate

    def _ensure_shown() -> None:
        if _progress_state[0] == "hidden":
            progress_bar.pack(fill="x", padx=40, pady=(0, 8), before=play_btn)

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
            _progress_state[0] = "hidden"

    def busy(b: bool) -> None:
        action_btn.configure(state="disabled" if b else "normal")
        alt_btn.configure(state="disabled" if b else "normal")

    def _clear_form() -> None:
        verify_frame.pack_forget()
        signup_frame.pack_forget()
        forgot_btn.pack_forget()
        for box in (email_box, pw_box, pw2_box):
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

        def ok(_):
            show_verify()

        def fail(result):
            if "already exists" in result.get("message", ""):
                save_login(state["settings_path"], email, pw)
                account_done()

        api_call("/signup", {"email": email, "password": pw}, ok, fail)

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

    play_btn.configure(command=do_play)

    # ----- background setup -----
    def setup(root_dir: Path) -> None:
        try:
            client_dir = root_dir / "client"
            cuo_dir = root_dir / "classicuo"
            print(f"[*] Install folder: {root_dir}")

            set_status("Checking game files -- first install downloads ~1.6 GB ...")
            root.after(0, show_busy)
            ensure_game_files(client_dir, skip=args.skip_patch)
            set_status("Checking the game client ...")
            root.after(0, show_busy)
            cuo_exe = ensure_classicuo(cuo_dir, force=args.update_cuo)
            settings_path = write_settings(cuo_exe, client_dir)

            state["cuo_exe"] = cuo_exe
            state["settings_path"] = settings_path

            def finish():
                hide_progress()
                if saved_username(settings_path):
                    account_done()
                else:
                    show_existing()

            root.after(0, finish)
        except Exception as exc:  # noqa: BLE001 -- show players a message, not a traceback
            print(f"[!] {exc}")
            set_status(f"Something went wrong: {exc} -- ask for help at {WEBSITE}.")

    def begin_setup(root_dir: Path) -> None:
        install_panel.pack_forget()
        save_root(root_dir)
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

    print(f"\n=== Set up your {SHARD_NAME} account ===")
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

    result = _api_post("/signup", {"email": email, "password": pw})
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
    parser.add_argument("--signup-test", metavar="SETTINGS", help=argparse.SUPPRESS)  # dev only
    args = parser.parse_args()

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
