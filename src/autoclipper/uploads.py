"""Upload receiver: recorders (the Segra fork) send recordings here instead of segra.tv.

Protocol (docs/UPLOADER.md), all requests carry `Authorization: Bearer <token>`:

- `POST /v1/uploads` with a JSON metadata body (`sha256`, `size`, `filename`, plus game, bookmarks, ...)
  creates or resumes an upload and answers `{"state": "partial"|"complete", "offset": n}`.
- `PUT /v1/uploads/<sha256>?offset=n` appends one chunk at `offset`. A wrong offset gets 409 with the
  current offset. The chunk that reaches `size` makes the hub verify the SHA-256 and move the upload into
  the inbox in one rename.

Everything is idempotent: the content hash names the upload, so re-sending a finished one is a no-op.
Finished uploads land in `<inbox>/<user>/<sha256>/` (the video plus `meta.json`); half-finished ones live
under `<inbox>/.staging/`, which the ingest step must skip.

Off by default: `serve` refuses to start unless AUTOCLIPPER_UPLOAD_PORT and AUTOCLIPPER_UPLOAD_TOKENS are set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from autoclipper.config import Settings, bind_socket

API = "/v1/uploads"
MAX_CHUNK = 64 * 1024 * 1024
MAX_META = 1024 * 1024
MIN_TOKEN_LEN = 16
FREE_SPACE_MARGIN = 2 * 1024**3
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm"}
SHA_RE = re.compile(r"[0-9a-f]{64}")
USER_RE = re.compile(r"[a-z0-9_-]{1,32}")
BLOCK = 1024 * 1024


class UploadError(Exception):
    def __init__(self, status: HTTPStatus, message: str, offset: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.offset = offset


def parse_tokens(spec: str) -> dict[str, str]:
    """`user:token,user2:token2` -> {token: user}. Raises ValueError on a malformed or short entry."""
    tokens: dict[str, str] = {}
    for entry in filter(None, (p.strip() for p in spec.split(","))):
        user, sep, token = entry.partition(":")
        user, token = user.strip().lower(), token.strip()
        if not sep or not USER_RE.fullmatch(user):
            raise ValueError("AUTOCLIPPER_UPLOAD_TOKENS: entries are user:token, user = [a-z0-9_-]{1,32}")
        if len(token) < MIN_TOKEN_LEN:
            raise ValueError(f"AUTOCLIPPER_UPLOAD_TOKENS: {user}'s token is under {MIN_TOKEN_LEN} chars")
        if token in tokens:
            raise ValueError("AUTOCLIPPER_UPLOAD_TOKENS: two users share a token")
        tokens[token] = user
    return tokens


def authenticate(header: str | None, tokens: dict[str, str]) -> str | None:
    """The user for a `Bearer <token>` header, compared in constant time; None if it matches nobody."""
    scheme, _, given = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not given:
        return None
    user = None
    for token, name in tokens.items():
        if hmac.compare_digest(token.encode(), given.strip().encode()):
            user = name
    return user


def safe_filename(name: Any) -> str | None:
    """A plain basename with a video extension, or None."""
    if not isinstance(name, str):
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._() -]", "_", base)[:160]
    if not base or base.startswith(".") or Path(base).suffix.lower() not in VIDEO_EXTS:
        return None
    return base


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(8 * BLOCK):
            h.update(block)
    return h.hexdigest()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Inbox:
    """Staging and finished uploads under one root (one filesystem, so the final rename is atomic)."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def lock(self, user: str, sha: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault((user, sha), threading.Lock())

    def final_dir(self, user: str, sha: str) -> Path:
        return self.root / user / sha

    def stage_dir(self, user: str, sha: str) -> Path:
        return self.root / ".staging" / user / sha

    def _state(self, user: str, sha: str) -> dict[str, Any]:
        final = self.final_dir(user, sha)
        if final.is_dir():
            meta = json.loads((final / "meta.json").read_text())
            return {"state": "complete", "offset": meta["size"]}
        part = self.stage_dir(user, sha) / "data.part"
        return {"state": "partial", "offset": part.stat().st_size if part.exists() else 0}

    def create(self, user: str, meta: dict[str, Any]) -> dict[str, Any]:
        sha = meta.get("sha256")
        size = meta.get("size")
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise UploadError(HTTPStatus.BAD_REQUEST, "sha256 must be 64 lowercase hex chars")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise UploadError(HTTPStatus.BAD_REQUEST, "size must be a positive integer")
        if size > self.max_bytes:
            raise UploadError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"size is over {self.max_bytes} bytes")
        filename = safe_filename(meta.get("filename"))
        if filename is None:
            raise UploadError(HTTPStatus.BAD_REQUEST, f"filename needs one of {sorted(VIDEO_EXTS)}")
        with self.lock(user, sha):
            state = self._state(user, sha)
            if state["state"] == "complete":
                return state
            stage = self.stage_dir(user, sha)
            if not stage.is_dir():
                self.root.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(self.root).free < size + FREE_SPACE_MARGIN:
                    raise UploadError(HTTPStatus.INSUFFICIENT_STORAGE, "not enough free space on the hub")
                stage.mkdir(parents=True, exist_ok=True)
            else:
                old = json.loads((stage / "meta.json").read_text())
                if old.get("size") != size:
                    raise UploadError(
                        HTTPStatus.CONFLICT, "size differs from the staged one", state["offset"]
                    )
            _write_json(stage / "meta.json", {**meta, "filename": filename})
            if state["offset"] == size:  # every byte arrived but the hub stopped before finalizing
                return self._finalize(user, sha)
            return state

    def append(
        self, user: str, sha: str, offset: int, length: int, read: Callable[[int], bytes]
    ) -> dict[str, Any]:
        """Append `length` bytes from `read` at `offset`; a bad request raises UploadError before reading."""
        with self.lock(user, sha):
            state = self._state(user, sha)
            if state["state"] == "complete":
                return state
            stage = self.stage_dir(user, sha)
            if not (stage / "meta.json").exists():
                raise UploadError(HTTPStatus.NOT_FOUND, "unknown upload: POST its metadata first")
            size = json.loads((stage / "meta.json").read_text())["size"]
            if offset != state["offset"]:
                raise UploadError(HTTPStatus.CONFLICT, "offset mismatch", state["offset"])
            if length <= 0 or offset + length > size:
                raise UploadError(HTTPStatus.BAD_REQUEST, "chunk runs past the declared size")
            part = stage / "data.part"
            with part.open("ab") as f:
                left = length
                while left:
                    block = read(min(BLOCK, left))
                    if not block:
                        break  # client went away; the bytes written so far stay valid
                    f.write(block)
                    left -= len(block)
                f.flush()
                os.fsync(f.fileno())
            got = part.stat().st_size
            if got == size:
                return self._finalize(user, sha)
            return {"state": "partial", "offset": got}

    def _finalize(self, user: str, sha: str) -> dict[str, Any]:
        stage = self.stage_dir(user, sha)
        part = stage / "data.part"
        meta = json.loads((stage / "meta.json").read_text())
        if sha256_file(part) != sha:
            part.unlink()
            raise UploadError(HTTPStatus.UNPROCESSABLE_ENTITY, "sha256 mismatch: the upload was discarded")
        filename = meta["filename"]
        os.replace(part, stage / filename)
        meta.update(user=user, file=filename, received_at=time.time())
        _write_json(stage / "meta.json", meta)
        _fsync_dir(stage)
        final = self.final_dir(user, sha)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(stage, final)
        _fsync_dir(final.parent)
        return {"state": "complete", "offset": meta["size"]}


class Handler(BaseHTTPRequestHandler):
    server: UploadServer
    server_version = "autoclipper-upload/1"
    _remaining = 0

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.server.quiet:
            super().log_message(format, *args)

    def _length(self) -> int | None:
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return n if n >= 0 else None

    def _read(self, k: int) -> bytes:
        """Read up to k body bytes, never past Content-Length (so `_drain` knows what is left)."""
        k = min(k, self._remaining)
        data = self.rfile.read(k) if k > 0 else b""
        self._remaining -= len(data)
        return data

    def _drain(self) -> None:
        # Read the unread body so the client sees the response instead of a reset; huge bodies just close.
        if self._length() is not None and self._remaining <= 2 * self.server.max_chunk:
            while self._remaining > 0 and self._read(BLOCK):
                pass
        else:
            self.close_connection = True

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, err: UploadError) -> None:
        self._drain()
        body: dict[str, Any] = {"error": str(err)}
        if err.offset is not None:
            body["offset"] = err.offset
        self._reply(err.status, body)

    def _user(self) -> str:
        # Browsers send Origin; recorders don't. Refusing it keeps web pages from driving the receiver.
        if self.headers.get("Origin") is not None:
            raise UploadError(HTTPStatus.FORBIDDEN, "browser requests are not accepted")
        user = authenticate(self.headers.get("Authorization"), self.server.tokens)
        if user is None:
            raise UploadError(HTTPStatus.UNAUTHORIZED, "bad or missing token")
        return user

    def parse_request(self) -> bool:
        ok = super().parse_request()
        self._remaining = (self._length() or 0) if ok else 0
        return ok

    def do_POST(self) -> None:
        try:
            user = self._user()
            if urlsplit(self.path).path.rstrip("/") != API:
                raise UploadError(HTTPStatus.NOT_FOUND, "not found")
            n = self._length()
            if n is None or n > MAX_META:
                raise UploadError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "metadata must be <= 1 MiB")
            try:
                meta = json.loads(self._read(n))
            except ValueError:
                return self._fail(UploadError(HTTPStatus.BAD_REQUEST, "metadata is not JSON"))
            if not isinstance(meta, dict):
                return self._fail(UploadError(HTTPStatus.BAD_REQUEST, "metadata must be an object"))
            try:
                state = self.server.inbox.create(user, meta)
            except UploadError as err:
                return self._fail(err)
            self._log(user, meta.get("sha256"), state)
            self._reply(HTTPStatus.OK, state)
        except UploadError as err:
            self._fail(err)

    def do_PUT(self) -> None:
        try:
            user = self._user()
            url = urlsplit(self.path)
            m = re.fullmatch(re.escape(API) + r"/([0-9a-f]{64})", url.path)
            if not m:
                raise UploadError(HTTPStatus.NOT_FOUND, "not found")
            try:
                offset = int(parse_qs(url.query).get("offset", [""])[0])
            except ValueError:
                raise UploadError(HTTPStatus.BAD_REQUEST, "offset query parameter required") from None
            n = self._length()
            if n is None:
                raise UploadError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            if n > self.server.max_chunk:
                raise UploadError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"chunk > {self.server.max_chunk} bytes"
                )
            state = self.server.inbox.append(user, m.group(1), offset, n, self._read)
            self._log(user, m.group(1), state)
            self._reply(HTTPStatus.OK, state)
        except UploadError as err:
            self._fail(err)

    def do_GET(self) -> None:
        self._fail(UploadError(HTTPStatus.NOT_FOUND, "not found"))

    def _log(self, user: str, sha: Any, state: dict[str, Any]) -> None:
        if state["state"] == "complete" and self.server.log:
            self.server.log(f"upload: {user} {str(sha)[:12]} complete ({state['offset']} bytes)")


class UploadServer(ThreadingHTTPServer):
    daemon_threads = True
    inbox: Inbox
    tokens: dict[str, str]
    max_chunk: int = MAX_CHUNK
    quiet: bool = False
    log: Callable[[str], None] | None = None


def inbox_root(settings: Settings) -> Path:
    return settings.upload_inbox or settings.data_dir / "inbox"


def make_server(
    settings: Settings,
    sock: socket.socket,
    *,
    quiet: bool = False,
    log: Callable[[str], None] | None = None,
) -> UploadServer:
    """Wrap an already-bound socket (tests bind port 0)."""
    tokens = parse_tokens(settings.upload_tokens)
    if not tokens:
        raise SystemExit("autoclipper: AUTOCLIPPER_UPLOAD_TOKENS is empty; the upload receiver needs tokens")
    server = UploadServer(sock.getsockname()[:2], Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = sock
    server.inbox = Inbox(inbox_root(settings), int(settings.upload_max_gb * 1024**3))
    server.tokens = tokens
    server.quiet = quiet
    server.log = log
    return server


def serve(settings: Settings, log: Callable[[str], None] = print) -> None:
    if not settings.upload_port:
        raise SystemExit(
            "autoclipper: the upload receiver is off; set AUTOCLIPPER_UPLOAD_PORT "
            "(`ports claim autoclipper-upload`)"
        )
    server = make_server(settings, bind_socket(settings.upload_host, settings.upload_port), log=log)
    log(f"upload receiver on {settings.upload_host}:{settings.upload_port}, inbox {inbox_root(settings)}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":  # python -m autoclipper.uploads (until the CLI has a subcommand)
    serve(Settings.from_env())
