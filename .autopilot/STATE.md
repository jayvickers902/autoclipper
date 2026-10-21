# Autopilot state

Markers: `[ ]` todo, `[~]` in progress, `[x]` done, `[!]` needs Jay, `[-]` dropped. Update after every task.
Task specs are in PLAN.md.

- [x] P0.1 scaffold repo
- [x] P0.2 ports claim
- [x] P0.3 fixtures
- [x] R1 design review
- [x] P1 OBS stopgap
- [x] P2.1 media primitives
- [x] P2.2a signals: loudness, onsets, CPU whisper, keywords
- [x] P2.2b signals: AudioSet tags, GPU whisper
- [x] P2.3 candidate windows
- [x] P2.4 judge (claude CLI) + usage guard
- [x] P2.5 store + pipeline + CLI
- [x] P2.6 review page
- [x] P2.7 Discord poster
- [x] P2.8 backfill + report
- [~] P2.9 judge comparison
- [x] P2.10 hub service
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
- P2.1 API: autoclipper.media {probe, keyframe_times, detect_black, detect_frozen, is_dead_capture, extract_audio(path, stream_index|None, out), audio_tracks, cut_copy, share_encode->EncodeResult, nvenc_works, thumbnail, preview_webp}; MediaError. Real replays have keyframes ~every 4 s.
- P1: OBS websocket on 4455 (password in obs-websocket config.json, never print). Test replay in Videos/autoclipper/obs-test/. In-game hook check is NEEDS-JAY.
- P2.2a API: autoclipper.signals.analyze(path, out_json=None, *, whisper=True, whisper_model=None, bias_keywords=False, check_dead=True, tagger=None); schema v1 in signals/analyze.py docstring; seconds keyed by role. large-v3-turbo CPU int8 ~30 s per 59 s replay. Commit only your task's files while another agent is mid-task (git add <paths>).
- P2.3 API: autoclipper.candidates.find_candidates(timeline, max_n=None, min_score=0.6) -> [Candidate] (to_dict); env AUTOCLIPPER_MAX_CANDIDATES (default 4). Real replays give 2 windows each, many hitting the 45 s cap. Thresholds are untuned guesses.
- q is paused all session: q's auto-detect treats EpicOnlineServicesUserHelper.exe (Epic Games folder) as a game. Don't rely on q; the hub's GPU gate (P2.10) must not copy that false positive (launcher/EOS helpers aren't games).
- tests/test_signals.py::test_speech_keywords flaked once under parallel load (passes alone).
- P2.5 API: pipeline.process_replay/process_pass/render_clip; judging.py has Judge Protocol + ClaudeJudge (lazy import of autoclipper.judge; adapt ClaudeJudge.__call__/paused/to_outcome when P2.4 API lands) + FakeJudge; CLI process/backfill/watch/report with `--judge fake`, `--no-whisper`; env AUTOCLIPPER_PIPELINE_JUDGE=fake. safety.assert_writable. Share encodes land ~18 MB (Discord free limit likely 10 MB -> 413 path re-encodes).
- P2.4 API: autoclipper.judge.judge_candidate(replay_path, candidate, timeline, settings=None, *, candidate_id, guard, rejudge_borderline=False) -> JudgeResult(verdict, ledger_rows, cost_usd, tokens, attempts, queued); polish(..., mode="polish"|"rejudge"); `uv run python -m autoclipper.judge status|clear-pause|one <replay>`. Ledger data/judge-ledger.sqlite (1 Haiku call used in P2.4 smoke: 11.7K in/1.1K out, 7.3 s, 5h util 0.22). Borderline re-judge opt-in (Sonnet cap 40). Pipeline queues on pause (R1), so ollama fallback is effectively unused by the pipeline; fine for now.
- P2.10: autoclipper.service installed+enabled (posting off: no --post). Review at http://autoclipper.localhost/ (200 via Caddy). GPU gate + claude CLI verified under systemd-run as jay. Restart it after code changes that affect run: sudo systemctl restart autoclipper.
- P2.2b: tagger = PANNs CNN14 ONNX on CPU (opt-in --tags / AUTOCLIPPER_TAGS=1; service unit sets it), GPU whisper float16 via `uv sync --extra gpu` (gated by GpuGate, CPU int8 fallback). Timeline schema v2. Service unit edited (--extra gpu, TAGS=1) but NOT yet re-installed: after P2.9 lands, sudo cp deploy/autoclipper.service /etc/systemd/system/ && daemon-reload && restart.
