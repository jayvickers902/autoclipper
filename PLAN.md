# Autoclipper build plan

This is the spec for the unattended autopilot run that starts 2026-10-21.

- Live status is in `.autopilot/STATE.md`.
- Edit this file only when the plan itself changes, and log why in `.autopilot/PROGRESS.md`.
- Design and verified facts are in `docs/DESIGN.md`.

## Model routing: who builds what

The run uses Jay's Claude **Pro** subscription. Its 5-hour and weekly limits decide how far the run gets,
not time. Heavier models use the limits up faster; by API price, Opus costs 2× Sonnet and Sonnet 20× Haiku.
Fable 5.1 isn't on Pro without usage credits, which are off. Pick the cheapest role that will get a task
right the first time.

| Role | Model | Effort | Used for |
|---|---|---|---|
| Orchestrator (the session) | Opus 5.5 | high | Picks tasks, writes briefs, verifies results, commits, keeps the state files. Delegates reading and building to keep its own context lean |
| `builder` subagent | Sonnet 5.5 | high | Well-specified implementation plus tests (most tasks) |
| `engineer` subagent | Opus 5.5 | high | Hard or risky work: OBS reconfiguration, signal fusion, judge prompts and eval, the judge's usage guard, Segra/libobs internals, escalated bugs |
| `reviewer` subagent | Opus 5.5 | xhigh | Only the R1 design review, the R2 final review, and one consult per stuck task. Read-only |
| `scout` subagent | Haiku 5.5 | low | Mechanical work: fixture extraction and redaction, inventories, doc drafts from notes |
| `q` (local qwen3:8b) | — | — | Summarizing noisy command output, at zero Claude usage |

Escalation:
1. A builder fails a task twice → hand it to an engineer.
2. The engineer fails twice → one reviewer consult.
3. Still stuck → write it in `.autopilot/NEEDS-JAY.md` and move on.

## Model routing: what the product calls at runtime

The judge runs on Jay's Claude subscription through the `claude` CLI, not the API, so it costs nothing
extra (Jay, 2026-10-21). DESIGN.md ("Runtime judge") has the invocation, verified on jaybox.

| Stage | Runs on | Model | Notes |
|---|---|---|---|
| Signals: black frames, audio tags, speech, keywords | jaybox GPU/CPU | local: ffmpeg, PANNs/BEATs or CLAP, faster-whisper | $0 |
| Judge every candidate window | `claude -p`, subscription | `claude-haiku-5-5`, 8–12 frames ≤ 1024 px plus a text timeline | ≈ 6–13K tokens and 4–6 s per call; notional API price ≈ $0.002 |
| Polish clips that get posted; re-judge borderline ones | `claude -p`, subscription | `claude-sonnet-5-5` | daily cap |
| Claude paused or failing | jaybox | ollama: `qwen3.8-27b` for vision, `qwen3:8b` for text | $0; Claude re-judges after the reset |

Guards, all in code:
- No API key and no Anthropic SDK anywhere in the hub.
- Calls run one at a time, within `AUTOCLIPPER_JUDGE_MAX_CALLS_PER_HOUR` and
  `AUTOCLIPPER_POLISH_MAX_CALLS_PER_DAY`.
- Each call's `rate_limit_event` is read. Claude judging pauses until `resetsAt` when a call is rejected or
  `five_hour.utilization` reaches `AUTOCLIPPER_JUDGE_MAX_5H` (0.9), so Jay keeps headroom for his own use.
- Every call's tokens, duration, notional cost and utilization go into a ledger in `data/`.
- This run: at most 300 Haiku and 40 Sonnet judge calls in total.

## Tasks

Status markers in STATE.md: `[ ]` todo, `[~]` in progress, `[x]` done, `[!]` needs Jay, `[-]` dropped.

### P0: Scaffold (orchestrator; builder for bulk)

**P0.1** Set up the repo:
- `git init` in `~/projects/autoclipper`; a uv project pinned to Python 3.12.
- `src/autoclipper/`, `tests/`, pytest and ruff, a README.
- Keep the existing `.gitignore`, `.env.example` and `docs/`.

*Done when* `uv run pytest -q` and `uv run ruff check` pass.

**P0.2** Run `ports claim autoclipper` and put the port in the config defaults and `.env.example`. Bind
127.0.0.1 and fail if the port is taken.

**P0.3** (builder) Write a script that generates fixtures into `tests/fixtures/`; don't commit binaries:
- clips: 10 s of black; a static clip; a "firefight" clip (testsrc plus transient bursts); a speech clip
  saying "clip that" (espeak-ng); a clip with 4 audio tracks;
- EFT log excerpts copied from Jay's logs, with profile IDs, account IDs and IPs redacted.

**R1** (reviewer) One call to review DESIGN.md, PLAN.md and the scaffold. Apply what's worth
applying and log the decisions in PROGRESS.md.

### P1: OBS stopgap (engineer). Jay authorized this

Goal: no more black clips, separate audio tracks, smaller files, and a buffer that's always on.

1. **Preconditions.**
   - Proceed only if no game is running.
   - Before touching anything, back up `%APPDATA%\obs-studio` to
     `C:\Users\jayvi\obs-studio-backup-<yyyymmdd-hhmm>` with `robocopy /E`. Never delete backups.
   - Close OBS gracefully; force it if it's still open after 20 s.
2. **Changes.** Drive OBS through obs-websocket rather than hand-editing JSON where possible. Past
   hooked-window strings are in the old OBS logs under `%APPDATA%\obs-studio\logs`.
   - A Game Capture per game, matched by executable: `EscapeFromTarkov.exe` and
     `WardogsClient-Win64-Shipping.exe`. Keep the existing "any fullscreen" source below them as a
     fallback for other games.
   - Application Audio Capture: the game exes on track 2, `Discord.exe` on track 3; the mic on tracks 1
     and 4; desktop audio on track 1 only, so track 1 stays the full mix.
   - Advanced output: Hybrid MP4, record tracks 1–4, AV1 NVENC, 1 s keyframes, a 180 s replay buffer,
     the same folder and `Replay` prefix, and the Num9 hotkey unchanged.
   - obs-websocket on (localhost, keep auth).
   - A Startup-folder shortcut running `obs64.exe --startreplaybuffer --minimize-to-tray --disable-shutdown-check`,
     with working directory `bin\64bit`.
3. **Verify.**
   - Relaunch OBS minimized and confirm through websocket that the buffer is active.
   - Wait 20 s and call `SaveReplayBuffer`.
   - ffprobe the new file: AV1 video, 4 audio tracks, keyframes about 1 s apart.
   - The OBS log shows no errors.
   - Move the test replay to `Videos\autoclipper\obs-test\`; don't delete it.
4. **On any failure:** restore the backup, relaunch OBS with the original config, and write the details
   to NEEDS-JAY.md.

`[!]` Jay: confirm in-game that Tarkov and Wardogs hook. The black-clip fix needs a real game.

### P2: Hub v1, the brain on existing replays (WSL, Python)

**P2.1** (builder) Media primitives:
- an ffprobe wrapper;
- black and frozen-frame detection, which must flag both 4 MB Oct-20 replays;
- per-track audio extraction to 16 kHz mono;
- keyframe-aware stream-copy cuts;
- a share encode: H.264 1080p60 ≤ 19 MB, using `h264_nvenc` if WSL's ffmpeg has it, otherwise libx264;
- a thumbnail and a 3 s WebP preview.

**P2.2** (engineer designs, builder implements) Signals, written as a per-second timeline JSON:
- loudness and onset density;
- AudioSet tags (gunshot, explosion, laughter, shout, speech) from PANNs/BEATs or CLAP;
- faster-whisper large-v3-turbo with VAD and word timestamps, on the GPU when it's free and the CPU
  otherwise;
- keyword spotting: "clip that", "clip it", "no way", "let's go", and similar.

It must work both on today's single-mix replays and on separate tracks.

**P2.3** (engineer) Candidate windows:
- fuse the signals into an interest curve;
- start about 6 s before a fight's first shot and end about 4 s after its last event;
- merge overlaps, cap at 45 s, snap the ends to silence;
- keep the top N per replay.

**P2.4** (engineer) Judge, through the `claude` CLI (DESIGN.md, "Runtime judge"):
- Haiku 5.5 gets frames, the timeline and the transcript in one stream-json message. Use `--json-schema`
  for `{keep, score, type, title (≤ 60 chars), summary (≤ 140 chars), start, end, thumb_t, game}`, and read
  the answer from `structured_output`.
- Sonnet 5.5 polishes clips that get posted and re-judges borderline ones.
- Treat an error result, a missing `structured_output` or a timeout as a failed call: retry once, then
  fall back.
- Build the guards and the ledger above, plus the local ollama judge as the fallback.
- Unit tests replace `claude` with a fake script that replays recorded stream-json. Live calls only within
  this run's call caps.

**P2.5** (builder) Store, pipeline and CLI:
- SQLite; the commands `process <file>`, `backfill <dir>`, `watch` and `report`; idempotent.
- Outputs go to `C:\Users\jayvi\Videos\autoclipper\`. Originals are read-only.

**P2.6** (builder) Review page served on the claimed port at `http://autoclipper.localhost/`:
- replays, then candidates, then kept clips, each with video, title, summary, score and cost;
- 👍/👎 labels saved to SQLite.

**P2.7** (builder) Discord poster:
- `DISCORD_TEST_WEBHOOK_URL` only. No other webhook, ever.
- Posts the title, game, length, summary, and the clip as an attachment ≤ 19 MB.
- On HTTP 413, re-encode to ≤ 9.5 MB and retry once; record the observed limit in DESIGN.md.
- Respect rate limits.
- A `notify <text>` CLI command for the autopilot's status lines.

**P2.8** (orchestrator) Backfill:
- Process the 35 replays and post the best ≤ 5 clips to the test webhook.
- Write `docs/BACKFILL-REPORT.md`: timings per stage, candidates, keeps, judge calls, tokens and seconds per
  candidate, how much of the 5-hour window the backfill used (from the ledger's utilization readings), and
  a projection for 2 users.

**P2.9** (engineer) Judge comparison, within this run's call caps:
- Compare Haiku alone, Haiku plus Sonnet polish, and the local judge, with Sonnet as the reference.
- Report agreement, calls and latency, recommend defaults, and leave the labeling page ready for Jay.

**P2.10** (builder) Service:
- `deploy/autoclipper.service` (systemd) runs `watch` and the review page as `jay`, with HOME and PATH set
  so the `claude` CLI finds Jay's login.
- GPU gate: no GPU stages while a game runs (the q-games.txt rules).
- Install and start it; posting goes only to the test webhook.

### P3: Recorder, the Segra fork (Windows)

**P3.1** (builder) Clone and build:
- If `dotnet --list-sdks` has no 10.x, install a per-user .NET 10 SDK with Microsoft's
  `dotnet-install.ps1` (no admin).
- `wgit clone` Segergren/Segra (tag v1.9.4, or main) into `C:\projects\autoclipper-recorder`; rename
  `origin` to `upstream` and set its push URL to `DISABLED`.
- Build the backend and the Node 24 frontend as `.github/workflows/create-release.yml` does.
- Record the steps in `RECORDER.md`.

**P3.2** (builder, with engineer review) Add `Backend/Games/Tarkov/TarkovIntegration.cs` on
`LogTailIntegration`:
- find the latest `log_*` directory and its `*application_000.log`;
- raid start (`GameStarted`), map (`Location:`), raid end (return to menu);
- register it for `EscapeFromTarkov.exe`;
- tests using the P0.3 log fixtures.

**P3.3** (engineer) Defaults for Tarkov and Wardogs: Session or Hybrid mode, AV1, 1 s keyframes, and
tracks for game, Discord and mic.

**P3.4** (engineer) Hub uploader:
- a design doc, plus an implementation behind config that replaces the segra.tv target;
- presigned R2 later, LAN or Tailscale for now;
- no calls to segra.tv.

`[!]` Jay: install it and test it in-game.

### P4: No-button mode (hub)

**P4.1** (engineer) Long-recording ingestion:
- process Segra session files and OBS split recordings in chunks;
- segment EFT raids and label maps by wall clock;
- treat OBS chapter markers (`ffprobe -show_chapters`) and Segra bookmarks as strong hints;
- tests on a synthetic 10-minute recording plus the log fixtures.

**P4.2** (builder) Retention:
- a rolling cap for raw recordings, 40 GB by default;
- it applies only to files that autoclipper or the recorder created;
- run it in dry-run mode first.

### P5: Group features (testable offline)

**P5.1** (engineer) Multi-POV alignment: cross-correlate one player's mic with another's Discord track,
and return the offset with a confidence. Synthetic tests.

**P5.2** (builder) Search: SQLite FTS5 over titles, summaries and transcripts; a CLI command and a search
box on the review page.

**P5.3** (scout) Write `docs/BOT-INTEGRATION.md`: how the dudgy bot (cs-analytics) would post clips,
`/clips search`, and reactions feeding a taste model. Doc only; don't touch cs-analytics.

### R2: Final review and report

- (reviewer) Review everything that changed: correctness, the judge's usage guard, delete safety, secrets
  in git, and the Windows-side changes.
- (orchestrator) Fix or ticket the findings. Write `docs/AUTOPILOT-REPORT.md`: what's done, the evidence,
  costs, the NEEDS-JAY list, and how to run it. Then set STATUS to `done`, or `blocked` if only items
  needing Jay remain.

## Order and parallelism

Usage, not time, limits this run, so the order is by value: if usage runs out, P1 and P2 matter most.

- Start with P0, then R1.
- After that, run at most two tracks at once: P1 (engineer) alongside P2.1 → P2.5 (builder and engineer).
- P2.8 needs P2.1–P2.5, and P2.7 for posting.
- P3 starts once P2.8 is done. P4 and P5 come after P2.5.
- R2 comes last.
