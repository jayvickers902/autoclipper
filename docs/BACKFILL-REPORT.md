# Backfill report (P2.8)

Run: 2026-10-21 03:59–04:16 (-03), `uv run autoclipper backfill /mnt/c/Users/jayvi/Videos`, default settings
(CPU faster-whisper large-v3-turbo int8, Haiku 5.5 judge through `claude -p`, NVENC share encode). Log:
`data/logs/backfill1.log`; store `data/autoclipper.sqlite`; ledger `data/judge-ledger.sqlite`.

## Totals

| | |
|---|---|
| Replays found (top-level `Replay *.mp4`) | 32 (the plan said 35; 32 are at the top level) |
| Dead captures (black) skipped | 4: `2026-07-08 12-51-54`, `2026-07-08 12-52-19`, `2026-10-20 09-01-16`, `2026-10-20 13-50-58` |
| Processed | 28 |
| Candidate windows | 56 (1–4 per replay, mostly 2), mean 24.8 s, max 45 s |
| Judge calls | 56 Haiku, 56 ok, 0 retries, 0 fallbacks, 0 pauses |
| Kept | 31 (55 %); score 5–8 on a 1–10 scale; dropped 2–5 |
| Clips rendered | 31, all `h264_nvenc`, share files 1.8–19.0 MB (most ~17–18 MB) |
| Posted to Discord | 0: `DISCORD_TEST_WEBHOOK_URL` isn't set (see NEEDS-JAY) |
| Wall clock | 17 min for 32 replays (~32 s per replay) |

## Timings per stage (per processed replay)

| Stage | Mean s | Total s |
|---|---|---|
| probe | 0.06 | 1.9 |
| dead-capture check | 1.8 | 56.8 |
| analyze (loudness, onsets, CPU whisper, keywords) | 13.3 | 373 |
| candidates | 0.0 | 0.0 |
| judge (all candidates of the replay) | 17.2 | 483 |
| render (copy cut, share encode, thumb, webp) | 3.6 | 102 |

## Judge cost

- Per call: mean 7.1 s (median 6.6, p90 9.5, max 18.2); ~15.0K input tokens (36 % cache reads) and ~1.1K output
  tokens; notional API price $0.0025 (total $0.14 for the backfill). On the subscription it costs nothing extra.
- 5-hour window: utilization went from 0.40 to 0.52 over the 15.7 min of judging, and 7-day from 0.25 to 0.26.
  Those readings are account-wide: the autopilot session and two builder subagents were working at the same
  time, so **12 points is an upper bound** for 56 Haiku calls; the judge's own share is likely well under half
  of that (Haiku tokens are far cheaper against the limit than the Opus/Sonnet work running alongside).

## Projection for 2 users

Assume each of 2 players saves ~10 replays a night (button mode) → ~40 candidates → 40 Haiku calls, ~0.6M
tokens, ~5 min of judging. Against the upper bound above (12 points per 56 calls) that is ≤ 9 % of a 5-hour
window per night, and probably 2–4 %. No-button mode (P4: whole sessions) would produce far more candidates;
at ~20 candidates per played hour, 2 players × 3 h ≈ 120 calls ≈ ≤ 25 % of a window, so the 0.9 pause and
the hourly cap (60) matter there, and a cheap local pre-filter before Haiku would be worth adding.

## Findings for tuning

- **Game label:** 37 of 56 verdicts say `game: other`. Many of those are CS2 (Dust II, AWP), which the schema
  doesn't list; add `cs2` (and check Wardogs is recognised).
- **Keep rate is high (55 %)** and scores cluster at 6. The keep threshold and prompt want calibration from
  Jay's 👍/👎 labels on the review page (`/tolabel`), which is what P2.9 compares against.
- **Share files are ~18 MB**, just under the 19 MB target. Discord's free webhook limit is likely 10 MB, so
  most posts would hit the 413 → 9.5 MB re-encode path; consider encoding to 9.5 MB up front.
- Best candidates (by Haiku score): `2026-04-26 21-55-35` 76–84 s "AWP streak…" (8), `2026-04-28 23-16-53`
  36–81 s "Two AWP kills on B site…" (8), `2026-05-04 21-31-58` 41–86 s "Jonny's headshot 4k…" (8),
  `2026-04-27 09-25-21` 63–85 s "Shotgun fight ends with elimination…" (7, Tarkov), `2026-04-27 15-51-18`
  26–64 s "Night scav firefight…" (7, Tarkov). These are the 5 the backfill would post once a webhook exists:
  `uv run autoclipper` has no "post best" command yet; P2.10's `run --post` posts new keeps (capped at 5).
