# Autopilot state

Markers: `[ ]` todo, `[~]` in progress, `[x]` done, `[!]` needs Jay, `[-]` dropped. Update after every task.
Task specs are in PLAN.md.

- [x] P0.1 scaffold repo
- [x] P0.2 ports claim
- [x] P0.3 fixtures
- [x] R1 design review
- [ ] P1 OBS stopgap
- [~] P2.1 media primitives
- [ ] P2.2a signals: loudness, onsets, CPU whisper, keywords
- [ ] P2.2b signals: AudioSet tags, GPU whisper
- [ ] P2.3 candidate windows
- [ ] P2.4 judge (claude CLI) + usage guard
- [ ] P2.5 store + pipeline + CLI
- [ ] P2.6 review page
- [ ] P2.7 Discord poster
- [ ] P2.8 backfill + report
- [ ] P2.9 judge comparison
- [ ] P2.10 hub service
- [ ] P3.1 Segra clone + build
- [ ] P3.2 TarkovIntegration
- [ ] P3.3 recorder defaults
- [ ] P3.4 hub uploader
- [ ] P4.1 long-recording ingestion
- [ ] P4.2 retention
- [ ] P5.1 multi-POV alignment
- [ ] P5.2 search
- [ ] P5.3 bot integration doc
- [ ] R2 final review + report

## Notes for the next session

Record decisions, gotchas, and anything a restarted session must know here.
- Port 4180 (`ports claim autoclipper`). Settings in `src/autoclipper/config.py` (`Settings.from_env`, `bind_socket` fails if taken).
- No `.env` yet: Discord posting is off (NEEDS-JAY). Code must treat a missing webhook as "skip posting", not an error.
- R1 applied to PLAN.md ("Order"): thin slice P2.1→P2.2a→P2.3→P2.4→P2.5→P2.7→P2.8→P2.10, then P2.6, P2.2b, P2.9. Hub tasks are carried by builders unless risky.
- R1 for P2.4 brief: caps in code (run_max_haiku 300, run_max_sonnet 40, judge_max_7d 0.6), ledger row reserved BEFORE each call and counted even on failure, pause_until in SQLite, cross-process flock, tests can never call real claude (AUTOCLIPPER_CLAUDE_BIN fake + refuse when PYTEST_CURRENT_TEST), pause on rejected / missing rate_limit_event / "usage limit" error; threshold pause uses five_hour.resetsAt, rejection uses top-level resetsAt (min now+15 min); 120 s timeout with killpg; auth failure = fatal pause + NEEDS-JAY. Statuses include allowed_warning.
- R1 for P2.5: scan only top-level `Replay *.mp4` in replay_dir, exclude output_dir, dedupe by hash, wait for size to be stable; one tested assert_writable() allowing only output_dir and data_dir.
- R1 for P2.8: queue candidates until reset instead of falling back to the 27B; run backfill with run_in_background.
- R1 for P1: robocopy exit >= 8 is failure, verify backup file count before editing; restore by renaming current dir and copying the backup back (never /MIR); add a Display Capture below the per-exe captures; keep websocket password out of .env/logs.
