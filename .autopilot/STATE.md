# Autopilot state

Markers: `[ ]` todo, `[~]` in progress, `[x]` done, `[!]` needs Jay, `[-]` dropped. Update after every task.
Task specs are in PLAN.md.

- [x] P0.1 scaffold repo
- [x] P0.2 ports claim
- [x] P0.3 fixtures
- [~] R1 design review
- [ ] P1 OBS stopgap
- [ ] P2.1 media primitives
- [ ] P2.2 signals
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
