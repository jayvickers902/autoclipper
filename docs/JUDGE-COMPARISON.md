# Judge comparison (P2.9)

Run 2026-10-21 with `uv run python -m autoclipper.judge.compare run` / `report`. Raw results:
`data/judge-compare-sonnet.json` (answers in `data/judge-compare.sqlite`, calls in the judge ledger).

## Setup

- **Haiku alone:** the 56 backfill verdicts from `claude-haiku-5-5` already in the store (P2.8).
- **Reference:** `claude-sonnet-5-5` judging a stratified sample of 17 of those candidates with the same
  prompt, frames and timeline.
- **Haiku + Sonnet rejudge:** Sonnet re-judges the borderline Haiku verdicts (score 4-6) in the sample (11),
  seeing Haiku's answer.
- **Local judge (ollama):** not run. Wardogs was running for the rest of this run, and the GPU gate forbids
  GPU work while a game runs. Run it later: `uv run python -m autoclipper.judge.compare run --ollama
  --ollama-vision 5` (no Claude calls), then `report`.
- Cost: 28 Sonnet calls (17 reference + 11 rejudge), 0 extra Haiku calls.

## Results (reference: Sonnet, n = 17)

| Judge | Accuracy | Cohen's kappa | TP/FP/FN/TN | Keep rate (ref 0.29) | Score Spearman | Score MAE | Type |
|---|---|---|---|---|---|---|---|
| Haiku | 0.71 | 0.40 | 4/4/1/8 | 0.47 | 0.78 | 1.41 | 0.59 |
| Haiku + rejudge | 0.88 | 0.68 | 3/0/2/12 | 0.18 | 0.88 | 0.47 | 0.94 |

Keep threshold sweep for Haiku (accuracy if keep = `score >= t` instead of the model's own flag):

| t | 3 | 4 | 5 | 6 | **7** | 8 | 9 |
|---|---|---|---|---|---|---|---|
| Haiku | 0.35 | 0.47 | 0.65 | 0.71 | **0.88** | 0.77 | 0.71 |

| Judge | Calls | Mean s | Median s | p90 s | Mean input tokens | Mean output tokens |
|---|---|---|---|---|---|---|
| Haiku | 56 | 7.1 | 6.6 | 9.5 | 15.0K | 1.06K |
| Sonnet (reference) | 17 | 4.4 | 4.1 | 5.8 | 12.2K | 0.37K |
| Sonnet (rejudge) | 11 | 4.9 | 4.4 | 7.0 | 12.1K | 0.43K |

Game labels agree 0.47 raw but 1.00 once `cs2` is folded into `other`: the backfill replays include CS2, and
the Haiku runs predate the `cs2` game value added for this comparison. Sonnet is faster than Haiku here
because Haiku writes about three times as many output tokens.

## Findings

- Haiku ranks clips well (Spearman 0.78) but its own keep flag is too generous: it keeps 47% of candidates
  where Sonnet keeps 29%, so most of its errors are false keeps.
- Using Haiku's score with keep = `score >= 7` matches the Sonnet + rejudge combination (0.88) at no extra
  Sonnet calls.
- Rejudging borderline verdicts fixes score calibration (MAE 1.41 -> 0.47) but costs 0.61 Sonnet calls per
  candidate (34 of 56 Haiku verdicts were borderline). With the 30/day Sonnet cap that's only ~50 candidates
  a day, and it also drops some true keeps (2 FN).
- n = 17 is small; treat the thresholds as a starting point and re-check against Jay's labels.

## Defaults (applied)

- `AUTOCLIPPER_KEEP_MIN_SCORE=7`: the hub keeps a Claude verdict when its score is >= 7 and stores the model's
  own flag as `model_keep` (`judging.apply_keep_threshold`). 0 restores the model's flag.
- Borderline Sonnet rejudge stays **off** (opt-in `rejudge_borderline`); Sonnet is for polishing posted clips.
- Haiku stays the per-candidate judge.

## For Jay: label, then re-score

1. Open http://autoclipper.localhost/tolabel and give each candidate 👍 or 👎 (about 56 of them; ~10 min).
2. `cd ~/projects/autoclipper && uv run python -m autoclipper.judge.compare report --against labels`
   (no Claude calls). It prints the same tables with your labels as the reference, and the threshold sweep
   shows whether 7 is right for your taste.
