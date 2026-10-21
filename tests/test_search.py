"""Full-text search: v4 backfill, index upkeep on verdicts, ranking, hostile queries, CLI and review page."""

from __future__ import annotations

import http.client
import json
import socket
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from autoclipper import store as store_mod
from autoclipper.cli import main
from autoclipper.config import Settings
from autoclipper.review import make_server
from autoclipper.store import SCHEMA_VERSION, Store


def get(port: int, path: str, **headers: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    return resp, data


def verdict(title: str, summary: str = "", **kw: object) -> dict:
    return {"keep": True, "score": 7, "type": "firefight", "game": "tarkov", "title": title,
            "summary": summary, **kw}  # fmt: skip


def write_timeline(path: Path, lines: list[tuple[float, float, str]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "transcript": [
        {"role": "mic", "start": s, "end": e, "text": t, "words": []} for s, e, t in lines
    ]}))  # fmt: skip
    return str(path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    replays = tmp_path / "replays"
    replays.mkdir()
    return Settings(replay_dir=replays, output_dir=replays / "autoclipper", data_dir=tmp_path / "data")


@pytest.fixture
def populated(settings: Settings) -> dict[str, int]:
    tl = write_timeline(settings.data_dir / "timelines" / "a.json", [
        (11.0, 13.0, "watch the stairwell, he is flanking"),
        (105.0, 108.0, "reloading behind the dumpster"),
        (500.0, 502.0, "outside every window"),
    ])  # fmt: skip
    out = settings.output_dir
    out.mkdir(parents=True)
    (out / "share.mp4").write_bytes(b"x")
    with Store(settings) as st:
        rid = st.upsert_replay(settings.replay_dir / "Replay A.mp4", "a" * 64, 10, 1.0)["id"]
        st.update_replay(rid, timeline_path=tl, status="done")
        st.add_candidates(rid, [
            {"start": 10.0, "end": 40.0, "score": 0.9},
            {"start": 100.0, "end": 120.0, "score": 0.4},
            {"start": 200.0, "end": 230.0, "score": 0.2},
        ])  # fmt: skip
        c1, c2, c3 = (c["id"] for c in st.candidates(rid))
        st.set_judgement(c1, "judged", verdict=verdict("Sniper duel at the pier", "long range"))
        st.set_judgement(c2, "judged", verdict=verdict("Quiet reload", "firefight in the yard", type="other"))
        st.save_clip(c1, status="ok", share_path=str(out / "share.mp4"))
    return {"replay": rid, "c1": c1, "c2": c2, "c3": c3}


def test_judging_updates_the_index(populated: dict[str, int], settings: Settings) -> None:
    with Store(settings) as st:
        hits = st.search("stairwell")  # transcript words from c1's window only
        assert [h["candidate_id"] for h in hits] == [populated["c1"]]
        assert st.search("dumpster")[0]["candidate_id"] == populated["c2"]
        assert st.search("outside") == []  # spoken outside every candidate window
        assert st.search("pending") == [] and st.search("sniper")[0]["title"] == "Sniper duel at the pier"
        # a new verdict makes the third candidate findable; a re-judge replaces, a failure removes
        st.set_judgement(populated["c3"], "judged", verdict=verdict("Extract camping"))
        assert st.search("camping")[0]["candidate_id"] == populated["c3"]
        st.set_judgement(populated["c3"], "judged", verdict=verdict("Bridge ambush"))
        assert st.search("camping") == [] and len(st.search("ambush")) == 1
        st.set_judgement(populated["c3"], "failed", error="boom")
        assert st.search("ambush") == []
        assert st.con.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 2


def test_ranking_title_beats_transcript_and_clip_path(populated: dict[str, int], settings: Settings) -> None:
    with Store(settings) as st:
        # "firefight" is c1's type, c2's summary; "reload" is c2's title and c2's transcript
        hits = st.search("reload")
        assert hits[0]["candidate_id"] == populated["c2"] and hits[0]["score"] >= 0
        hits = st.search("firefight yard")  # AND: only c2 has both
        assert [h["candidate_id"] for h in hits] == [populated["c2"]]
        ranked = st.search("firefight")
        assert {h["candidate_id"] for h in ranked} == {populated["c1"], populated["c2"]}
        assert [h["score"] for h in ranked] == sorted((h["score"] for h in ranked), reverse=True)
        c1 = next(h for h in ranked if h["candidate_id"] == populated["c1"])
        assert c1["clip_path"].endswith("share.mp4") and (c1["start"], c1["end"]) == (10.0, 40.0)
        assert next(h for h in ranked if h["candidate_id"] == populated["c2"])["clip_path"] == ""
        # AND finds nothing -> OR fallback still ranks the partial matches
        assert st.search("stairwell zeppelin")[0]["candidate_id"] == populated["c1"]
        assert len(st.search("firefight", limit=1)) == 1


def test_title_outranks_summary_outranks_speech(settings: Settings, tmp_path: Path) -> None:
    tl = write_timeline(tmp_path / "t.json", [(300.0, 301.0, "ambush ambush ambush")])
    with Store(settings) as st:
        rid = st.upsert_replay(settings.replay_dir / "R.mp4", "b" * 64, 1, 1.0)["id"]
        st.update_replay(rid, timeline_path=tl)
        st.add_candidates(rid, [{"start": i * 100.0, "end": i * 100.0 + 50, "score": 0.5} for i in range(8)])
        ids = [c["id"] for c in st.candidates(rid)]
        for i, cid in enumerate(ids):  # candidate 3 covers the speech (300-301)
            by_title, by_summary = i == 0, i == 1
            st.set_judgement(cid, "judged", verdict=verdict(
                "Ambush at the bridge" if by_title else f"Filler number {i}",
                "an ambush happened" if by_summary else "nothing",
            ))  # fmt: skip
        hits = st.search("ambush")
        assert [h["candidate_id"] for h in hits] == [ids[0], ids[1], ids[3]]
        assert hits[0]["score"] > hits[1]["score"] > hits[2]["score"] > 0


def test_map_label_is_searchable(populated: dict[str, int], settings: Settings) -> None:
    with Store(settings) as st:
        st.set_segments(populated["replay"], [
            {"kind": "raid", "start": 0, "end": 60, "map_id": "customs", "map_name": "Customs"},
        ])  # fmt: skip
        assert st.search("customs") == []  # segments arrive after the verdict: not stale-proof...
        assert st.rebuild_search() == 2  # ...until a rebuild (or the next verdict)
        assert [h["candidate_id"] for h in st.search("customs")] == [populated["c1"]]
        st.set_judgement(populated["c2"], "judged", verdict=verdict("Quiet reload"))
        assert [h["candidate_id"] for h in st.search("customs")] == [populated["c1"]]


@pytest.mark.parametrize("q", [
    '"', "'", "AND", "OR NOT", "title:", "*", "(", "a OR", '" OR 1=1 --', "foo:bar baz*", "^x", "NEAR(a b)",
    "'; DROP TABLE candidates; --", "\x00\x02\x03", "ünï", "", "   ", "-", "{x}", "a" * 5000,
])  # fmt: skip
def test_hostile_queries_never_raise(populated: dict[str, int], settings: Settings, q: str) -> None:
    with Store(settings) as st:
        assert isinstance(st.search(q), list)
        assert st.con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3


def test_v3_database_is_backfilled(settings: Settings, tmp_path: Path) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / "old.sqlite"
    tl = write_timeline(tmp_path / "t.json", [(1.0, 2.0, "grenade over the wall")])
    con = sqlite3.connect(path)
    for script in store_mod.MIGRATIONS[:3]:
        con.executescript(script)
    con.execute("PRAGMA user_version = 3")
    con.execute("INSERT INTO replays(path, sha256, size, mtime, timeline_path, created_at, updated_at) "
                "VALUES('p','h',1,1,?,1,1)", (tl,))  # fmt: skip
    for i, (status, v) in enumerate([("judged", verdict("Wall bang")), ("pending", None)]):
        con.execute(
            'INSERT INTO candidates(replay_id, idx, start, "end", score, data, judge_status, verdict) '
            "VALUES(1,?,?,10,1,'{}',?,?)",
            (i, i, status, json.dumps(v) if v else None),
        )
    con.commit()
    con.close()
    with Store(settings, path) as st:
        assert st.version == SCHEMA_VERSION == 4
        assert [h["title"] for h in st.search("grenade")] == ["Wall bang"]
        assert st.con.execute("SELECT COUNT(*) FROM search").fetchone()[0] == 1


def test_cli_search(populated: dict[str, int], settings: Settings, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("AUTOCLIPPER_OUTPUT_DIR", str(settings.output_dir))
    assert main(["search", "sniper", "pier", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "Replay A.mp4" in out and "Sniper duel" in out and "share.mp4" in out and "0:10-0:40" in out
    assert main(["search", "--json", "stairwell"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["candidate_id"] == populated["c1"] and "[stairwell]" in data[0]["snippet"]
    assert main(["search", '"', "--reindex"]) == 0
    assert "no matches" in capsys.readouterr().out
    assert main(["search", "x", "--limit", "0"]) == 2


@pytest.fixture
def server(settings: Settings, populated: dict[str, int]) -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    srv = make_server(settings, sock, quiet=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield sock.getsockname()[1]
    srv.shutdown()
    srv.server_close()
    t.join(5)


def test_search_page(server: int) -> None:
    resp, body = get(server, "/search?q=stairwell")
    assert resp.status == 200
    assert "Sniper duel at the pier" in body and "<video" in body and "<mark>stairwell</mark>" in body
    assert 'name="q"' in body and 'value="stairwell"' in body
    resp, body = get(server, "/search?q=firefight")
    assert resp.status == 200 and "2 result(s)" in body
    resp, body = get(server, "/search?q=zzzznothing")
    assert resp.status == 200 and "No clips match" in body
    resp, body = get(server, "/search")
    assert resp.status == 200 and "Type words" in body


@pytest.mark.parametrize("q", ["%22", "AND%20OR", "x*", "%00", "a%22%20OR%20%221", "%ff%fe", "a" * 3000])
def test_search_page_survives_odd_queries(server: int, q: str) -> None:
    resp, _ = get(server, f"/search?q={q}")
    assert resp.status == 200


def test_search_page_escapes_everything(server: int, settings: Settings, populated: dict[str, int]) -> None:
    evil = "<script>alert(1)</script>"
    with Store(settings) as st:
        st.set_judgement(
            populated["c3"], "judged", verdict=verdict(f"{evil} zebra", f"<img src=x onerror=1> {evil}")
        )
    resp, body = get(server, "/search?q=" + "%3Cscript%3Ezebra%22%3E")
    assert resp.status == 200 and evil not in body and "&lt;script&gt;" in body and "<img src=x" not in body
    assert 'value="&lt;script&gt;zebra&quot;&gt;"' in body
    resp, body = get(server, "/search?q=zebra")
    assert evil not in body and "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<mark>zebra</mark>" in body
    # Host check still applies to the new route
    resp, _ = get(server, "/search?q=zebra", Host="evil.example")
    assert resp.status == 403
