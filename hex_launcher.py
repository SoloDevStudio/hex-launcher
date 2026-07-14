#!/usr/bin/env python3
"""Hex Launcher -- install, update, and play Hex (uohex.com) in one click.

What it does, in order:
  1. Downloads/verifies the freely-distributed UO client files, straight from
     the publisher's official patch servers (via the vendored MIT-licensed
     uo-patcher core -- https://github.com/andrezaiats/uo-patcher). The
     launcher never redistributes game assets: every player downloads their
     own copy from the official source, exactly as the official installer
     would.
  2. Downloads the open-source ClassicUO client (official GitHub release).
  3. Writes ClassicUO's settings to point at the Hex shard.
  4. Launches the game.

Re-running the launcher verifies/updates everything and plays again --
it is the patcher from then on. Your saved login is never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import stat
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import uo_patcher

# ---------------------------------------------------------------------------
# Shard configuration -- the only lines that make this "the Hex launcher"
# ---------------------------------------------------------------------------

SHARD_NAME = "Hex"
WEBSITE = "uohex.com"
SERVER_HOST = "play.uohex.com"
SERVER_PORT = 2593
CLIENT_VERSION = "7.0.116.0"

CUO_DOWNLOAD = (
    "https://github.com/ClassicUO/ClassicUO/releases/download/"
    "ClassicUO-main-release/{asset}"
)
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

def _download_with_progress(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "HexLauncher/1.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
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
                print(f"    {done * 100 // total}%", end="\r", flush=True)
                next_mark += 10
    print()


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


# ---------------------------------------------------------------------------
# Step 4: play
# ---------------------------------------------------------------------------

def launch(cuo_exe: Path) -> None:
    print(f"\n[*] Launching {SHARD_NAME} ...")
    print("    First time? Type the username and password you WANT at the")
    print("    login screen -- that creates your account. Write it down:")
    print("    whatever you type first IS your password.\n")
    subprocess.Popen([str(cuo_exe)], cwd=str(cuo_exe.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{SHARD_NAME} launcher ({WEBSITE})")
    parser.add_argument("--dir", help="install folder (default: HexUO in your user folder)")
    parser.add_argument("--skip-patch", action="store_true", help="skip the game-file check")
    parser.add_argument("--update-cuo", action="store_true", help="force re-download of ClassicUO")
    parser.add_argument("--no-play", action="store_true", help="install/update only, don't launch")
    args = parser.parse_args()

    print(BANNER)
    root = install_root(args.dir)
    client_dir = root / "client"
    cuo_dir = root / "classicuo"
    print(f"[*] Install folder: {root}\n")

    ensure_game_files(client_dir, skip=args.skip_patch)
    cuo_exe = ensure_classicuo(cuo_dir, force=args.update_cuo)
    write_settings(cuo_exe, client_dir)

    if args.no_play:
        print("\n[*] Install/update complete (--no-play).")
        return 0

    launch(cuo_exe)
    return 0


if __name__ == "__main__":
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
