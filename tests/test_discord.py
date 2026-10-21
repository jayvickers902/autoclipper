"""Discord poster tests: no network (httpx.MockTransport), never a real webhook."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from autoclipper import discord
from autoclipper.config import Settings
from autoclipper.discord import DiscordPoster, InvalidWebhookError, parse_webhook, redact, redacted_url
from autoclipper.media import EncodeResult

TOKEN = "SeCrEtTokEn_abc-123"
URL = f"https://discord.com/api/webhooks/123456789/{TOKEN}"


@pytest.fixture(autouse=True)
def _no_real_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TEST_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", "/nonexistent-never-used")


def settings(tmp_path: Path, url: str = URL, **kw: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", discord_test_webhook_url=url, **kw)  # type: ignore[arg-type]


class Server:
    """Scripted responses; records requests. Anything outside the scripted URL fails the test."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(URL), "request to an unexpected host"
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else httpx.Response(200, json={"id": "1"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def poster(tmp_path: Path, server: Server, sleeps: list[float] | None = None, **kw: object) -> DiscordPoster:
    sleeps = sleeps if sleeps is not None else []
    s = kw.pop("settings", None) or settings(tmp_path)
    return DiscordPoster(s, client=server.client(), sleep=sleeps.append, **kw)  # type: ignore[arg-type]


def clip(tmp_path: Path, size: int = 1000) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\0" * size)
    return p


def test_skips_without_webhook(tmp_path: Path) -> None:
    server = Server()
    p = poster(tmp_path, server, settings=settings(tmp_path, url=""))
    for res in (p.post_text("hi"), p.post_clip(clip(tmp_path), "t", "g", 5)):
        assert res.status == "skipped" and res.ok and "no webhook" in res.reason
    assert server.requests == []


@pytest.mark.parametrize(
    "bad",
    [
        "http://discord.com/api/webhooks/1/abc",
        "https://evil.example/api/webhooks/1/abc",
        "https://discord.com.evil.example/api/webhooks/1/abc",
        "https://discord.com/api/webhooks/notanid/abc",
        "https://discord.com/api/webhooks/1",
        "https://discord.com/api/webhooks/1/abc?x=https://discord.com",
    ],
)
def test_invalid_url_is_rejected_without_echo(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InvalidWebhookError) as ei:
        parse_webhook(bad)
    assert bad not in str(ei.value)
    server = Server()
    res = poster(tmp_path, server, settings=settings(tmp_path, url=bad)).post_text("hi")
    assert res.status == "failed" and not res.ok and bad not in str(res)
    assert server.requests == []


def test_valid_urls() -> None:
    assert parse_webhook(URL) == ("123456789", TOKEN)
    assert parse_webhook("https://discordapp.com/api/v10/webhooks/5/tok/")[0] == "5"


def test_redaction() -> None:
    assert redacted_url(URL) == ".../webhooks/123456789/***"
    assert redacted_url("nope") == "<invalid webhook>"
    out = redact(f"failed calling {URL}?wait=true: token {TOKEN}", URL)
    assert TOKEN not in out and URL not in out
    assert TOKEN not in redact(f"see {URL}")


def test_url_never_in_logs_or_results(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    p = DiscordPoster(settings(tmp_path), client=httpx.Client(transport=httpx.MockTransport(boom)))
    res = p.post_text("hi")
    assert res.status == "failed"
    assert TOKEN not in str(res) and TOKEN not in caplog.text and URL not in caplog.text
    res = poster(tmp_path, Server(httpx.Response(500))).post_text("hi")
    assert TOKEN not in str(res) and TOKEN not in caplog.text


def test_post_text_request_shape(tmp_path: Path) -> None:
    server = Server(httpx.Response(200, json={"id": "42"}))
    res = poster(tmp_path, server).post_text("hello @everyone")
    assert res.status == "posted" and res.message_id == "42"
    req = server.requests[0]
    assert req.url.params["wait"] == "true"
    payload = json.loads(parse_qs(req.content.decode())["payload_json"][0])
    assert payload == {"content": "hello @everyone", "allowed_mentions": {"parse": []}}


def test_post_clip_multipart(tmp_path: Path) -> None:
    server = Server()
    res = poster(tmp_path, server).post_clip(clip(tmp_path), "Big Win", "Tarkov", 12.4, "squad wipe")
    assert res.status == "posted" and not res.reencoded
    req = server.requests[0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    for needle in (b"payload_json", b"Big Win", b"Tarkov", b"12s", b"squad wipe", b'filename="clip.mp4"'):
        assert needle in req.content


def test_413_reencodes_and_retries_once(tmp_path: Path) -> None:
    calls: list[tuple] = []

    def encoder(path: Path, start: float, end: float, out: Path, max_mb: float = 19.0) -> EncodeResult:
        calls.append((Path(path), start, end, max_mb))
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\0" * 100)
        return EncodeResult(out, 100, "libx264", 500, 1)

    server = Server(httpx.Response(413, json={"message": "too large"}))
    p = poster(tmp_path, server, encoder=encoder, work_dir=tmp_path / "enc")
    src = clip(tmp_path, 12 * 1_000_000)
    res = p.post_clip(src, "t", "g", 30, "s")
    assert res.status == "posted" and res.reencoded
    assert calls == [(src, 0.0, 30, 9.5)]
    assert len(server.requests) == 2
    assert b"clip-9.5mb.mp4" in server.requests[1].content


def test_413_after_fallback_is_not_retried_again(tmp_path: Path) -> None:
    def encoder(path: Path, start: float, end: float, out: Path, max_mb: float = 19.0) -> EncodeResult:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\0" * 100)
        return EncodeResult(Path(out), 100, "libx264", 500, 1)

    server = Server(httpx.Response(413), httpx.Response(413))
    res = poster(tmp_path, server, encoder=encoder, work_dir=tmp_path / "enc").post_clip(
        clip(tmp_path, 12 * 1_000_000), "t", "g", 30
    )
    assert res.status == "failed" and res.http_status == 413 and len(server.requests) == 2
    # the failed post must not use up a clip slot
    assert poster(tmp_path, Server()).counter.count("clip") == 0


def test_oversize_clip_is_encoded_before_upload(tmp_path: Path) -> None:
    seen: list[float] = []

    def encoder(path: Path, start: float, end: float, out: Path, max_mb: float = 19.0) -> EncodeResult:
        seen.append(max_mb)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\0" * 100)
        return EncodeResult(Path(out), 100, "libx264", 500, 1)

    server = Server()
    p = poster(tmp_path, server, encoder=encoder, work_dir=tmp_path / "enc")
    res = p.post_clip(clip(tmp_path, 25 * 1_000_000), "t", "g", 30)
    assert res.status == "posted" and res.reencoded and seen == [19.0] and len(server.requests) == 1


def test_429_honors_retry_after(tmp_path: Path) -> None:
    sleeps: list[float] = []
    server = Server(httpx.Response(429, json={"retry_after": 1.5}), httpx.Response(200, json={"id": "9"}))
    res = poster(tmp_path, server, sleeps).post_text("hi")
    assert res.status == "posted" and sleeps == [1.5] and len(server.requests) == 2


def test_429_is_bounded(tmp_path: Path) -> None:
    sleeps: list[float] = []
    server = Server(*[httpx.Response(429, json={"retry_after": 0.1})] * 10)
    res = poster(tmp_path, server, sleeps, max_attempts=3).post_text("hi")
    assert res.status == "failed" and res.http_status == 429 and len(server.requests) == 3
    # an unreasonably long wait is not slept at all
    server = Server(httpx.Response(429, json={"retry_after": 3600}))
    sleeps.clear()
    res = poster(tmp_path, server, sleeps).post_text("hi")
    assert res.status == "failed" and sleeps == [] and len(server.requests) == 1


def test_exhausted_bucket_delays_next_request(tmp_path: Path) -> None:
    sleeps: list[float] = []
    hdr = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.0"}
    server = Server(httpx.Response(200, json={"id": "1"}, headers=hdr), httpx.Response(200, json={"id": "2"}))
    p = poster(tmp_path, server, sleeps)
    p.post_text("a")
    p.post_text("b")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 2.0


def test_clip_cap_is_persistent_across_processes(tmp_path: Path) -> None:
    s = settings(tmp_path, post_max_clips=2)
    results = []
    for _ in range(3):  # a fresh poster each time stands in for a fresh process
        server = Server()
        results.append(poster(tmp_path, server, settings=s).post_clip(clip(tmp_path), "t", "g", 5))
    assert [r.status for r in results] == ["posted", "posted", "refused"]
    assert "cap" in results[2].reason
    assert DiscordPoster(s).counter.count("clip") == 2


def test_text_cap_per_run_and_does_not_touch_clip_cap(tmp_path: Path) -> None:
    s = settings(tmp_path, post_max_texts=2, post_max_clips=1)
    server = Server()
    p = poster(tmp_path, server, settings=s)
    assert [p.post_text(str(i)).status for i in range(3)] == ["posted", "posted", "refused"]
    assert p.post_clip(clip(tmp_path), "t", "g", 5).status == "posted"


def test_env_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().post_max_clips == 5 and Settings().post_max_texts == 20
    monkeypatch.setenv("AUTOCLIPPER_POST_MAX_CLIPS", "3")
    monkeypatch.setenv("AUTOCLIPPER_POST_MAX_TEXTS", "7")
    s = Settings.from_env(dotenv=False)
    assert (s.post_max_clips, s.post_max_texts) == (3, 7)


def test_notify_cli_skips_without_webhook(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:  # fmt: skip
    from autoclipper.cli import main

    monkeypatch.setenv("AUTOCLIPPER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("autoclipper.config.load_dotenv", lambda path=None: None)
    assert main(["notify", "hi"]) == 0
    assert "skipped" in capsys.readouterr().out


def test_module_exports() -> None:
    assert callable(discord.post_text) and callable(discord.post_clip)
