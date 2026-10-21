"""Prompts and the text context (window, audio events, loudness, transcript) sent with the frames."""

from __future__ import annotations

import json
from pathlib import Path

from .schema import SUMMARY_MAX, TITLE_MAX, TYPES

_COMMON = f"""Games: Escape from Tarkov is "tarkov", Wardogs is "wardogs", Counter-Strike 2 (CS2 or CS:GO: buy
menu, bomb sites, maps like Dust II, Mirage, Inferno, Cache) is "cs2", anything else is "other".
Score 0-10: 0-3 nothing happens, 4-6 maybe, 7-8 good, 9-10 exceptional. Gunfights, kills, deaths with a
story, clutch plays and funny comms score high; menus, loading screens, inventory and walking with nothing
happening score low. Comms reactions ("clip that", laughing, shouting) are strong signals.
type: one of {", ".join(TYPES)}.
title: at most {TITLE_MAX} characters, specific and punchy, no hashtags, emoji or quotes.
summary: at most {SUMMARY_MAX} characters, one sentence saying what happens.
start/end: the tight clip inside the window in replay seconds (a little lead-in before the action, end after
the payoff). thumb_t: the replay second of the most striking frame.
Describe only what the frames, events and transcript show; never invent kills or names."""

SYSTEM_JUDGE = f"""You judge gameplay clips for a friend group's Discord. You get frames from one candidate
window of a replay (each labelled with its replay time), its audio events, loudness and voice transcript.
Decide whether the window is worth posting (keep) and fill in every field.
{_COMMON}"""

SYSTEM_POLISH = f"""You are the editor for a friend group's Discord gameplay clips. A first-pass judge already
chose to post this clip; its verdict is in the message. Write a better title and summary, tighten start/end
and thumb_t, and correct type, game or score if the frames show they are wrong. Keep keep=true unless the
clip is clearly worthless.
{_COMMON}"""

SYSTEM_REJUDGE = f"""You give the second opinion on a borderline gameplay clip for a friend group's Discord.
The first-pass verdict is in the message, but judge the frames, events and transcript yourself and decide
keep independently.
{_COMMON}"""

MAX_EVENT_LINES = 40
TRANSCRIPT_PAD_S = 5.0


def candidate_dict(candidate: object) -> dict:
    """Accept a candidates.Candidate or its to_dict()."""
    to_dict = getattr(candidate, "to_dict", None)
    return to_dict() if callable(to_dict) else dict(candidate)  # type: ignore[call-overload]


def context_text(replay_path: Path | str, cand: dict, timeline: dict, frame_ts: list[float]) -> str:
    start, end = float(cand["start"]), float(cand["end"])
    lines = [
        f"Replay: {Path(replay_path).name} ({float(timeline.get('duration', 0)):.0f} s long).",
        f"Candidate window: {start:.1f}-{end:.1f} s (heuristic score {float(cand.get('score', 0)):.2f}; "
        f"reasons: {', '.join(cand.get('reasons') or []) or 'none'}; "
        f"peak at {float(cand.get('peak_t', 0)):.1f} s).",
        f"start, end and thumb_t must lie within {start:.1f}-{end:.1f}.",
    ]
    if frame_ts:
        lines.append("Frames at: " + ", ".join(f"{t:.1f}" for t in frame_ts) + " s.")

    evs = [e for e in timeline.get("events") or [] if start <= float(e.get("t", -1)) <= end]
    trans = [e for e in evs if e.get("kind") == "transient"]
    other = [e for e in evs if e.get("kind") != "transient"]
    lines.append(f"Audio events in the window ({len(trans)} gunshot-like transients):")
    shown = sorted(other + trans[: max(0, MAX_EVENT_LINES - len(other))], key=lambda e: e["t"])
    for e in shown:
        if e.get("kind") == "transient":
            lines.append(
                f"  {e['t']:.1f} s transient strength {e.get('strength', 0):.2f} ({e.get('role', '')})"
            )
        else:
            lines.append(f'  {e["t"]:.1f} s {e.get("kind")} "{e.get("phrase", "")}" ({e.get("role", "")})')
    if len(trans) > len([e for e in shown if e.get("kind") == "transient"]):
        lines.append("  (more transients omitted)")

    seconds = timeline.get("seconds") or {}
    role = (timeline.get("sources") or {}).get("transients") or next(iter(seconds), None)
    if role in seconds:
        rms = [s for s in seconds[role] if start <= s["t"] <= end]
        if rms:
            lines.append(
                f"Loudness of the {role} track, dBFS per second from {rms[0]['t']} s: "
                + " ".join(str(round(s["rms_db"])) for s in rms)
            )

    said = [
        s for s in timeline.get("transcript") or []
        if s.get("end", 0) >= start - TRANSCRIPT_PAD_S and s.get("start", 0) <= end + TRANSCRIPT_PAD_S
    ]  # fmt: skip
    lines.append("Transcript:" if said else "Transcript: (no speech)")
    for s in said:
        lines.append(f"  [{s.get('role', '')} {s['start']:.1f}-{s['end']:.1f}] {s.get('text', '').strip()}")
    return "\n".join(lines)


def prior_verdict_text(verdict: dict, model: str) -> str:
    keys = ("keep", "score", "type", "title", "summary", "start", "end", "thumb_t", "game")
    return f"First-pass verdict ({model}): " + json.dumps({k: verdict[k] for k in keys})
