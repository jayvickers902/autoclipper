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

## obs-websocket listens on all interfaces (port 4455): turn it off
P1 turned the OBS websocket server on to verify the setup. The backup had it off, and the hub never uses it, but OBS 32 binds it to all interfaces (LAN/Tailscale; auth is still on). The autopilot left it on rather than force-restart OBS unattended.
1. In OBS: Tools > WebSocket Server Settings > untick "Enable WebSocket server" > OK.
(Alternative if you want to keep it: in an admin PowerShell, `New-NetFirewallRule -DisplayName "OBS websocket local only" -Direction Inbound -LocalPort 4455 -Protocol TCP -RemoteAddress Any -Action Block -Profile Any`.)

## q thinks the Epic launcher is a game
`q` stood down all run because `EpicOnlineServicesUserHelper.exe` (under `Epic Games\`) matches its auto-detect. If the Epic launcher stays running, add these to `C:\Users\jayvi\bin\q-games.txt`:
`!EpicOnlineServicesUserHelper.exe`, `!EOSOverlayRenderer-Win64-Shipping.exe`, `!EpicGamesLauncher.exe`, `!EpicWebHelper.exe`

## Recorder (Segra fork): install and test in-game (P3.2-P3.4)
Built from `C:\projects\autoclipper-recorder` (branch `autoclipper`); build steps in its RECORDER.md (rebuild the frontend first so the new defaults show).
1. Install/run the published Segra.exe. If you already used Segra, your saved settings win over the new defaults: in Settings pick the AV1 NVENC encoder, turn on separate audio tracks and set voice chat to Game + Discord.
2. Play a Tarkov raid: check the raid gets start/map/end bookmarks, and the recording has tracks 1 mix, 2 mic, 3 game, 4 voice chat.
3. To upload to the hub (off now): `cd ~/projects/autoclipper && ports claim autoclipper-upload`, then in `.env` set `AUTOCLIPPER_UPLOAD_PORT` to that port, the bind address (your Tailscale/LAN IP) and the upload token(s) (see docs/UPLOADER.md), run `uv run python -m autoclipper.uploads`, allow the port in Windows Firewall for your Tailscale range only, and put the hub URL + token into Segra's settings.json (`hubUploadUrl`, `hubUploadToken`).

## After the autopilot: fresh judge budget
The hub's judge caps (300 Haiku / 40 Sonnet) were sized for the autopilot run and count from a "run epoch" (unset = all calls so far: 60 Haiku, 28 Sonnet). When you take over, start a new budget:
`cd ~/projects/autoclipper && uv run python -m autoclipper.judge new-run`. The weekly-usage guard (pause at 60 % of the week) still applies.

## Open from the final review
The Sonnet "polish" step (better titles for posted clips) is built but never called by the pipeline or poster. Wire it into the poster when you turn posting on, if you want it. Full review outcome: `docs/AUTOPILOT-REPORT.md`.
