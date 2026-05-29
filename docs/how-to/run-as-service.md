# Run as a background service (macOS)

Keep Tunapi running in the background — starting at login and restarting on
crash — without hand-editing a launchd plist.

> Requires a working config (`~/.tunapi/tunapi.toml`). Run
> [`tunapi doctor`](troubleshooting.md) first if you're unsure.

## Install

From your Tunapi source checkout:

```sh
uv run tunapi service install
```

This:

- detects `uv`, Node (via `fnm` when present, so Node upgrades don't break
  it), and Homebrew, and writes a launcher at `~/.local/bin/tunapi-launcher.sh`;
- writes a launchd agent at `~/Library/LaunchAgents/com.tunapi.plist`
  (`RunAtLoad` + `KeepAlive`);
- loads it immediately. The bridge is now running.

Re-run with `--force` to regenerate after upgrading `uv`/Node, or if you move
the checkout.

## Manage

```sh
tunapi service status     # running? + last log lines
tunapi service logs -f    # follow the log (~/.tunapi/service.log)
tunapi service restart    # pick up new code/config
tunapi service stop       # stop until next login/start
tunapi service start      # start again
tunapi service uninstall  # unload + remove launcher and plist
```

## Notes

- Logs go to `~/.tunapi/service.log`.
- The launcher clears stale transport lock files (`~/.tunapi/*.lock`) on each
  start, so a hard crash + `KeepAlive` restart won't get stuck on a lock.
- After changing `tunapi.toml` or pulling new code, run
  `tunapi service restart`.
- Linux (systemd) is not supported yet; on other platforms run
  `uv run tunapi` under your own supervisor.
