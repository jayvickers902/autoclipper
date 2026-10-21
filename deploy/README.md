# Deploying the hub

`autoclipper.service` runs `autoclipper run`: the review page (port 4180) and the replay watcher in one
process. Add `--post` to `ExecStart` to post newly kept clips to the Discord test webhook (the persistent
5-clip cap applies). Secrets live in `/home/jay/projects/autoclipper/.env`, never in the unit.

## Install

```bash
systemd-analyze verify deploy/autoclipper.service
sudo cp deploy/autoclipper.service /etc/systemd/system/autoclipper.service
sudo systemctl daemon-reload
sudo systemctl enable --now autoclipper
journalctl -u autoclipper -e
```

Review page: http://autoclipper.localhost/ (or http://127.0.0.1:4180/). Stop other `serve`/`watch`/`run`
processes first: the port is shared and a second `run` fails with "is taken".

## Uninstall

```bash
sudo systemctl disable --now autoclipper
sudo rm /etc/systemd/system/autoclipper.service
sudo systemctl daemon-reload
```

## Notes

- Stopping (`systemctl stop`) sends SIGTERM: the current replay finishes (up to `TimeoutStopSec`), the HTTP
  server closes, exit 0. A killed run resumes at the first unfinished stage.
- GPU gate: while a game from `q-games.txt` runs (one `powershell.exe Get-Process`, cached 30 s) share
  encodes use libx264 on the CPU. If the check fails it is logged once and a game is assumed. Override the
  list with `AUTOCLIPPER_GAMES_FILE`.
- After editing the unit: `sudo cp` again, `sudo systemctl daemon-reload`, `sudo systemctl restart autoclipper`.
