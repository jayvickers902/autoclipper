from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from autoclipper import candidates, signals
from autoclipper.candidates import Candidate, find_candidates, interest_curve

BED_DB, LOUD_DB = -45.0, -20.0


def timeline(
    duration: float,
    shots: list[float | tuple[float, float]] = (),
    keywords: list[tuple[float, float, str]] = (),
    dead: bool = False,
    voice: list[float] | None = None,
    tags: dict[int, dict[str, float]] | None = None,
) -> dict:
    """A schema-v1 timeline built in code: quiet bed, loud seconds wherever a shot lands."""
    n = math.ceil(duration)
    transients = [(s, 0.8) if isinstance(s, float | int) else s for s in shots]
    loud = {int(t) for t, _ in transients}
    game = [
        {"t": s, "rms_db": LOUD_DB if s in loud else BED_DB, "peak_db": -10.0, "onsets": 0, "speech": False,
         "tags": (tags or {}).get(s, {})}
        for s in range(n)
    ]  # fmt: skip
    seconds = {"game": game}
    sources = {"transients": "game", "speech": []}
    if voice is not None:
        seconds["mic"] = [
            {"t": s, "rms_db": voice[s], "peak_db": -5.0, "onsets": 0, "speech": True, "tags": {}}
            for s in range(n)
        ]
        sources["speech"] = ["mic"]
    events = [{"t": t, "kind": "transient", "strength": st, "role": "game"} for t, st in transients]
    events += [
        {"t": t, "kind": "keyword", "phrase": p, "text": p, "end": e, "role": "mic"} for t, e, p in keywords
    ]
    return {
        "version": 1, "source": "synthetic.mp4", "duration": duration, "dead": dead, "dead_reason": "",
        "tracks": [], "sources": sources, "whisper_model": "", "seconds": seconds,
        "events": sorted(events, key=lambda e: e["t"]), "transcript": [],
    }  # fmt: skip

FIGHT_AT_20 = [20.0, 20.4, 21.1, 21.3]


def assert_valid(cands: list[Candidate], tl: dict) -> None:
    ts = [e["t"] for e in tl["events"] if e["kind"] == "transient"]
    for c in cands:
        assert 0 <= c.start < c.end <= tl["duration"]
        assert c.end - c.start <= 45.0 + 1e-6
        assert c.end - c.start >= 6.0 - 1e-6 or tl["duration"] < 6
        for edge in (c.start, c.end):
            assert all(abs(edge - t) >= 0.3 for t in ts), (edge, ts)
    spans = sorted((c.start, c.end) for c in cands)
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:], strict=False))


FIGHT = [6.0, 6.4, 7.1, 7.3]


def test_single_fight_window() -> None:
    tl = timeline(20.0, FIGHT)
    (c,) = find_candidates(tl)
    assert c.start == pytest.approx(0.0, abs=1.5)
    assert c.end == pytest.approx(11.3, abs=1.5)
    assert c.n_transients == 4 and c.reasons[0] == "fight:4 shots" and c.keywords == []
    assert 6.0 <= c.peak_t <= 7.5
    assert_valid([c], tl)


def test_two_fights_far_apart() -> None:
    tl = timeline(80.0, [10.0, 10.5, 11.2, 40.0, 40.4, 41.0])
    cands = sorted(find_candidates(tl), key=lambda c: c.start)
    assert len(cands) == 2
    assert cands[0].end < cands[1].start
    assert cands[0].start == pytest.approx(4.0, abs=1.5) and cands[1].start == pytest.approx(34.0, abs=1.5)
    assert_valid(cands, tl)


def test_long_fight_capped_at_45s() -> None:
    tl = timeline(80.0, [5.0 + 0.8 * i for i in range(88)])
    (c,) = find_candidates(tl)
    assert 40.0 <= c.end - c.start <= 45.0 + 1e-6
    assert_valid([c], tl)


def test_keyword_alone_window_ends_after_keyword() -> None:
    tl = timeline(80.0, keywords=[(49.4, 50.0, "clip that")])
    (c,) = find_candidates(tl)
    assert c.end == pytest.approx(53.0, abs=1.5)
    assert c.start == pytest.approx(30.0, abs=1.5)
    assert c.reasons == ["keyword:clip that"] and c.keywords == ["clip that"] and c.n_transients == 0
    assert_valid([c], tl)


def test_keyword_after_fight_merges_into_one_window() -> None:
    tl = timeline(60.0, [20.0, 20.4, 21.1, 21.3], keywords=[(26.3, 27.0, "clip that")])
    (c,) = find_candidates(tl)
    assert c.start == pytest.approx(14.0, abs=1.5) and c.end == pytest.approx(29.0, abs=1.5)
    assert c.reasons == ["fight:4 shots", "keyword:clip that"]
    assert_valid([c], tl)


def test_weak_keyword_alone_makes_no_window() -> None:
    assert find_candidates(timeline(60.0, keywords=[(30.0, 30.4, "nice")])) == []


@pytest.mark.parametrize(
    "tl",
    [
        timeline(60.0),
        timeline(60.0, [(30.0, 0.3)]),
        timeline(60.0, [(10.0, 0.3), (30.0, 0.35), (50.0, 0.3)]),
        timeline(60.0, FIGHT, dead=True),
        timeline(3.0),
    ],
    ids=["empty", "one weak", "scattered weak", "dead", "tiny"],
)
def test_uneventful_or_dead_gives_nothing(tl: dict) -> None:
    assert find_candidates(tl) == []
    assert interest_curve(tl).max() < 0.6


def test_top_n_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    tl = timeline(150.0, [t + d for t in (10, 35, 60, 85, 110, 135) for d in (0.0, 0.4, 1.1)])
    monkeypatch.delenv("AUTOCLIPPER_MAX_CANDIDATES", raising=False)
    assert len(find_candidates(tl)) == 4
    assert len(find_candidates(tl, max_n=2)) == 2
    monkeypatch.setenv("AUTOCLIPPER_MAX_CANDIDATES", "3")
    cands = find_candidates(tl)
    assert len(cands) == 3 and [c.score for c in cands] == sorted((c.score for c in cands), reverse=True)
    assert_valid(cands, tl)


def test_windows_clamped_to_replay() -> None:
    tl = timeline(30.0, [0.5, 0.9, 29.2, 29.6])
    cands = find_candidates(tl)
    assert len(cands) == 2 and min(c.start for c in cands) == 0.0 and max(c.end for c in cands) == 30.0
    assert_valid(cands, tl)


def test_short_window_padded_to_min_length() -> None:
    tl = timeline(60.0, [(30.0, 1.0)])  # lone very strong shot: 10 s window
    (c,) = find_candidates(tl)
    assert c.end - c.start >= 6.0
    assert_valid([c], tl)


def test_ends_avoid_transients() -> None:
    # the fight's natural end (11.3) sits 0.1 s from a stray, weak transient
    tl = timeline(20.0, FIGHT + [(11.4, 0.3)])
    (c,) = find_candidates(tl)
    assert abs(c.end - 11.4) >= 0.3
    assert_valid([c], tl)


def test_to_dict_is_json_serialisable() -> None:
    (c,) = find_candidates(timeline(20.0, FIGHT))
    d = json.loads(json.dumps(c.to_dict()))
    assert set(d) == {"start", "end", "score", "reasons", "n_transients", "keywords", "peak_t"}


def test_interest_curve_components() -> None:
    n = 40
    calm = timeline(float(n), FIGHT)
    curve = interest_curve(calm)
    assert curve.shape == (80,) and curve.max() <= 1.0 + 1e-9
    assert 12 <= int(curve.argmax()) <= 15 and curve[60] == 0.0  # peak near 6-7.5 s, nothing at 30 s
    # a clip-that keyword lifts the 10 s before it
    kw = interest_curve(timeline(float(n), keywords=[(30.0, 30.5, "clip that")]))
    assert kw[40:61].min() > 0 and kw[:20].max() == 0
    # loud speech on the voice role counts, ordinary speech does not
    voice = [-30.0] * n
    base = interest_curve(timeline(float(n), voice=voice)).max()
    voice[20] = voice[21] = -8.0
    assert interest_curve(timeline(float(n), voice=voice)).max() > base
    # tags: weighted when present, no effect when absent
    tagged = timeline(float(n), tags={25: {"Gunshot, gunfire": 0.9}, 5: {"Laughter": 0.9}})
    tc = interest_curve(tagged)
    assert tc[50] > tc[10] > tc[30] == 0.0
    assert np.array_equal(interest_curve(timeline(float(n))), np.zeros(80))


def test_cli_prints_candidates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "t.json"
    p.write_text(json.dumps(timeline(20.0, FIGHT)))
    assert candidates.main([str(p)]) == 0
    out = capsys.readouterr().out
    assert "1 candidate" in out and "fight:4 shots" in out


def test_firefight_fixture_integration(fixtures_dir: Path) -> None:
    tl = signals.analyze(fixtures_dir / "firefight.mp4", whisper=False)
    cands = find_candidates(tl)
    assert cands and any(c.start <= 6.0 and c.end >= 12.2 for c in cands)
    assert_valid(cands, tl)


def _tags_at(secs: range, tag: dict[str, float]) -> dict[int, dict[str, float]]:
    return {s: dict(tag) for s in secs}


def test_tag_weighting_orders_gunshot_over_laughter_over_nothing() -> None:
    n = 40
    gun = interest_curve(timeline(float(n), tags={20: {"gunshot": 0.5}}))
    fun = interest_curve(timeline(float(n), tags={20: {"laughter": 0.5}}))
    shout = interest_curve(timeline(float(n), tags={20: {"shout": 0.5}}))
    assert gun[40] > fun[40] > 0 and shout[40] == pytest.approx(fun[40])
    assert gun[40] / fun[40] == pytest.approx(2.0)  # tag_high 1.0 vs tag_mid 0.5
    weak = interest_curve(timeline(float(n), tags={20: {"gunshot": 0.05}}))  # under the noise floor
    assert weak.max() == 0.0
    # score scales the boost until TAG_REF, then saturates
    half = interest_curve(timeline(float(n), tags={20: {"explosion": 0.25}}))[40]
    assert half == pytest.approx(gun[40] / 2) and interest_curve(
        timeline(float(n), tags={20: {"explosion": 0.9}})
    )[40] == pytest.approx(gun[40])


def test_tags_lift_a_candidate_score() -> None:
    plain = find_candidates(timeline(60.0, FIGHT_AT_20))
    tags = _tags_at(range(20, 24), {"gunshot": 0.6, "laughter": 0.6})
    tagged = find_candidates(timeline(60.0, FIGHT_AT_20, tags=tags))
    assert plain and tagged and tagged[0].score > plain[0].score
    assert "audio tags" in tagged[0].reasons and "audio tags" not in plain[0].reasons


def test_music_damps_false_fights() -> None:
    quiet = timeline(60.0, FIGHT_AT_20)
    music = timeline(60.0, FIGHT_AT_20, tags=_tags_at(range(15, 30), {"music": 0.9, "speech": 0.4}))
    q, m = interest_curve(quiet), interest_curve(music)
    assert m[41] < q[41] * 0.7 and m[41] > 0  # damped, not erased
    assert find_candidates(music)[0].score < find_candidates(quiet)[0].score
    # below the threshold music does nothing; a gunshot tag in the same second cancels the damping
    low = timeline(60.0, FIGHT_AT_20, tags=_tags_at(range(60), {"music": 0.3}))
    assert np.array_equal(interest_curve(low), q)
    both = timeline(60.0, FIGHT_AT_20, tags=_tags_at(range(15, 30), {"music": 0.9, "gunshot": 0.6}))
    assert interest_curve(both)[41] >= q[41]


def test_music_on_a_voice_role_is_ignored() -> None:
    tl = timeline(60.0, FIGHT_AT_20, voice=[-30.0] * 60)
    for e in tl["seconds"]["mic"]:
        e["tags"] = {"music": 0.95}
    assert np.array_equal(interest_curve(tl), interest_curve(timeline(60.0, FIGHT_AT_20, voice=[-30.0] * 60)))


def test_v1_and_v2_timelines_both_work() -> None:
    v2 = timeline(60.0, FIGHT_AT_20)
    v2.update(version=2, whisper_device="cuda", tagger="panns-cnn14-16k")
    v1 = {k: v for k, v in v2.items() if k not in ("whisper_device", "tagger")} | {"version": 1}
    assert [c.to_dict() for c in find_candidates(v1)] == [c.to_dict() for c in find_candidates(v2)]
