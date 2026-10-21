# Autoclipper — design

Written 2026-10-21 by the planning session. Facts in "Verified" were checked on jaybox that day.

## The problem

Jay and his friends save gameplay clips with OBS's replay buffer, mostly in Escape from Tarkov (BSG
launcher) and WARDOGS (Steam, Unreal). In Jay's words, the pain points are:

1. trimming clips by hand;
2. not knowing what a clip is without watching it;
3. having to run OBS and remember the replay buffer with the right source set;
4. pressing a button is acceptable, but fully automatic is the goal.

No new subscriptions or per-use bills. Jay's ceiling is $20/month for the first 2 test users, and the
design needs $0: Claude judging runs on his existing Claude subscription through the `claude` CLI (Jay,
2026-10-21).

## Verified facts about the current setup

- **OBS 32.2.2** (profile `Untitled`, scene collection `Untitled.json`):
  - Simple output; replay buffer 60 s, saved with Num9; NVENC H.264 "High Quality" at 2560x1440 60 fps
    (the latest normal replay is 401 MB for 59 s, about 54 Mbps); Hybrid MP4; one mixed audio track.
  - Sources: mic (`wasapi_input_capture`) and one `game_capture` in `any_fullscreen` mode.
  - obs-websocket is disabled (port 4455, auth on).
  - Output goes to `C:\Users\jayvi\Videos` as `Replay %CCYY-%MM-%DD %hh-%mm-%ss.mp4`.
- **The black clips are real.** `Replay 2026-10-20 13-50-58.mp4` (57 s, 4 MB, 0.5 Mbps) is solid black:
  every sampled frame has mean luma 16, so the capture never hooked. `Replay 2026-10-20 09-01-16.mp4`
  (4 MB) looks the same.
- **Existing clips:** 35 replays (2.7 GB) in `C:\Users\jayvi\Videos`; the `trimmed\` and `trimmer\` folders
  are manual trimming.
- **Hardware:**
  - RTX 4070 Ti 12 GB (NVENC, including AV1), Ryzen 7 7700X, 31 GB RAM.
  - C: has 88 GB free; WSL ext4 has about 930 GB.
  - Windows sleeps after 30 min idle on AC; the autopilot keeps it awake while it runs.
- **EFT logs** live at `C:\Battlestate Games\Escape from Tarkov\Logs\log_<date>_<ver>\<stamp> application_000.log`.
  Useful lines:
  - `GameStarting:` and `GameStarted:` mark raid start.
  - `TRACE-NetworkGameCreate profileStatus: ... RaidMode: Online, Ip: <ip>, Port: <port>, Location: TarkovStreets`
    gives the map and the raid server. The server address should identify the same raid across friends
    (unconfirmed).
  - `Matching with group id: <id>` and `Network game matching cancelled.`
  - After a raid the client returns to menu: `PrepareSelectedProfileLocally`, GC cleanup, BattlEye re-init.
  - EFT has no kill feed and logs no kills.
- **WARDOGS:** the exe is `WardogsClient-Win64-Shipping.exe`. `%LOCALAPPDATA%\Wardogs\Saved\Logs` is empty
  (shipping build), but kills can be read off the screen (see Segra below).
- **Local models:** ollama on Windows (127.0.0.1:11434) has `qwen3:8b` and `qwen3.8-27b`. The 27B has a
  vision projector but spills past 12 GB VRAM, so it is slow.
- **Code to borrow from** (read-only references; don't edit these repos):
  - cs-analytics: the dudgy Discord bot, webhook clip posts with thumbnails and @mentions, the
    `STEAM_TO_DISCORD` map, R2 clips at `cstrike.dudgy.net/clips`.
  - frag-movie: a beat-synced montage editor, plus the Windows render-host agent and tray pattern.
  - tarkov-squad-planner: a Tauri companion that imports EFT logs.
  - clip-trimmer: `find_action.py` sends frames to Claude, picks one peak and makes a fixed 30 s cut.
    It uses frames only, no audio.

## Architecture

### 1. Recorder on each PC: a fork of Segra

[Segra](https://github.com/Segergren/Segra) is GPL-2.0, written in C# on .NET 10 with libobs through
ObsKit.NET; v1.9.4 shipped 2026-10-14. It already covers most of the capture side:

- `Backend/Games/GameDetectionService.cs` detects games and starts recording, with no OBS window.
- `Backend/Recorder/OBSService.cs` has Session, Hybrid and replay-buffer modes, NVENC/AMF/QSV encoding to
  H.264, HEVC or AV1, and separate audio tracks.
- Integrations in `Backend/Games/*` add `Bookmark`s (`Kill`, `Death`, …). Two base classes:
  - `OcrIntegration` runs Windows.Media.Ocr on a crop region;
  - `LogTailIntegration` tails one log file.
- `Wardogs/WardogsIntegration.cs` uses an OCR crop (X 0.36, Y 0.68, W 0.28, H 0.25): `CONFIRMED`
  (excluding `ASSIST`) becomes a Kill, and `CALL FOR HELP` becomes a Death.
- `Backend/Media/HighlightService.cs` cuts segments around highlight bookmarks.
- `Backend/Media/UploadService.cs` uploads to segra.tv; this gets replaced.
- Build: `dotnet publish Segra.csproj -c Release -f net10.0-windows10.0.19041.0 -r win-x64`, a Node 24
  frontend, and Velopack (`vpk`) packaging. See `.github/workflows/create-release.yml`.

What we add:
- `TarkovIntegration`, a log tail for raid start, raid end and map;
- an audio track layout of game, Discord and mic;
- Session or Hybrid defaults for Tarkov and Wardogs;
- an uploader that sends to the hub.

The fork lives in `C:\projects\autoclipper-recorder`. It is a Windows-only repo, so use `wgit`, and the
upstream push URL is disabled. Until the recorder ships, OBS keeps working with the P1 stopgap config.

### 2. Hub on jaybox (WSL, Python, systemd unit `autoclipper`)

The hub is a funnel, so the paid step only sees good candidates.

1. **Signals (local, $0).** In Tarkov, the audio is the kill feed.
   - Black and frozen-frame detection.
   - Per-second loudness and onset density.
   - AudioSet tags (gunshot, explosion, laughter, shout, speech) from PANNs, BEATs or CLAP.
   - A faster-whisper transcript with VAD and word timestamps.
   - Keyword spotting ("clip that", "no way", …).
   - Bookmarks: OBS chapter markers, Segra bookmarks and EFT raid events.
2. **Candidates.** The signals fuse into an interest curve.
   - Each window starts about 6 s before a fight's first shot and ends about 4 s after its last event.
   - Overlapping windows merge, windows are capped at 45 s, and their ends snap to silence.
3. **Judge.** It calls Claude through the `claude` CLI on Jay's subscription; see "Runtime judge" below.
   - Claude Haiku 5.5 sees 8–12 frames (≤ 1024 px), the event timeline and the transcript. It returns
     JSON: keep, score, type, title, summary, in and out points, and a thumbnail time.
   - Claude Sonnet 5.5 polishes the clips that get posted.
   - Local ollama models take over while Claude judging is paused by a usage limit or failing.
4. **Outputs.**
   - An H.264 1080p60 share encode of ≤ 19 MB, a thumbnail and a 3 s preview.
   - A SQLite store.
   - A review page at `http://autoclipper.localhost/`.
   - A Discord post.

GPU stages run only when no game is running, using the rules in `C:\Users\jayvi\bin\q-games.txt`.

### 3. Sharing

- **Discord first.**
  - During the autopilot run, posts go only to Jay's private test webhook.
  - Later they go through the dudgy bot (cs-analytics), with @mentions, reactions as taste signals,
    `/clips search` and a clip of the week.
- **Attachments stay ≤ ~19 MB.** Discord raised the free upload cap from 10 to 20 MB on 2026-08-14. An
  older report shows webhooks capped lower, so the poster measures the real limit and falls back to
  ≤ 9.5 MB.
- **R2 isn't used yet.** The `fragvid` bucket (about 40 GB) already exceeds the 10 GB free tier, so every
  extra GB stored costs about $0.015 a month.

## Runtime judge: Claude through the CLI, on Jay's subscription

The hub shells out to the `claude` CLI, which is signed in with Jay's Claude subscription. It has no API key
and no Anthropic SDK. Verified on jaybox on 2026-10-21 with Claude Code 2.1.306, signed in through
claude.ai on the `pro` plan with extra usage off:

- **One process per call:**
  `claude -p --model claude-haiku-5-5 --safe-mode --tools "" --strict-mcp-config --no-session-persistence
  --system-prompt <prompt> --json-schema <schema> --input-format stream-json --output-format stream-json --verbose`
  - Frames go as base64 `image` blocks in one stream-json user message on stdin. Run it from an empty
    working directory with a minimal environment (HOME, PATH), dropping inherited `CLAUDECODE` and
    `CLAUDE_CODE_*` variables. Launching it from inside a Claude Code session works.
  - `--safe-mode` skips CLAUDE.md, skills, plugins, hooks and MCP servers, and `--tools ""` removes the
    tools, so a request holds only the prompt and frames. Don't use `--bare`: it ignores the
    subscription login.
- **Test call.** Six 1024 px frames from `Replay 2026-10-19 23-34-12.mp4`:
  - plain JSON: 4.0 s, 6.2K input and 0.6K output tokens, notional $0.0016;
  - with `--json-schema`: 5.9 s, 13.2K input and 1.2K output tokens (it adds a tool round trip), notional
    $0.0021, and the answer arrives schema-valid in `structured_output`;
  - both named the game correctly (Squad) and wrote a usable title.
- **Reading the stream.** Each call emits a `rate_limit_event` with:
  - `status`: `allowed` or `rejected`;
  - `resetsAt`, in epoch seconds;
  - `unifiedWindows.five_hour` and `unifiedWindows.seven_day` utilization, from 0 to 1.

  The final `result` line carries the answer, `usage` and a notional `total_cost_usd`. That's the API
  price; nothing is billed.
- **Limits.** Judging shares the 5-hour and weekly limits with Jay's own Claude use, this autopilot
  included. So:
  - calls run one at a time, capped per hour;
  - Claude judging pauses until `resetsAt` when a call is rejected, or when 5-hour utilization reaches
    `AUTOCLIPPER_JUDGE_MAX_5H`;
  - meanwhile local ollama models judge, and Claude re-judges those clips after the reset.
- **Only jaybox needs Claude.** Friends' recorders upload to the hub, and the hub does all judging.

## Hard lines

- **No game memory or injection.** Never read EFT or Wardogs memory or inject into either game. BattlEye
  and the other anti-cheat systems ban for that, and the bans would hit Jay and his friends too. Use
  screen OCR, log files, audio and the player's own input only.
- **Consent for voices.** Comms audio records friends, so "include my voice" is a per-person setting.
- **Originals are read-only.** Autoclipper never modifies, moves or deletes original recordings.

## Later

- **Multi-POV clips:** align players by cross-correlating one player's mic with another's Discord track.
- **A taste model** learned from reactions.
- **A nightly recap montage** made with frag-movie.
- **Presigned R2 uploads for friends.** Cloudflare's free plan rejects request bodies over 100 MB, so
  video can't go through tunnels.
- **Steam background recordings as a zero-install fallback recorder.** They are MPEG-DASH (`session.mpd`
  plus `.m4s` segments), and ffmpeg reads them directly.
