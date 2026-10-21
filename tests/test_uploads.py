import hashlib
import http.client
import json
import os
import socket
import threading

import pytest

from autoclipper.config import Settings
from autoclipper.safety import WriteRefused
from autoclipper.uploads import (
    authenticate,
    make_server,
    parse_tokens,
    safe_filename,
    serve,
)

JAY = "jay-token-0123456789abcdef"
BOB = "bob-token-0123456789abcdef"
DATA = os.urandom(300_000)
SHA = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def hub(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        upload_tokens=f"jay:{JAY}, bob:{BOB}",
        upload_max_gb=0.001,  # ~1 MiB
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    srv = make_server(settings, sock, quiet=True)
    srv.max_chunk = 128 * 1024
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, tmp_path / "data" / "inbox"
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=b"", token=JAY, headers=None):
    conn = http.client.HTTPConnection(*srv.server_address[:2], timeout=10)
    h = {"Content-Length": str(len(body))}
    if token:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    conn.close()
    return out


def meta(**kw):
    m = {
        "version": 1,
        "sha256": SHA,
        "size": len(DATA),
        "filename": "2026-10-21 20-15-03.mp4",
        "game": "Escape from Tarkov",
        "contentType": "Session",
        "bookmarks": [{"type": "Manual", "subtype": None, "timeSeconds": 12.5}],
        "audioTrackNames": ["Full Mix", "Mic", "Game Audio", "Voice Chat"],
    }
    m.update(kw)
    return json.dumps(m).encode()


def put(srv, offset, chunk, sha=SHA, token=JAY):
    return call(srv, "PUT", f"/v1/uploads/{sha}?offset={offset}", chunk, token=token)


def upload_all(srv, chunk=100_000):
    st, body = call(srv, "POST", "/v1/uploads", meta())
    assert st == 200
    off = body["offset"]
    while body["state"] != "complete":
        st, body = put(srv, off, DATA[off : off + chunk])
        assert st == 200, body
        off = body["offset"]
    return body


def test_parse_tokens_and_auth():
    tokens = parse_tokens(f"Jay:{JAY},bob:{BOB},")
    assert tokens == {JAY: "jay", BOB: "bob"}
    assert authenticate(f"Bearer {BOB}", tokens) == "bob"
    assert authenticate(f"bearer {JAY}", tokens) == "jay"
    assert authenticate("Bearer nope", tokens) is None
    assert authenticate(None, tokens) is None
    assert parse_tokens("") == {}
    for bad in ("jay:short", "nocolon", f"bad user:{JAY}", f"a:{JAY},b:{JAY}"):
        with pytest.raises(ValueError):
            parse_tokens(bad)


def test_safe_filename():
    assert safe_filename("C:\\Videos\\a b.mp4") == "a b.mp4"
    assert safe_filename("../../etc/x.MKV") == "x.MKV"
    assert safe_filename("x.exe") is None
    assert safe_filename(".mp4") is None
    assert safe_filename(5) is None


def test_full_upload_lands_atomically(hub):
    srv, inbox = hub
    assert upload_all(srv) == {"state": "complete", "offset": len(DATA)}
    final = inbox / "jay" / SHA
    assert (final / "2026-10-21 20-15-03.mp4").read_bytes() == DATA
    m = json.loads((final / "meta.json").read_text())
    assert m["user"] == "jay" and m["sha256"] == SHA and m["game"] == "Escape from Tarkov"
    assert m["bookmarks"][0]["timeSeconds"] == 12.5 and m["file"] == "2026-10-21 20-15-03.mp4"
    assert not (inbox / ".staging" / "jay" / SHA).exists()
    assert sorted(p.name for p in final.iterdir()) == ["2026-10-21 20-15-03.mp4", "meta.json"]


def test_idempotent_after_complete(hub):
    srv, inbox = hub
    upload_all(srv)
    assert call(srv, "POST", "/v1/uploads", meta()) == (200, {"state": "complete", "offset": len(DATA)})
    assert put(srv, 0, DATA[:1000]) == (200, {"state": "complete", "offset": len(DATA)})
    assert (inbox / "jay" / SHA / "2026-10-21 20-15-03.mp4").read_bytes() == DATA


def test_resume_and_offset_conflict(hub):
    srv, inbox = hub
    assert call(srv, "POST", "/v1/uploads", meta()) == (200, {"state": "partial", "offset": 0})
    assert put(srv, 0, DATA[:50_000]) == (200, {"state": "partial", "offset": 50_000})
    # a retried chunk (offset already passed) gets 409 with the offset to continue from
    st, body = put(srv, 0, DATA[:50_000])
    assert st == 409 and body["offset"] == 50_000
    # a recorder restart re-POSTs and resumes where the hub is
    assert call(srv, "POST", "/v1/uploads", meta()) == (200, {"state": "partial", "offset": 50_000})
    st, body = put(srv, 50_000, DATA[50_000 : 50_000 + 128 * 1024 + 1])
    assert st == 413  # over the (test) chunk cap
    st, body = put(srv, 50_000, DATA[50_000:150_000])
    assert (st, body) == (200, {"state": "partial", "offset": 150_000})
    assert put(srv, 150_000, DATA[150_000:250_000])[1]["offset"] == 250_000
    assert put(srv, 250_000, DATA[250_000:] + b"extra")[0] == 400  # past the declared size
    assert put(srv, 250_000, DATA[250_000:]) == (200, {"state": "complete", "offset": len(DATA)})
    assert (inbox / "jay" / SHA / "2026-10-21 20-15-03.mp4").read_bytes() == DATA


def test_hash_mismatch_discards(hub):
    srv, inbox = hub
    call(srv, "POST", "/v1/uploads", meta())
    bad = bytearray(DATA)
    bad[-1] ^= 0xFF
    put(srv, 0, bytes(bad[:100_000]))
    put(srv, 100_000, bytes(bad[100_000:200_000]))
    st, body = put(srv, 200_000, bytes(bad[200_000:]))
    assert st == 422 and "mismatch" in body["error"]
    assert not (inbox / "jay" / SHA).exists()
    # the staged metadata survives, so the recorder can start over from offset 0
    assert call(srv, "POST", "/v1/uploads", meta()) == (200, {"state": "partial", "offset": 0})
    assert upload_all(srv)["state"] == "complete"


def test_auth_and_browser_rejected(hub):
    srv, _ = hub
    assert call(srv, "POST", "/v1/uploads", meta(), token=None)[0] == 401
    assert call(srv, "POST", "/v1/uploads", meta(), token="x" * 32)[0] == 401
    assert put(srv, 0, DATA[:10], token="x" * 32)[0] == 401
    st = call(srv, "POST", "/v1/uploads", meta(), headers={"Origin": "http://evil.example"})[0]
    assert st == 403
    assert call(srv, "GET", "/v1/uploads")[0] == 404


def test_users_are_separate(hub):
    srv, inbox = hub
    upload_all(srv)
    # bob has no staged upload under this hash, so a chunk without a POST is unknown
    assert put(srv, 0, DATA[:10], token=BOB)[0] == 404
    assert call(srv, "POST", "/v1/uploads", meta(), token=BOB)[1] == {"state": "partial", "offset": 0}


@pytest.mark.parametrize(
    ("override", "status"),
    [
        ({"sha256": "ABC"}, 400),
        ({"size": 0}, 400),
        ({"size": "12"}, 400),
        ({"filename": "evil.exe"}, 400),
        ({"size": 10 * 1024**2}, 413),
    ],
)
def test_bad_metadata(hub, override, status):
    srv, _ = hub
    assert call(srv, "POST", "/v1/uploads", meta(**override))[0] == status


def test_bad_requests(hub):
    srv, _ = hub
    assert call(srv, "POST", "/v1/uploads", b"not json")[0] == 400
    assert call(srv, "POST", "/v1/uploads", b"[1]")[0] == 400
    assert call(srv, "POST", "/v1/elsewhere", meta())[0] == 404
    assert call(srv, "PUT", f"/v1/uploads/{SHA}", b"abc")[0] == 400
    assert put(srv, 0, b"abc", sha="0" * 64)[0] == 404


def test_size_change_conflicts(hub):
    srv, _ = hub
    call(srv, "POST", "/v1/uploads", meta())
    assert call(srv, "POST", "/v1/uploads", meta(size=len(DATA) + 1))[0] == 409


def test_finalize_on_repost_after_crash(hub):
    srv, inbox = hub
    call(srv, "POST", "/v1/uploads", meta())
    # simulate a hub that stored every byte but died before the final rename
    (inbox / ".staging" / "jay" / SHA / "data.part").write_bytes(DATA)
    assert call(srv, "POST", "/v1/uploads", meta()) == (200, {"state": "complete", "offset": len(DATA)})
    assert (inbox / "jay" / SHA / "2026-10-21 20-15-03.mp4").read_bytes() == DATA


def test_inbox_outside_data_dir_refused_at_startup(tmp_path):
    s = Settings(
        data_dir=tmp_path / "data", output_dir=tmp_path / "out", replay_dir=tmp_path / "replays",
        upload_tokens=f"jay:{JAY}", upload_inbox=tmp_path / "elsewhere",
    )  # fmt: skip
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        with pytest.raises(WriteRefused):
            make_server(s, sock)
    finally:
        sock.close()
    assert not (tmp_path / "elsewhere").exists()


def test_off_by_default(monkeypatch, tmp_path):
    for k in ("AUTOCLIPPER_UPLOAD_PORT", "AUTOCLIPPER_UPLOAD_TOKENS", "AUTOCLIPPER_UPLOAD_INBOX"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env(dotenv=False)
    assert s.upload_port == 0 and s.upload_host == "127.0.0.1" and s.upload_inbox is None
    assert "upload_tokens" not in repr(s)
    with pytest.raises(SystemExit, match="off"):
        serve(s)
    monkeypatch.setenv("AUTOCLIPPER_UPLOAD_PORT", "1")
    monkeypatch.setenv("AUTOCLIPPER_UPLOAD_INBOX", str(tmp_path / "in"))
    s = Settings.from_env(dotenv=False)
    assert s.upload_port == 1 and s.upload_inbox == tmp_path / "in"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        with pytest.raises(SystemExit, match="TOKENS"):
            make_server(s, sock)
    finally:
        sock.close()
