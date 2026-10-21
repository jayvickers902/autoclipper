"""Keyword spotting over timed words. Edit KEYWORDS to change what counts as a hype phrase."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

KEYWORDS: tuple[str, ...] = (
    "clip that", "clip it", "clip this",
    "no way", "let's go", "oh my god", "holy shit", "what the",
    "nice shot", "nice", "get him", "he's down", "he's dead", "one shot",
)  # fmt: skip

FUZZY_MIN_LEN = 4  # shorter tokens must match exactly
FUZZY_RATIO = 0.8  # "click" ~ "clip", "lets" ~ "let's" (apostrophes are stripped first)


def normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower().replace("’", "'"))


def _token_match(heard: str, want: str) -> bool:
    if heard == want:
        return True
    if min(len(heard), len(want)) < FUZZY_MIN_LEN:
        return False
    return SequenceMatcher(None, heard, want).ratio() >= FUZZY_RATIO


def find_keywords(words: list[dict], keywords: tuple[str, ...] = KEYWORDS) -> list[dict]:
    """Match phrases over consecutive words (each {"start", "end", "word"}).

    Returns [{"t", "end", "phrase", "text"}] sorted by time. When phrases overlap, the longer one wins
    ("nice shot" beats "nice").
    """
    heard = [normalize(w["word"]) for w in words]
    wanted = sorted(((k, [normalize(p) for p in k.split()]) for k in keywords), key=lambda kv: -len(kv[1]))
    hits: list[tuple[int, int, str]] = []  # (first word index, last word index, phrase)
    for phrase, toks in wanted:
        for i in range(len(heard) - len(toks) + 1):
            if all(_token_match(heard[i + j], tok) for j, tok in enumerate(toks)):
                hits.append((i, i + len(toks) - 1, phrase))
    taken: set[int] = set()
    kept = []
    for a, b, phrase in sorted(hits, key=lambda h: (-(h[1] - h[0]), h[0])):
        span = set(range(a, b + 1))
        if span & taken:
            continue
        taken |= span
        kept.append((a, b, phrase))
    return [
        {
            "t": round(words[a]["start"], 2),
            "end": round(words[b]["end"], 2),
            "phrase": phrase,
            "text": " ".join(w["word"].strip() for w in words[a : b + 1]),
        }
        for a, b, phrase in sorted(kept)
    ]
