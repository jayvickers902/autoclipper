# Autopilot report (2026-10-21)

The unattended build ran on 2026-10-21 from 01:23 to about 14:30 (-03), across 4 sessions on the Pro
subscription. All tasks in PLAN.md are done (P0–P5, R1, R2). What's left needs you; see
[.autopilot/NEEDS-JAY.md](../.autopilot/NEEDS-JAY.md), summarized below.

## What you have now

| Piece | State | Evidence |
|---|---|---|
| **OBS stopgap (P1)** | AV1, 4 audio tracks (1 mix, 2 game, 3 Discord, 4 mic), 180 s replay buffer, per-game captures plus a Display Capture fallback. Backup at `C:\Users\jayvi\obs-studio-backup-20261021-0130` | `docs/OBS-STOPGAP.md` (includes revert steps). In-game hooking is unproven |
| **Hub (P2)** | The `autoclipper` systemd service runs, watches `C:\Users\jayvi\Videos`, and does this per replay: dead-capture check → loudness/onsets/whisper/keywords/AudioSet tags → candidate windows → Haiku judge via `claude -p` → clip render (NVENC, libx264 while a game runs) | `systemctl status autoclipper`, `journalctl -u autoclipper -e` |
| **Review page** | http://autoclipper.localhost/ (127.0.0.1:4180): replays, clips, labelling, `/search` | P2.6, P5.2 |
| **Backfill** | 32 replays (4 dead), 56 candidates, 31 kept and rendered, 17 min | `docs/BACKFILL-REPORT.md` |
| **Judge comparison** | Haiku with keep at score ≥ 7 gets 0.88 agreement with Sonnet (the default). Borderline Sonnet re-judge is off. The local ollama judge isn't a usable fallback (qwen3:8b: kappa 0.10, keeps 88 %) | `docs/JUDGE-COMPARISON.md` |
| **Discord poster** | Built and tested. **Off**: there is no `.env` webhook yet, so nothing was posted | P2.7 |
| **Recorder (P3)** | Segra v1.9.4 fork at `C:\projects\autoclipper-recorder` (branch `autoclipper`, upstream push disabled): Tarkov raid bookmarks from EFT logs, AV1-first defaults with separate tracks, hub uploader instead of segra.tv, upstream auto-update disabled. Built but not installed | `RECORDER.md`, `dotnet test` 75 passed |
| **No-button mode (P4)** | Long-recording ingest (chunked, EFT raid segments and map labels, OBS chapters and Segra bookmarks as hints), and a report-only retention plan (no delete code) | `autoclipper ingest`, `autoclipper retention` |
| **Group features (P5)** | Multi-POV audio alignment (`autoclipper align A B`), FTS5 search, and a design for the dudgy bot | `docs/BOT-INTEGRATION.md` |

Tests: `uv run pytest -q -m 'not real_media'` gives 306 passed. The `real_media` tests also pass, except
two timing tests that fail only while a game loads the CPU.

## Costs

- **Money: $0.** Everything ran on the Pro subscription. There is no API key; the service unit unsets
  `ANTHROPIC_*`.
- **Runtime judge (ledger `data/judge-ledger.sqlite`):**
  - Haiku: 60 calls out of the run's 300 cap.
  - Sonnet: 28 calls out of 40, all for the P2.9 comparison.
  - About 1.22M input and 73K output tokens, $1.32 at API prices.
- **Weekly usage:** 17% at the start, 37% before the last session, against the supervisor's 70% stop.
- **Disk:** about 1 GB of clips in `C:\Users\jayvi\Videos\autoclipper\`. Your recordings were not
  modified.

## R2 final review: findings and what happened to them

| # | Finding | Outcome |
|---|---|---|
| 1 | BLOCKER: when a judge cap was hit, the pipeline kept calling ollama and threw the verdicts away. The run caps also counted every row ever, so after 300 the service would never judge again | **Fixed** (b789e89). Caps count as a pause, the pipeline never calls ollama, and ollama is GPU-gated elsewhere. Caps now count from a run epoch: run `uv run python -m autoclipper.judge new-run` to start a fresh budget |
| 2 | obs-websocket was turned on (the backup had it off) and listens on all interfaces | **Ticketed** in NEEDS-JAY (untick it in OBS). The hub doesn't use it, and I didn't want to force-restart OBS unattended |
| 3 | The weekly guard failed open when usage fields were missing | **Fixed**: it pauses instead |
| 4 | Two recordings with the same name overwrote each other's clips | **Fixed**: new clips go to `<stem>-<sha8>/`. Old clips keep their paths |
| 5 | The recorder would auto-update to upstream Segra | **Fixed** (recorder 0722634) |
| 6 | A judge error after the call skipped the pause checks | **Fixed**: `record()` runs in `finally` |
| 7 | A weekly-limit pause without a reset time lasted only 15 min | **Fixed**: 6 h |
| 8 | The upload inbox wasn't checked by `assert_writable` | **Fixed** at receiver startup |
| 9 | The Sonnet polish step is never called by the pipeline or the poster | **Open**: left off to save Sonnet usage. Wire it into the poster when posting goes live |

The review verified:
- no secrets in either repo's history, and `.env` and `data/` are gitignored;
- no delete path touches recordings;
- the guard reserves a ledger row before each call, under a cross-process lock;
- the review and upload servers check Host/Origin and reject path traversal.

## What needs you (details and exact steps in NEEDS-JAY.md)

1. **Discord webhook.** Create `.env` with `DISCORD_TEST_WEBHOOK_URL`, then
   `uv run autoclipper notify "hello"`. Posting stays off until then; to turn it on, add `--post` to the
   service's `run` line.
2. **Check OBS in-game.** Play Tarkov and Wardogs, press Num9, and confirm the replay isn't black and its
   tracks are right.
3. **Turn off obs-websocket** in OBS (Tools > WebSocket Server Settings).
4. **q and the Epic launcher.** Add the `!Epic...` lines to `q-games.txt` so q stops standing down.
5. **Try the recorder in a raid.** Install the Segra fork, play a raid, and check bookmarks and tracks.
   Uploading to the hub is opt-in: claim the port, set tokens and add a firewall rule.
6. **After the autopilot:** start a new judge budget with `uv run python -m autoclipper.judge new-run`.

## How to run it

```sh
cd ~/projects/autoclipper
sudo systemctl status autoclipper          # the hub (watch loop + review page)
journalctl -u autoclipper -e               # its log
uv run autoclipper report                  # counts, keeps, timings, judge calls
uv run autoclipper search "headshot"       # find clips
uv run autoclipper ingest <file-or-dir> --eft-logs   # a long recording
uv run autoclipper retention               # what retention would remove (report only)
uv run python -m autoclipper.judge status  # guard: pause, caps, usage
```

Review page: http://autoclipper.localhost/. Clips: `C:\Users\jayvi\Videos\autoclipper\`.
After changing code, run `sudo systemctl restart autoclipper`.
