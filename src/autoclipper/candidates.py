"""Candidate clip windows from a signals timeline (`autoclipper.signals.analyze`, schema v1 or v2).

    find_candidates(timeline)  -> list[Candidate]   best windows first, at most AUTOCLIPPER_MAX_CANDIDATES
    interest_curve(timeline)   -> np.ndarray        fused per-hop interest, normalised per replay

Pipeline: fights (transients with gaps <= FIGHT_GAP) and strong keywords ("clip that", said after the
action) become windows; overlapping or nearly touching windows merge; long ones are cut to the most
interesting MAX_LEN seconds, short ones padded to MIN_LEN; ends then snap to the quietest nearby spot that
is not inside a burst. Windows are scored on the *absolute* interest scale (curve / FLOOR) so a quiet
replay yields nothing even though its curve is normalised to its own maximum.

`python -m autoclipper.candidates <timeline.json>` prints the candidates of a saved timeline.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field

import numpy as np

HOP = 0.5  # s per interest-curve bin

# Relative weight of each interest component (each component is ~0..1 before weighting, density up to 2).
WEIGHTS: dict[str, float] = {
    "transient": 1.0,  # strength-weighted shots/s on the transient role, Gaussian smoothed
    "loud": 0.6,  # transient-role rms above the replay's median, 20 dB = 1
    "keyword": 1.5,  # strong keyword ("clip that"): box over the KEYWORD_LOOKBACK s before it
    "keyword_weak": 0.4,  # other hype phrases ("nice", "let's go"): short box before it
    "speech": 0.5,  # voice-role rms well above that role's median
    "tag_high": 1.0,  # AudioSet gunshot / explosion (scaled by the tag score)
    "tag_mid": 0.5,  # laughter / shout: the funny / hype boost
    "tag_music": 0.0,  # not additive: music damps the transient and loud components (see MUSIC_DAMP)
}
SIGMA = 1.0  # s, Gaussian smoothing of transient density
LOUD_REF_DB = 20.0
SPEECH_MARGIN_DB = 6.0  # voice must exceed its median by this much before it counts
SPEECH_REF_DB = 15.0
STRONG_KEYWORDS = frozenset({"clip that", "clip it", "clip this"})
KEYWORD_LOOKBACK = 10.0  # s of interest credited before a strong keyword
WEAK_LOOKBACK = 3.0
TAGS_HIGH = ("gunshot", "gunfire", "machine gun", "explosion")
TAGS_MID = ("laughter", "shout", "yell", "scream")
TAGS_MUSIC = ("music",)
TAG_MIN = 0.1  # tag scores below this are noise floor
TAG_REF = 0.5  # a tag score at this level counts fully (PANNs rarely exceeds ~0.5 on game audio)
MUSIC_MIN = 0.3  # music scores below this do not damp
MUSIC_DAMP = 0.6  # at most this share of the transient/loud interest is removed under pure music
FLOOR = 1.5  # raw interest that counts as "a real fight"; curve = raw / max(raw.max(), FLOOR)

FIGHT_GAP = 4.0
VERY_STRONG = 0.8  # a lone transient at least this strong is a fight
PRE_FIGHT, POST_FIGHT = 6.0, 4.0
KW_PRE, KW_POST = 20.0, 3.0
KW_FIGHT_REACH = 15.0  # a keyword this soon after a fight's last event belongs to the fight
KW_FIGHT_POST = 2.0
MERGE_GAP = 1.5
MAX_LEN, MIN_LEN = 45.0, 6.0
SNAP_RANGE, SNAP_STEP, SNAP_LEN = 1.5, 0.25, 0.25
TRANSIENT_CLEARANCE = 0.3  # an end never lands this close to a transient
BURST_TAIL = 1.0  # within this of a transient the snap cost is penalised (tail of the burst)
MIN_SCORE = 0.6
DEFAULT_MAX_CANDIDATES = 4
KW_BONUS_S = 5.0  # integrated-interest bonus for keeping a strong keyword inside a capped window


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    reasons: list[str] = field(default_factory=list)
    n_transients: int = 0
    keywords: list[str] = field(default_factory=list)
    peak_t: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("start", "end", "score", "peak_t"):
            d[k] = round(d[k], 2)
        return d


@dataclass
class _Win:
    start: float
    end: float


def _n_bins(timeline: dict) -> int:
    return max(1, math.ceil(float(timeline["duration"]) / HOP))


def _transients(timeline: dict) -> list[tuple[float, float]]:
    return [(e["t"], e.get("strength", 0.5)) for e in timeline["events"] if e["kind"] == "transient"]


def _keywords(timeline: dict) -> list[dict]:
    return [e for e in timeline["events"] if e["kind"] == "keyword"]


def _transient_role(timeline: dict) -> str | None:
    seconds = timeline.get("seconds") or {}
    role = (timeline.get("sources") or {}).get("transients")
    if role in seconds:
        return role
    return next(iter(seconds), None)


def _per_second(values: list[float], n: int) -> np.ndarray:
    """Spread a per-second series over the hop grid (bin i covers second floor(i * HOP))."""
    if not values:
        return np.zeros(n)
    idx = np.minimum((np.arange(n) * HOP).astype(int), len(values) - 1)
    return np.asarray(values, dtype=float)[idx]


def _gauss(x: np.ndarray, sigma_s: float) -> np.ndarray:
    half = int(math.ceil(4 * sigma_s / HOP))
    k = np.exp(-0.5 * (np.arange(-half, half + 1) * HOP / sigma_s) ** 2)
    k /= k.sum() * HOP  # unit area in seconds, so the result is "strength per second"
    return np.convolve(x, k)[half : half + len(x)]


def _box(n: int, t0: float, t1: float) -> np.ndarray:
    out = np.zeros(n)
    out[max(0, int(t0 / HOP)) : max(0, min(n, int(math.ceil(t1 / HOP)) + 1))] = 1.0
    return out


def _tag_curve(timeline: dict, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(high, mid, music) per hop, each 0..1. Music is read from the transient role only (a song on the
    voice channel says nothing about the game audio); gunshot and laughter from any role."""
    seconds = timeline.get("seconds") or {}
    secs = max((len(v) for v in seconds.values()), default=0)
    high, mid, music = np.zeros(secs), np.zeros(secs), np.zeros(secs)
    t_role = _transient_role(timeline)
    for role, entries in seconds.items():
        for s, e in enumerate(entries):
            for label, score in (e.get("tags") or {}).items():
                low = label.lower()
                if any(t in low for t in TAGS_HIGH):
                    high[s] = max(high[s], _tag_level(score))
                elif any(t in low for t in TAGS_MID):
                    mid[s] = max(mid[s], _tag_level(score))
                elif role == t_role and any(t in low for t in TAGS_MUSIC) and score > MUSIC_MIN:
                    music[s] = max(music[s], (score - MUSIC_MIN) / (1.0 - MUSIC_MIN))
    return _per_second(list(high), n), _per_second(list(mid), n), _per_second(list(music), n)


def _tag_level(score: float) -> float:
    return 0.0 if score < TAG_MIN else min(1.0, score / TAG_REF)


def _components(timeline: dict) -> dict[str, np.ndarray]:
    n = _n_bins(timeline)
    comp = {k: np.zeros(n) for k in WEIGHTS}
    hits = np.zeros(n)
    for t, strength in _transients(timeline):
        hits[min(n - 1, max(0, int(round(t / HOP))))] += strength
    comp["transient"] = np.minimum(_gauss(hits, SIGMA), 2.0)
    role = _transient_role(timeline)
    if role:
        rms = np.array([s["rms_db"] for s in timeline["seconds"][role]], dtype=float)
        if rms.size:
            comp["loud"] = _per_second(list(np.clip((rms - np.median(rms)) / LOUD_REF_DB, 0, 1)), n)
    for kw in _keywords(timeline):
        if kw["phrase"] in STRONG_KEYWORDS:
            comp["keyword"] = np.maximum(comp["keyword"], _box(n, kw["t"] - KEYWORD_LOOKBACK, kw["end"]))
        else:
            comp["keyword_weak"] = np.maximum(
                comp["keyword_weak"], _box(n, kw["t"] - WEAK_LOOKBACK, kw["end"])
            )
    for vrole in (timeline.get("sources") or {}).get("speech") or []:
        entries = (timeline.get("seconds") or {}).get(vrole)
        if entries:
            rms = np.array([s["rms_db"] for s in entries], dtype=float)
            excite = np.clip((rms - np.median(rms) - SPEECH_MARGIN_DB) / SPEECH_REF_DB, 0, 1)
            comp["speech"] = np.maximum(comp["speech"], _per_second(list(excite), n))
    comp["tag_high"], comp["tag_mid"], comp["tag_music"] = _tag_curve(timeline, n)
    return comp


def _raw(timeline: dict) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    comp = _components(timeline)
    # music makes transients (beats) look like fights; a gunshot tag in the same second cancels the damping
    keep = 1.0 - MUSIC_DAMP * comp["tag_music"] * (1.0 - comp["tag_high"])
    raw = sum(WEIGHTS[k] * v * (keep if k in ("transient", "loud") else 1.0) for k, v in comp.items())
    return raw, comp


def interest_curve(timeline: dict, hop: float = HOP) -> np.ndarray:
    """Fused interest per `hop` seconds (bin i = time i * hop), at most 1 and floored at FLOOR.

    Divided by max(its own peak, FLOOR): the replay's best moment reads 1.0 only when it is a real fight;
    a replay with nothing in it stays near 0.
    """
    if hop != HOP:
        raise ValueError(f"only hop={HOP} is supported")
    if timeline.get("dead"):
        return np.zeros(_n_bins(timeline))
    raw, _ = _raw(timeline)
    return raw / max(float(raw.max()), FLOOR)


# --- window building -------------------------------------------------------------------------------------


def _fights(transients: list[tuple[float, float]]) -> list[tuple[float, float, int]]:
    """(first, last, n) of each transient cluster that qualifies as a fight."""
    out, cur = [], []
    for t, st in transients + [(math.inf, 0.0)]:
        if cur and t - cur[-1][0] > FIGHT_GAP:
            if len(cur) >= 2 or cur[0][1] >= VERY_STRONG:
                out.append((cur[0][0], cur[-1][0], len(cur)))
            cur = []
        cur.append((t, st))
    return out


def _raw_windows(timeline: dict) -> list[_Win]:
    fights = _fights(_transients(timeline))
    wins = [_Win(f - PRE_FIGHT, last + POST_FIGHT) for f, last, _ in fights]
    for kw in _keywords(timeline):
        if kw["phrase"] not in STRONG_KEYWORDS:
            continue
        t, end = kw["t"], kw["end"]
        owner = next(
            (w for w, (f, last, _) in zip(wins[: len(fights)], fights, strict=True)
             if w.start <= t <= w.end or 0 <= t - last <= KW_FIGHT_REACH),
            None,
        )  # fmt: skip
        if owner:
            owner.end = max(owner.end, end + KW_FIGHT_POST)
        else:
            wins.append(_Win(t - KW_PRE, end + KW_POST))
    return wins


def _merge(wins: list[_Win]) -> list[_Win]:
    out: list[_Win] = []
    for w in sorted(wins, key=lambda w: w.start):
        if out and w.start - out[-1].end <= MERGE_GAP:
            out[-1].end = max(out[-1].end, w.end)
        else:
            out.append(_Win(w.start, w.end))
    return out


def _cap(w: _Win, curve: np.ndarray, strong_kws: list[dict]) -> _Win:
    """Keep the MAX_LEN sub-window with the highest integrated interest (strong keywords get a bonus)."""
    if w.end - w.start <= MAX_LEN:
        return w
    n = len(curve)
    csum = np.concatenate([[0.0], np.cumsum(curve)])
    width = int(round(MAX_LEN / HOP))
    first = int(math.ceil(w.start / HOP))
    last = max(first, int(w.end / HOP) - width)
    best, best_val = first, -1.0
    for i in range(first, last + 1):
        a, b = min(i, n), min(i + width, n)
        val = (csum[b] - csum[a]) * HOP
        s, e = i * HOP, i * HOP + MAX_LEN
        val += KW_BONUS_S * sum(1 for k in strong_kws if s <= k["t"] and k["end"] <= e)
        if val > best_val + 1e-9:
            best, best_val = i, val
    return _Win(best * HOP, best * HOP + MAX_LEN)


def _pad(w: _Win, duration: float) -> _Win:
    start, end = max(0.0, w.start), min(duration, w.end)
    if end - start < MIN_LEN and duration >= MIN_LEN:
        mid = (start + end) / 2
        start = min(max(0.0, mid - MIN_LEN / 2), duration - MIN_LEN)
        end = start + MIN_LEN
    return _Win(start, end)


def _snap(pos: float, lo: float, hi: float, rms_at, transient_ts: np.ndarray) -> float:
    """Best position within +-SNAP_RANGE of `pos` (and within [lo, hi]): quiet, clear of bursts, close."""
    best, best_cost = pos, math.inf
    for k in range(-int(SNAP_RANGE / SNAP_STEP), int(SNAP_RANGE / SNAP_STEP) + 1):
        p = pos + k * SNAP_STEP
        if p < lo or p > hi:
            continue
        dist = float(np.abs(transient_ts - p).min()) if transient_ts.size else math.inf
        if dist < TRANSIENT_CLEARANCE:
            continue
        cost = rms_at(p) + 0.5 * abs(p - pos)
        if dist < BURST_TAIL:
            cost += (BURST_TAIL - dist) * 10.0
        if cost < best_cost - 1e-9:
            best, best_cost = p, cost
    return best


def _snap_ends(w: _Win, duration: float, timeline: dict) -> _Win:
    role = _transient_role(timeline)
    rms = [s["rms_db"] for s in timeline["seconds"][role]] if role else []

    def rms_at(p: float) -> float:
        if not rms:
            return 0.0
        c = p + SNAP_LEN / 2
        return rms[min(len(rms) - 1, max(0, int(c)))]

    ts = np.array([t for t, _ in _transients(timeline)])
    start = _snap(w.start, max(0.0, w.end - MAX_LEN), w.end - MIN_LEN, rms_at, ts)
    end = _snap(w.end, start + MIN_LEN, min(duration, start + MAX_LEN), rms_at, ts)
    return _Win(start, end)


def _score(raw: np.ndarray, w: _Win, n_trans: int, strong_kw: bool) -> tuple[float, float]:
    """(score, peak) on the absolute scale: peak + 0.1 * integral (<= 2) + shots bonus + keyword bonus."""
    seg = raw[int(w.start / HOP) : max(int(w.start / HOP) + 1, int(math.ceil(w.end / HOP)))] / FLOOR
    peak = float(seg.max())
    integral = min(float(seg.sum()) * HOP, 20.0)
    return peak + 0.1 * integral + 0.05 * min(n_trans, 12) + (0.5 if strong_kw else 0.0), peak


def max_candidates() -> int:
    try:
        return max(1, int(os.environ.get("AUTOCLIPPER_MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES)))
    except ValueError:
        return DEFAULT_MAX_CANDIDATES


def find_candidates(
    timeline: dict, max_n: int | None = None, min_score: float = MIN_SCORE
) -> list[Candidate]:
    """Best clip windows of one replay, highest score first (empty for dead or uneventful replays)."""
    if timeline.get("dead"):
        return []
    duration = float(timeline["duration"])
    raw, comp = _raw(timeline)
    curve = raw / max(float(raw.max()), FLOOR)
    kws = _keywords(timeline)
    strong = [k for k in kws if k["phrase"] in STRONG_KEYWORDS]
    transients = _transients(timeline)
    out: list[Candidate] = []
    for w in _merge(_raw_windows(timeline)):
        w = _pad(_cap(_pad(w, duration), curve, strong), duration)
        w = _snap_ends(w, duration, timeline)
        inside = [(t, st) for t, st in transients if w.start <= t <= w.end]
        in_kws = [k for k in kws if w.start <= k["t"] <= w.end]
        score, _ = _score(raw, w, len(inside), any(k["phrase"] in STRONG_KEYWORDS for k in in_kws))
        if score < min_score:
            continue
        reasons = []
        if len(inside) >= 2 or any(st >= VERY_STRONG for _, st in inside):
            reasons.append(f"fight:{len(inside)} shot{'s' if len(inside) != 1 else ''}")
        reasons += [f"keyword:{k['phrase']}" for k in in_kws]
        a, b = int(w.start / HOP), max(int(w.start / HOP) + 1, int(math.ceil(w.end / HOP)))
        if comp["speech"][a:b].max(initial=0.0) >= 0.5:
            reasons.append("excited speech")
        if max(comp["tag_high"][a:b].max(initial=0.0), comp["tag_mid"][a:b].max(initial=0.0)) >= 0.5:
            reasons.append("audio tags")
        dens = comp["transient"][a:b]
        seg = dens if dens.size and dens.max() > 0 else curve[a:b]
        peak_t = (a + int(seg.argmax())) * HOP if seg.size else w.start
        out.append(
            Candidate(w.start, w.end, score, reasons, len(inside), [k["phrase"] for k in in_kws], peak_t)
        )
    out.sort(key=lambda c: -c.score)
    return out[: max_n if max_n is not None else max_candidates()]


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        timeline = json.load(f)
    cands = find_candidates(timeline)
    print(f"{argv[0]}: {timeline['duration']}s, {len(cands)} candidate(s)")
    for c in cands:
        print(
            f"  {c.start:7.2f} - {c.end:7.2f}  score {c.score:.2f}  peak {c.peak_t:.1f}  "
            + ", ".join(c.reasons)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
