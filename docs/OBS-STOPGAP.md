# OBS stopgap configuration (P1, 2026-10-21)

The OBS 32.2.2 replay-buffer setup was reconfigured as a stopgap until the Segra fork replaces it.

## Backup

`C:\Users\jayvi\obs-studio-backup-20261021-0130` holds a full robocopy of `%APPDATA%\obs-studio`, taken
before any edit (43 files, 4,023,152 bytes, identical to the source at the time).

## What changed

Profile `Untitled`, `basic\profiles\Untitled\basic.ini` (edited while OBS was closed):

- `[Output] Mode=Advanced` (was `Simple`; the `[SimpleOutput]` section is untouched).
- `[AdvOut]`: `RecEncoder=obs_nvenc_av1_tex`, `RecTracks=15` (tracks 1-4), `RecFormat2=hybrid_mp4`,
  `RecRB=true`, `RecRBTime=180`, `RecRBSize=2048`, and `Track1Name..Track4Name` = Mix, Game, Discord, Mic.
- New `basic\profiles\Untitled\recordEncoder.json`: AV1 NVENC, VBR 20 Mbps (max 25), `keyint_sec=1`,
  preset p5, tune hq, multipass qres.
- Not changed: output folder `C:\Users\jayvi\Videos`, filename format `%CCYY-%MM-%DD %hh-%mm-%ss`, prefix
  `Replay` (advanced mode still reads `[SimpleOutput] RecRBPrefix`), and the Num9 `ReplayBuffer.Save` hotkey.

`plugin_config\obs-websocket\config.json`: `server_enabled=true`. Auth and the password are unchanged, and the
port is still 4455.

Scene `Scene` in `basic\scenes\Untitled.json` (changed live through obs-websocket). Sources from top to bottom:

| Source | Kind | Setting | Audio tracks |
|---|---|---|---|
| Discord Audio | app audio | `Discord:Chrome_WidgetWin_1:Discord.exe`, match exe | 3 |
| Game Audio - Wardogs | app audio | `Wardogs:UnrealWindow:WardogsClient-Win64-Shipping.exe` | 2 |
| Game Audio - Tarkov | app audio | `EscapeFromTarkov:UnityWndClass:EscapeFromTarkov.exe` | 2 |
| Audio Input Capture | mic (existing) | unchanged | none (it duplicated Mic/Aux) |
| Game Capture - Tarkov | game capture, window mode | Tarkov string above, match exe | - |
| Game Capture - Wardogs | game capture, window mode | Wardogs string above, match exe | - |
| Game Capture | game capture, any fullscreen (existing) | unchanged | - |
| Display Capture (fallback) | monitor capture | 27GL850 primary, method auto | - |

Global audio: Desktop Audio is on track 1 only, and Mic/Aux is on tracks 1 and 4. Track 1 is the full mix. App
audio is kept off track 1 so that game and Discord sound is not doubled. The video sources are bounded to fit the
2560x1440 canvas.

Startup shortcut: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\OBS Replay.lnk` runs `obs64.exe
--startreplaybuffer --minimize-to-tray --disable-shutdown-check`, starting in `C:\Program Files\obs-studio\bin\64bit`
and minimised. OBS 32.2.2 does not recognise `--disable-shutdown-check`. After an unclean exit, OBS shows a "Crash
Detected" dialog that blocks startup until someone clicks "Run in Normal Mode".

## Reverting

1. Close OBS.
2. Rename `%APPDATA%\obs-studio` to `obs-studio-reverted-<stamp>`.
3. Run `robocopy C:\Users\jayvi\obs-studio-backup-20261021-0130 %APPDATA%\obs-studio /E`. Do not use `/MIR`.
4. Delete or rename the `OBS Replay.lnk` startup shortcut. It did not exist before this change.
5. Start OBS.
