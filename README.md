# Hex Launcher

One-click installer, updater, and launcher for **Hex** (https://uohex.com), an
Ultima Online private server tuned for the Renaissance era.

Download `HexLauncher.exe` from the [latest release](../../releases/latest),
run it, and press Play. It:

1. Downloads the freely-distributed UO client files **directly from the
   publisher's official patch servers** (first run ~1.6 GB; later runs only
   fetch what changed, and verify/repair your install).
2. Downloads the open-source [ClassicUO](https://github.com/ClassicUO/ClassicUO)
   client from its official release.
3. Configures ClassicUO for the Hex shard (`play.uohex.com:2593`).
4. Launches the game. Re-run it any time to update and play again.

Everything installs under `HexUO` in your user folder — no admin rights, no
registry, no installer. Delete that folder to uninstall.

## Legal posture

This launcher redistributes **no game assets**. The Ultima Online Classic
Client files are distributed free of charge by Electronic Arts / Broadsword
Online Games from public, unauthenticated patch servers; the launcher simply
downloads each player's own copy from that official source — the same files
the official installer delivers. The patching core is the MIT-licensed
[uo-patcher](https://github.com/andrezaiats/uo-patcher) by Andre Zaiats,
vendored as `uo_patcher.py`.

## Development

```bash
python3 hex_launcher.py            # install/update + play
python3 hex_launcher.py --no-play  # install/update only
python3 hex_launcher.py --dir /path/to/somewhere
```

Releases are built by GitHub Actions (`.github/workflows/build.yml`) —
tag `v*` to publish `HexLauncher.exe` (Windows) and `HexLauncher-mac`.
