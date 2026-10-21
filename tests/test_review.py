from __future__ import annotations

import http.client
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from autoclipper.cli import main
from autoclipper.config import Settings
from autoclipper.review import make_server, parse_range, resolve_media
from autoclipper.store import SCHEMA_V1, Store

VIDEO = bytes(range(256)) * 8  # 2048 bytes


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    replays = tmp_path / "replays"
    replays.mkdir()
    return Settings(replay_dir=replays, output_dir=replays / "autoclipper", data_dir=tmp_path / "data")


@pytest.fixture
def ids(settings: Settings) -> dict[str, int]:
    """Replay 1: kept+clip, discarded, pending candidates. Replay 2: dead."""
    out = settings.output_dir / "Replay A"
    out.mkdir(parents=True)
    paths = {}
    for key, name, data in [
        ("share_path", "c_share.mp4", VIDEO), ("cut_path", "c_cut.mp4", b"cut"),
        ("thumb_path", "c_thumb.jpg", b"\xff\xd8jpg"), ("webp_path", "c_preview.webp", b"RIFFwebp"),
    ]:  # fmt: skip
        (out / name).write_bytes(data)
        paths[key] = str(out / name)
    with Store(settings) as st:
        a = st.upsert_replay(settings.replay_dir / "Replay A.mp4", "a" * 64, 10, 1.0)["id"]
        b = st.upsert_replay(settings.replay_dir / "Replay B.mp4", "b" * 64, 10, 2.0)["id"]
        st.update_replay(a, status="done", duration=125.0)
        st.update_replay(b, status="dead", dead=1, dead_reason="silent <menu>", duration=30.0)
        st.record_stage(a, "judge", 3.25)
        st.add_candidates(a, [
            {"start": 10.0, "end": 40.0, "score": 0.9},
            {"start": 100.0, "end": 120.0, "score": 0.4},
            {"start": 200.0, "end": 230.0, "score": 0.2},
        ])  # fmt: skip
        c1, c2, c3 = (c["id"] for c in st.candidates(a))
        st.set_judgement(c1, "judged", model="haiku", cost_usd=0.0123, verdict={
            "keep": True, "score": 8, "type": "firefight", "title": "Big <b>fight</b>", "summary": "won it",
        })  # fmt: skip
        st.set_judgement(c2, "judged", model="haiku", cost_usd=0.01, verdict={
            "keep": False, "score": 2, "type": "other", "title": "Nothing", "summary": "boring",
        })  # fmt: skip
        clip = st.save_clip(c1, status="ok", share_bytes=len(VIDEO), encoder="libx264", **paths)
    return {"replay": a, "dead": b, "keep": c1, "drop": c2, "pending": c3, "clip": clip}


@pytest.fixture
def server(settings: Settings, ids: dict[str, int]) -> Iterator[int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))  # port 0: never the registered 4180
    sock.listen()
    srv = make_server(settings, sock, quiet=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield sock.getsockname()[1]
    srv.shutdown()
    srv.server_close()
    t.join(5)


def request(port: int, method: str, path: str, body: str | None = None, **headers: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    if body is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def get(port: int, path: str, **headers: str):
    resp, data = request(port, "GET", path, **headers)
    return resp, data.decode("utf-8", "replace") if resp.getheader("Content-Type", "").startswith(
        "text"
    ) else data


def test_replays_page(server: int):
    resp, html = get(server, "/")
    assert resp.status == 200
    assert "Replay A.mp4" in html and "Replay B.mp4" in html
    assert "2:05" in html and "judge&nbsp;3.2s" in html or "judge&nbsp;3.3s" in html
    assert "silent &lt;menu&gt;" in html and "silent <menu>" not in html  # escaped
    assert "To label (2)" in html


def test_replay_page(server: int, ids: dict[str, int]):
    resp, html = get(server, f"/replay/{ids['replay']}")
    assert resp.status == 200
    assert "Big &lt;b&gt;fight&lt;/b&gt;" in html and "Big <b>fight</b>" not in html
    assert "won it" in html and "haiku" in html and "$0.0123" in html and "signal 0.90" in html
    assert "judge score 8" in html and "firefight" in html and ">keep<" in html and ">discard<" in html
    assert html.count("<video") == 1 and 'src="/media/Replay%20A/c_share.mp4"' in html
    assert 'poster="/media/Replay%20A/c_thumb.jpg"' in html and "/media/Replay%20A/c_preview.webp" in html
    assert "(not judged)" in html and "pending" in html
    assert html.count('class="lab"') == 3
    assert get(server, "/replay/999")[0].status == 404
    assert get(server, "/nope")[0].status == 404


def test_clips_page(server: int):
    resp, html = get(server, "/clips")
    assert resp.status == 200 and html.count("<video") == 1 and "Big &lt;b&gt;" in html


def test_label_roundtrip_and_tolabel_filter(server: int, settings: Settings, ids: dict[str, int]):
    _, html = get(server, "/tolabel")
    assert "Nothing" in html and "Big &lt;b&gt;fight" in html and "boring" in html
    assert "(not judged)" not in html  # the pending candidate is not offered

    resp, _ = request(server, "POST", "/label", f"candidate_id={ids['keep']}&label=up&next=/tolabel")
    assert resp.status == 303 and resp.getheader("Location") == "/tolabel"
    resp, body = request(
        server, "POST", "/label", f"candidate_id={ids['drop']}&label=down", **{"X-Requested-With": "fetch"}
    )
    assert resp.status == 200

    with Store(settings) as st:
        rows = st.con.execute("SELECT candidate_id, clip_id, label FROM labels ORDER BY id").fetchall()
        assert [(r["candidate_id"], r["clip_id"], r["label"]) for r in rows] == [
            (ids["keep"], ids["clip"], "up"),
            (ids["drop"], None, "down"),  # discarded candidates have no clip but can be labelled
        ]
    _, html = get(server, "/tolabel")
    assert "Nothing left to label." in html and "To label (0)" in html
    _, html = get(server, f"/replay/{ids['replay']}")
    assert html.count('class="on"') == 2  # the chosen thumb is highlighted on each labelled card

    # relabel replaces (latest wins); repeating the same label adds no row
    request(server, "POST", "/label", f"candidate_id={ids['keep']}&label=down")
    request(server, "POST", "/label", f"candidate_id={ids['keep']}&label=down")
    with Store(settings) as st:
        assert st.latest_labels()[ids["keep"]] == "down"
        assert st.con.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 3


def test_label_rejects_bad_input(server: int, settings: Settings, ids: dict[str, int]):
    for body in ["candidate_id=1&label=sideways", "label=up", "candidate_id=x&label=up", ""]:
        assert request(server, "POST", "/label", body)[0].status == 400
    assert request(server, "POST", "/label", "candidate_id=9999&label=up")[0].status == 404
    assert request(server, "POST", "/other", "a=b")[0].status == 404
    # cross-site POST and rebinding Host are refused
    ok = f"candidate_id={ids['keep']}&label=up"
    assert request(server, "POST", "/label", ok, Origin="http://evil.example")[0].status == 403
    assert request(server, "POST", "/label", ok, Host="evil.example")[0].status == 403
    assert get(server, "/", Host="evil.example")[0].status == 403
    assert get(server, "/", Host="autoclipper.localhost")[0].status == 200
    # open redirects are not followed
    resp, _ = request(server, "POST", "/label", ok + "&next=//evil.example/x")
    assert resp.getheader("Location") == "/"
    with Store(settings) as st:
        assert st.con.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1


def test_video_range(server: int):
    url = "/media/Replay%20A/c_share.mp4"
    resp, data = get(server, url)
    assert resp.status == 200 and data == VIDEO
    assert resp.getheader("Accept-Ranges") == "bytes" and resp.getheader("Content-Type") == "video/mp4"

    resp, data = get(server, url, Range="bytes=100-199")
    assert resp.status == 206 and data == VIDEO[100:200]
    assert resp.getheader("Content-Range") == f"bytes 100-199/{len(VIDEO)}"
    assert resp.getheader("Content-Length") == "100"

    resp, data = get(server, url, Range="bytes=2000-")
    assert resp.status == 206 and data == VIDEO[2000:]
    resp, data = get(server, url, Range="bytes=-10")
    assert resp.status == 206 and data == VIDEO[-10:]
    resp, data = get(server, url, Range="bytes=0-99999")  # end clamped
    assert resp.status == 206 and data == VIDEO
    resp, _ = get(server, url, Range=f"bytes={len(VIDEO)}-")
    assert resp.status == 416 and resp.getheader("Content-Range") == f"bytes */{len(VIDEO)}"
    resp, data = get(server, url, Range="bytes=0-1,5-6")  # multi-range: served whole
    assert resp.status == 200 and data == VIDEO
    resp, data = request(server, "HEAD", url)
    assert resp.status == 200 and data == b"" and resp.getheader("Content-Length") == str(len(VIDEO))


def test_media_refuses_everything_outside_output_dir(server: int, settings: Settings, tmp_path: Path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"secret")
    replay = settings.replay_dir / "Replay A.mp4"
    replay.write_bytes(b"original replay")
    (settings.output_dir / "sneaky.mp4").symlink_to(secret)  # symlink out of output_dir
    (settings.output_dir / "linkdir").symlink_to(tmp_path)  # directory symlink out
    (settings.output_dir / "notes.txt").write_text("not media")
    attempts = [
        "/media/../Replay%20A.mp4",
        "/media/%2e%2e/Replay%20A.mp4",
        "/media/Replay%20A/../../secret.mp4",
        "/media/..%2f..%2fsecret.mp4",
        f"/media/{secret}",  # absolute path
        f"/media/%2f{str(secret).lstrip('/')}",
        f"/media//{str(secret).lstrip('/')}",
        "/media/sneaky.mp4",
        "/media/linkdir/secret.mp4",
        "/media/notes.txt",  # inside, but not a media type
        "/media/Replay%20A",  # a directory
        "/media/",
        "/media/Replay%20A/c_share.mp4%00.jpg",
        "/media/Replay%20A%5Cc_share.mp4",
    ]
    for url in attempts:
        # http.client sends the raw target, so `..` reaches the server un-normalised
        resp, data = request(server, "GET", url)
        assert resp.status in (403, 404), url
        assert b"secret" not in data and b"original replay" not in data, url
    assert get(server, "/media/Replay%20A/c_thumb.jpg")[0].status == 200


def test_resolve_media_unit(settings: Settings, ids: dict[str, int], tmp_path: Path):
    assert resolve_media(settings.output_dir, "Replay A/c_share.mp4") is not None
    assert resolve_media(settings.output_dir, "../Replay A.mp4") is None
    assert resolve_media(settings.output_dir, "") is None
    # the replay dir itself is never served, even if a file there has a media extension
    (settings.replay_dir / "x.mp4").write_bytes(b"x")
    assert resolve_media(settings.output_dir, "../x.mp4") is None


def test_parse_range():
    assert parse_range(None, 10) is None
    assert parse_range("bytes=2-4", 10) == (2, 4)
    assert parse_range("bytes=2-", 10) == (2, 9)
    assert parse_range("bytes=-3", 10) == (7, 9)
    assert parse_range("bytes=-30", 10) == (0, 9)
    assert parse_range("items=1-2", 10) is None
    assert parse_range("bytes=1-2,4-5", 10) is None
    for bad in ["bytes=10-", "bytes=5-2", "bytes=-0"]:
        with pytest.raises(ValueError):
            parse_range(bad, 10)


def test_read_only_apart_from_labels(server: int, settings: Settings):
    def snapshot() -> dict[str, list]:
        with Store(settings) as st:
            return {
                t: [tuple(r) for r in st.con.execute(f"SELECT * FROM {t} ORDER BY 1")]  # noqa: S608
                for t in ("replays", "stages", "candidates", "clips")
            }

    before = snapshot()
    for path in ["/", "/clips", "/tolabel", "/replay/1"]:
        get(server, path)
    request(server, "POST", "/label", "candidate_id=1&label=up")
    assert snapshot() == before


def test_label_migration_from_v1(settings: Settings, ids: dict[str, int]):
    import sqlite3

    # a v1 database with a clip-keyed label, then reopened by the current code
    path = settings.data_dir / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_V1)
    con.execute("PRAGMA user_version = 1")
    con.execute(
        "INSERT INTO replays(path, sha256, size, mtime, created_at, updated_at) VALUES('p','h',1,1,1,1)"
    )
    con.execute('INSERT INTO candidates(replay_id, idx, start, "end", score, data) VALUES(1,0,0,1,1,"{}")')
    con.execute("INSERT INTO clips(candidate_id, status, created_at) VALUES(1,'ok',1)")
    con.execute("INSERT INTO labels(clip_id, label, created_at) VALUES(1,'up',1)")
    con.commit()
    con.close()
    with Store(settings, path) as st:
        assert st.version == 2
        assert st.latest_labels() == {1: "up"}


def test_serve_command_fails_if_port_taken(monkeypatch, tmp_path: Path, capsys):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AUTOCLIPPER_OUTPUT_DIR", str(tmp_path / "o"))
    try:
        with pytest.raises(SystemExit) as exc:
            main(["serve", "--port", str(taken.getsockname()[1])])
        assert "is taken" in str(exc.value)
    finally:
        taken.close()
