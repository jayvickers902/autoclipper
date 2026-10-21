# Needs Jay

These are things the autopilot can't do alone. Each item says exactly what to do and why.



## Discord test webhook (posting is off until this is done)
`.env` doesn't exist, so `DISCORD_TEST_WEBHOOK_URL` is unset and every Discord post (P2.7 test clips, phase status lines) is skipped.
1. In a private Discord channel: Edit Channel > Integrations > Webhooks > New Webhook > Copy Webhook URL.
2. `cp ~/projects/autoclipper/.env.example ~/projects/autoclipper/.env && chmod 600 ~/projects/autoclipper/.env`
3. Paste the URL after `DISCORD_TEST_WEBHOOK_URL=`.
4. Test: `cd ~/projects/autoclipper && uv run autoclipper notify "hello from autoclipper"`.

## P1 OBS stopgap: check it in-game (5 min)
OBS is reconfigured (AV1, 4 tracks, 180 s buffer, per-game captures). Details and revert steps: `docs/OBS-STOPGAP.md`. Backup: `C:\Users\jayvi\obs-studio-backup-20261021-0130`.
The autopilot's test replay was black only because the Windows screensaver was on, so hooking is unproven.
1. Launch Tarkov, get in-game, press Num9. Do the same in Wardogs with Discord voice active.
2. Check the newest `C:\Users\jayvi\Videos\Replay *.mp4` isn't black: `uv run python -c "from autoclipper.media import is_dead_capture as d; print(d('/mnt/c/Users/jayvi/Videos/<file>'))"` from ~/projects/autoclipper.
3. In VLC, Audio > Audio Track: 1 = everything, 2 = game only, 3 = Discord only, 4 = your mic only.
If it's black, in OBS move "Display Capture" to the top of the scene for that game.

## OBS startup: crash dialog can block the buffer
OBS 32.2.2 ignores `--disable-shutdown-check`. After an unclean exit (forced close, power loss), a "Crash Detected / Run in Safe Mode?" dialog blocks the Startup shortcut and the replay buffer doesn't start. Fix when it appears: click "Run in Normal Mode". Always quit OBS from the tray (File > Exit) to avoid it.

## obs-websocket listens on all interfaces (port 4455)
OBS 32 has no localhost-only option, so it's reachable over LAN/Tailscale (auth is still on). To restrict it, run in an admin PowerShell:
`New-NetFirewallRule -DisplayName "OBS websocket local only" -Direction Inbound -LocalPort 4455 -Protocol TCP -RemoteAddress Any -Action Block -Profile Any` (localhost traffic doesn't pass the firewall, so the hub keeps working).

## q thinks the Epic launcher is a game
`q` stood down all run because `EpicOnlineServicesUserHelper.exe` (under `Epic Games\`) matches its auto-detect. If the Epic launcher stays running, add these to `C:\Users\jayvi\bin\q-games.txt`:
`!EpicOnlineServicesUserHelper.exe`, `!EOSOverlayRenderer-Win64-Shipping.exe`, `!EpicGamesLauncher.exe`, `!EpicWebHelper.exe`
