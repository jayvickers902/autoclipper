"""Post clips and status lines to Jay's private test webhook (`DISCORD_TEST_WEBHOOK_URL`) and nothing else.

The webhook URL is a secret: it is never printed, logged or put in an exception message; use `redact`.
With no webhook configured every post is skipped (a normal result, not an error).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from autoclipper.config import Settings
from autoclipper.media import EncodeResult, MediaError, share_encode

log = logging.getLogger(__name__)

MB = 1_000_000
UPLOAD_MAX_MB = 19.0  # attachments stay under this (Discord's free cap is 20 MB, older webhooks 10 MB)
FALLBACK_MB = 9.5  # re-encode target after an HTTP 413
_WEBHOOK_RE = re.compile(
    r"^https://(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(?P<id>\d+)/(?P<token>[A-Za-z0-9._-]+)/?$"
)


class _RedactFilter(logging.Filter):
    """httpx logs every request URL at INFO, which would leak the webhook token; rewrite those records."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        return True


logging.getLogger("httpx").addFilter(_RedactFilter())
logging.getLogger("httpcore").setLevel(logging.WARNING)  # its DEBUG/TRACE records carry request URLs


class InvalidWebhookError(ValueError):
    """The configured webhook URL is not a Discord webhook URL. The message never contains the URL."""


@dataclass(frozen=True)
class PostResult:
    status: str  # posted | skipped | refused | failed
    reason: str = ""
    http_status: int | None = None
    reencoded: bool = False
    message_id: str | None = None

    @property
    def ok(self) -> bool:
        """True when nothing went wrong: posted, or deliberately skipped (no webhook)."""
        return self.status in ("posted", "skipped")

    def __str__(self) -> str:
        return f"{self.status}: {self.reason}" if self.reason else self.status


def parse_webhook(url: str) -> tuple[str, str]:
    """Return (id, token) of a valid Discord webhook URL, else raise InvalidWebhookError (URL not echoed)."""
    m = _WEBHOOK_RE.match(url.strip())
    if not m:
        raise InvalidWebhookError("webhook URL must look like https://discord.com/api/webhooks/<id>/<token>")
    return m["id"], m["token"]


def redact(text: str, url: str = "") -> str:
    """Hide a webhook URL (and its token) in `text`; with no `url`, redact anything shaped like one."""
    if url:
        text = text.replace(url, "<webhook>")
        try:
            _, token = parse_webhook(url)
            text = text.replace(token, "***")
        except InvalidWebhookError:
            pass
    return re.sub(
        r"https://(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(\d+)/[^\s/?\"']+",
        r".../webhooks/\1/***",
        text,
    )


def redacted_url(url: str) -> str:
    """`.../webhooks/<id>/***` for a valid webhook, `<invalid webhook>` otherwise."""
    try:
        wid, _ = parse_webhook(url)
    except InvalidWebhookError:
        return "<invalid webhook>"
    return f".../webhooks/{wid}/***"


class PostCounter:
    """Persistent post counts (SQLite), shared by every process using the same file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, kind TEXT, ts REAL)")
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def count(self, kind: str) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM posts WHERE kind = ?", (kind,)).fetchone()[0]

    def reserve(self, kind: str, cap: int) -> int | None:
        """Atomically take a slot; None if `cap` slots of this kind are already used."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT COUNT(*) FROM posts WHERE kind = ?", (kind,)).fetchone()[0] >= cap:
                    db.execute("ROLLBACK")
                    return None
                cur = db.execute("INSERT INTO posts (kind, ts) VALUES (?, ?)", (kind, time.time()))
                db.execute("COMMIT")
                return cur.lastrowid
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def release(self, slot: int) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM posts WHERE id = ?", (slot,))


Encoder = Callable[..., EncodeResult]


class DiscordPoster:
    """Posts to the test webhook. One instance is one "run" for the per-run text cap."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        encoder: Encoder = share_encode,
        max_attempts: int = 4,
        max_sleep_s: float = 30.0,
        work_dir: Path | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.url = self.settings.discord_test_webhook_url.strip()
        self._client = client
        self._sleep = sleep
        self._encoder = encoder
        self.max_attempts = max_attempts
        self.max_sleep_s = max_sleep_s
        self.work_dir = work_dir or self.settings.data_dir / "discord-encodes"
        self.counter = PostCounter(self.settings.data_dir / "discord-posts.sqlite")
        self._texts_this_run = 0
        self._ready_at = 0.0  # monotonic time before which the bucket is exhausted

    # -- public API ---------------------------------------------------------------------------------

    def post_text(self, text: str) -> PostResult:
        pre = self._precheck()
        if pre:
            return pre
        if self._texts_this_run >= self.settings.post_max_texts:
            return PostResult("refused", f"text cap reached ({self.settings.post_max_texts} per run)")
        self._texts_this_run += 1
        payload = {"content": text[:2000], "allowed_mentions": {"parse": []}}
        return self._send(payload, None)

    def post_clip(
        self,
        path: Path | str,
        title: str,
        game: str,
        length_s: float,
        summary: str = "",
    ) -> PostResult:
        pre = self._precheck()
        if pre:
            return pre
        src = Path(path)
        if not src.is_file():
            return PostResult("failed", f"clip file not found: {src.name}")
        cap = self.settings.post_max_clips
        slot = self.counter.reserve("clip", cap)
        if slot is None:
            return PostResult("refused", f"clip cap reached ({cap} posted; AUTOCLIPPER_POST_MAX_CLIPS)")
        result = PostResult("failed", "not sent")
        try:
            result = self._post_clip(src, title, game, length_s, summary)
        finally:
            if result.status != "posted":
                self.counter.release(slot)
        return result

    # -- internals ----------------------------------------------------------------------------------

    def _precheck(self) -> PostResult | None:
        if not self.url:
            return PostResult("skipped", "no webhook")
        try:
            parse_webhook(self.url)
        except InvalidWebhookError as exc:
            return PostResult("failed", str(exc))
        return None

    def _post_clip(self, src: Path, title: str, game: str, length_s: float, summary: str) -> PostResult:
        send_path, reencoded = src, False
        if src.stat().st_size > UPLOAD_MAX_MB * MB:
            enc = self._reencode(src, length_s, UPLOAD_MAX_MB)
            if isinstance(enc, PostResult):
                return enc
            send_path, reencoded = enc, True
        payload = clip_payload(title, game, length_s, summary, send_path.name)
        result = self._send(payload, send_path, reencoded=reencoded)
        if result.http_status == 413 and send_path.stat().st_size > FALLBACK_MB * MB:
            log.warning("discord: 413 for %.1f MB; re-encoding to %.1f MB", _mb(send_path), FALLBACK_MB)
            enc = self._reencode(src, length_s, FALLBACK_MB)
            if isinstance(enc, PostResult):
                return enc
            payload = clip_payload(title, game, length_s, summary, enc.name)
            result = self._send(payload, enc, reencoded=True)
        return result

    def _reencode(self, src: Path, length_s: float, max_mb: float) -> Path | PostResult:
        out = self.work_dir / f"{src.stem}-{max_mb:g}mb.mp4"
        try:
            res = self._encoder(src, 0.0, length_s, out, max_mb=max_mb)
        except MediaError as exc:
            return PostResult("failed", f"re-encode to {max_mb:g} MB failed: {redact(str(exc), self.url)}")
        return res.path

    def _send(
        self, payload: dict[str, Any], attachment: Path | None, *, reencoded: bool = False
    ) -> PostResult:
        client = self._client or httpx.Client(timeout=120.0, follow_redirects=False)
        try:
            return self._send_with(client, payload, attachment, reencoded)
        finally:
            if self._client is None:
                client.close()

    def _send_with(
        self, client: httpx.Client, payload: dict[str, Any], attachment: Path | None, reencoded: bool
    ) -> PostResult:
        last = "no attempt made"
        for _ in range(self.max_attempts):
            self._wait_for_bucket()
            try:
                resp = self._request(client, payload, attachment)
            except httpx.HTTPError as exc:
                reason = redact(f"{type(exc).__name__}: {exc}", self.url)
                log.warning("discord: request failed (%s)", reason)
                return PostResult("failed", reason, reencoded=reencoded)
            self._note_bucket(resp)
            if resp.status_code == 429:
                delay = _retry_after(resp)
                last = f"rate limited (retry_after={delay:g}s)"
                if delay > self.max_sleep_s:
                    return PostResult("failed", last + ", too long to wait", 429, reencoded)
                log.info("discord: 429, sleeping %.2fs", delay)
                self._sleep(delay)
                continue
            if 200 <= resp.status_code < 300:
                return PostResult("posted", http_status=resp.status_code, reencoded=reencoded,
                                  message_id=_message_id(resp))  # fmt: skip
            reason = f"HTTP {resp.status_code}"
            if resp.status_code == 413:
                reason += " payload too large"
            log.warning("discord: %s on %s", reason, redacted_url(self.url))
            return PostResult("failed", reason, resp.status_code, reencoded)
        return PostResult("failed", f"gave up after {self.max_attempts} attempts: {last}", 429, reencoded)

    def _request(
        self, client: httpx.Client, payload: dict[str, Any], attachment: Path | None
    ) -> httpx.Response:
        data = {"payload_json": json.dumps(payload)}
        if attachment is None:
            return client.post(self.url, params={"wait": "true"}, data=data)
        with attachment.open("rb") as fh:
            files = {"files[0]": (attachment.name, fh, "video/mp4")}
            return client.post(self.url, params={"wait": "true"}, data=data, files=files)

    def _wait_for_bucket(self) -> None:
        delay = self._ready_at - time.monotonic()
        if 0 < delay <= self.max_sleep_s:
            self._sleep(delay)

    def _note_bucket(self, resp: httpx.Response) -> None:
        """If the bucket is empty, hold the next request until it resets."""
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            try:
                self._ready_at = time.monotonic() + float(resp.headers.get("X-RateLimit-Reset-After", "1"))
            except ValueError:
                self._ready_at = time.monotonic() + 1.0


def clip_payload(title: str, game: str, length_s: float, summary: str, filename: str) -> dict[str, Any]:
    """The `payload_json` for a clip post: an embed (title, summary, game, length) plus the attachment."""
    embed: dict[str, Any] = {
        "title": title[:256],
        "fields": [
            {"name": "Game", "value": (game or "?")[:1024], "inline": True},
            {"name": "Length", "value": f"{length_s:.0f}s", "inline": True},
        ],
    }
    if summary:
        embed["description"] = summary[:4096]
    return {
        "embeds": [embed],
        "attachments": [{"id": 0, "filename": filename}],
        "allowed_mentions": {"parse": []},
    }


def _mb(path: Path) -> float:
    return path.stat().st_size / MB


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.json()["retry_after"]))
    except (ValueError, KeyError, TypeError):
        pass
    try:
        return max(0.0, float(resp.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0


def _message_id(resp: httpx.Response) -> str | None:
    try:
        mid = resp.json().get("id")
    except (ValueError, AttributeError):
        return None
    return str(mid) if mid else None


def post_text(text: str, settings: Settings | None = None, **kw: Any) -> PostResult:
    return DiscordPoster(settings, **kw).post_text(text)


def post_clip(
    path: Path | str,
    title: str,
    game: str,
    length_s: float,
    summary: str = "",
    settings: Settings | None = None,
    **kw: Any,
) -> PostResult:
    return DiscordPoster(settings, **kw).post_clip(path, title, game, length_s, summary)
