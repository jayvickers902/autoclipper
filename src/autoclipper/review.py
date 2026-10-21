"""The review page: a small local web app over the SQLite store (stdlib `http.server`, no dependencies).

    GET  /                replays: status, duration, dead flag, stage timings
    GET  /replay/<id>     that replay's candidates: window, signal score, judge verdict, model, cost, clips
    GET  /clips           every rendered clip, newest first, with an inline video
    GET  /tolabel         judged candidates nobody has labelled yet (for the judge comparison)
    GET  /search?q=...    full-text search (titles, summaries, map, transcript) over judged candidates
    POST /label           candidate_id + label (up|down) [+ next]; the only write, and only to `labels`
    GET  /media/<path>    files under output_dir only (Range supported), never replay_dir or anything else

Bound to 127.0.0.1 by `config.bind_socket` (fails if the port is taken). Requests whose Host or Origin is not
a loopback name are refused, so a web page or a DNS-rebinding host cannot reach it or post labels.
"""

from __future__ import annotations

import html
import re
import socket
import sys
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import Settings, bind_socket
from .search import highlight
from .store import Store

MEDIA_TYPES = {
    ".mp4": "video/mp4", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png",
}  # fmt: skip
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "autoclipper.localhost"})
CHUNK = 256 * 1024
MAX_QUERY = 200
SEARCH_LIMIT = 50
MAX_BODY = 4096
CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; media-src 'self'"  # noqa: E501

# media files


def resolve_media(output_dir: Path, rel: str) -> Path | None:
    """The real file for a URL-relative path, or None unless it lies under output_dir (symlinks resolved)."""
    if not rel or "\0" in rel or "\\" in rel or rel.startswith("/"):
        return None
    root = output_dir.resolve()
    try:
        target = (root / rel).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        return None
    if not target.is_file() or not target.is_relative_to(root) or target.suffix.lower() not in MEDIA_TYPES:
        return None
    return target


def media_url(output_dir: Path, path: str) -> str | None:
    """`/media/...` for a stored output path, or None if it is empty, missing, or outside output_dir."""
    if not path:
        return None
    root = output_dir.resolve()
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not real.is_file() or not real.is_relative_to(root):
        return None
    return "/media/" + quote(real.relative_to(root).as_posix())


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Inclusive (start, end) for a single `bytes=` range; None means serve everything.

    Raises ValueError when the range cannot be satisfied (-> 416). Multi-range requests are served whole.
    """
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if not m or (not m[1] and not m[2]):
        return None  # malformed or multi-range: ignore, per RFC 9110
    if not m[1]:  # suffix: the last n bytes
        n = int(m[2])
        if n == 0 or size == 0:
            raise ValueError("unsatisfiable")
        return max(size - n, 0), size - 1
    start = int(m[1])
    end = min(int(m[2]), size - 1) if m[2] else size - 1
    if start >= size or end < start:
        raise ValueError("unsatisfiable")
    return start, end


# html

CSS = """
:root{color-scheme:dark}
body{font:14px/1.45 system-ui,sans-serif;background:#14161a;color:#d8dce2;margin:0 auto;max-width:1100px;padding:0 16px 48px}
a{color:#7ab4ff;text-decoration:none}a:hover{text-decoration:underline}
nav{display:flex;gap:18px;padding:14px 0;border-bottom:1px solid #2a2e36;margin-bottom:18px}
nav b{margin-right:12px}h1{font-size:20px;margin:6px 0 14px}h2{font-size:16px;margin:22px 0 8px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #23262d;vertical-align:top}
th{color:#8c93a0;font-weight:500}.dim{color:#8c93a0}.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:0 7px;border-radius:9px;background:#2a2e36;font-size:12px}
.ok,.keep{background:#1d4a2c}.bad,.failed,.dead{background:#5a2323}.warn,.queued,.pending{background:#5a4a1d}
.card{background:#1b1e24;border:1px solid #2a2e36;border-radius:8px;padding:12px 14px;margin:0 0 14px}
.card h3{margin:0 0 4px;font-size:15px}.meta{color:#8c93a0;font-size:12.5px;margin-bottom:6px}
.media{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start;margin:8px 0}
video{max-width:560px;width:100%;background:#000;border-radius:6px}.media img{max-height:200px;border-radius:6px}
form.srch{margin-left:auto;display:flex;gap:6px}form.srch input{background:#1b1e24;color:inherit;border:1px solid #333843;border-radius:6px;padding:3px 8px}
.snip{color:#b4bac4;font-size:13px;margin:4px 0}mark{background:#5a4a1d;color:inherit;border-radius:3px}
form.lab{display:inline-flex;gap:6px;margin:6px 0 0}
form.lab button{font-size:18px;background:#23262d;border:1px solid #333843;border-radius:6px;padding:2px 12px;cursor:pointer;color:inherit}
form.lab button.on{background:#2d4a7a;border-color:#7ab4ff}
"""  # noqa: E501

JS = """
document.addEventListener('submit', async (e) => {
  const f = e.target;
  if (!f.classList.contains('lab')) return;
  e.preventDefault();
  const body = new URLSearchParams(new FormData(f, e.submitter));
  const r = await fetch('/label', {method: 'POST', body, headers: {'X-Requested-With': 'fetch'}});
  if (!r.ok) { alert('label failed: ' + r.status); return; }
  f.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.submitter));
});
"""


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def fmt_time(t: float | None) -> str:
    if t is None:
        return "-"
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def tag(text: Any, cls: str = "") -> str:
    return f'<span class="tag {esc(cls)}">{esc(text)}</span>'


def page(title: str, body: str, to_label: int = 0, query: str = "") -> bytes:
    nav = (
        '<nav><b>autoclipper</b><a href="/">Replays</a><a href="/clips">Clips</a>'
        f'<a href="/tolabel">To label ({to_label})</a>'
        '<form class="srch" action="/search" method="get"><input type="search" name="q" '
        f'value="{esc(query)}" placeholder="search clips and speech" maxlength="{MAX_QUERY}">'
        "<button>Search</button></form></nav>"
    )
    doc = (
        f'<!doctype html><html lang="en"><meta charset="utf-8"><title>{esc(title)} - autoclipper</title>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style>'
        f"{nav}<h1>{esc(title)}</h1>{body}<script>{JS}</script></html>"
    )
    return doc.encode()


def label_buttons(cid: int, current: str | None, nxt: str) -> str:
    def btn(value: str, icon: str) -> str:
        on = ' class="on"' if current == value else ""
        return f'<button name="label" value="{value}"{on} title="{value}">{icon}</button>'

    return (
        f'<form class="lab" method="post" action="/label">'
        f'<input type="hidden" name="candidate_id" value="{cid}">'
        f'<input type="hidden" name="next" value="{esc(nxt)}">{btn("up", "👍")}{btn("down", "👎")}</form>'
    )


def clip_media(output_dir: Path, clip: dict[str, Any] | None) -> str:
    if not clip or clip["status"] != "ok":
        return ""
    share, thumb, webp = (media_url(output_dir, clip[k]) for k in ("share_path", "thumb_path", "webp_path"))
    parts = []
    if share:
        poster = f' poster="{esc(thumb)}"' if thumb else ""
        parts.append(f'<video controls preload="metadata"{poster} src="{esc(share)}"></video>')
    elif thumb:
        parts.append(f'<img src="{esc(thumb)}" alt="thumbnail">')
    if webp:
        parts.append(f'<img src="{esc(webp)}" alt="preview" loading="lazy">')
    if not parts:
        return '<p class="dim">clip files are missing from the output directory</p>'
    size = f"{clip['share_bytes'] / 1e6:.1f} MB" if clip["share_bytes"] else ""
    meta = f"{esc(size)} {esc(clip['encoder'])}"
    return f'<div class="media">{"".join(parts)}</div><div class="meta">{meta}</div>'


def candidate_card(
    output_dir: Path, c: dict[str, Any], clip: dict[str, Any] | None, label: str | None, nxt: str,
    replay: dict[str, Any] | None = None, extra: str = "",
) -> str:  # fmt: skip
    v = c["verdict"] or {}
    status = c["judge_status"]
    verdict = ""
    if c["keep"] is not None:
        verdict = tag("keep" if c["keep"] else "discard", "keep" if c["keep"] else "")
    title = esc(v.get("title") or ("(no title)" if v else "(not judged)"))
    where = ""
    if replay:
        where = f'<a href="/replay/{replay["id"]}">{esc(Path(replay["path"]).name)}</a> &middot; '
    detail = [f"signal {c['score']:.2f}", f"judge score {esc(v['score'])}" if "score" in v else ""]
    detail += [esc(v["type"]) if v.get("type") else "", esc(c["model"]), f"${c['cost_usd']:.4f}"]
    detail = [d for d in detail if d]
    err = f'<div class="meta">error: {esc(c["error"])}</div>' if c["error"] else ""
    return (
        f'<div class="card" id="c{c["id"]}"><h3>{title}</h3>'
        f'<div class="meta">{where}#{c["idx"] + 1} {fmt_time(c["start"])}-{fmt_time(c["end"])} '
        f"&middot; {tag(status, status)} {verdict} &middot; {' &middot; '.join(detail)}</div>"
        f"<div>{esc(v.get('summary', ''))}</div>{extra}{err}{clip_media(output_dir, clip)}"
        f"{label_buttons(c['id'], label, nxt)}</div>"
    )


# data


def stage_summary(stages: dict[str, dict[str, Any]]) -> str:
    return " ".join(
        f"{name}&nbsp;{st['seconds']:.1f}s" + ("" if st["status"] == "ok" else f"({esc(st['status'])})")
        for name, st in stages.items()
    )


def replays_page(store: Store) -> tuple[str, str]:
    q = store.con.execute
    counts = {
        r["replay_id"]: r
        for r in q(
            "SELECT replay_id, COUNT(*) AS n, SUM(judge_status='judged') AS judged, SUM(keep=1) AS keeps "
            "FROM candidates GROUP BY replay_id"
        )
    }  # fmt: skip
    clips = {
        r["replay_id"]: r["n"]
        for r in q(
            "SELECT c.replay_id, COUNT(*) AS n FROM clips k JOIN candidates c ON c.id = k.candidate_id "
            "WHERE k.status = 'ok' GROUP BY c.replay_id"
        )
    }  # fmt: skip
    rows = []
    for r in q("SELECT * FROM replays ORDER BY id DESC"):
        n = counts.get(r["id"])
        cand = f"{n['n']} / {n['judged'] or 0} / {n['keeps'] or 0}" if n else "0 / 0 / 0"
        dead = tag("dead", "dead") + f" {esc(r['dead_reason'])}" if r["dead"] else ""
        rows.append(
            f'<tr><td><a href="/replay/{r["id"]}">{esc(Path(r["path"]).name)}</a></td>'
            f"<td>{tag(r['status'], r['status'])} {dead}</td><td class=num>{fmt_time(r['duration'])}</td>"
            f"<td class=num>{cand}</td>"
            f"<td class=num>{clips.get(r['id'], 0)}</td>"
            f'<td class="dim">{stage_summary(store.stages(r["id"]))}</td></tr>'
        )
    head = (
        "<tr><th>Replay</th><th>Status</th><th class=num>Length</th><th class=num>Cand / judged / kept</th>"
        "<th class=num>Clips</th><th>Stage timings</th></tr>"
    )
    if not rows:
        return "Replays", '<p class="dim">No replays yet.</p>'
    return "Replays", f"<table>{head}{''.join(rows)}</table>"


def replay_page(store: Store, output_dir: Path, rid: int) -> tuple[str, str] | None:
    if store.con.execute("SELECT 1 FROM replays WHERE id = ?", (rid,)).fetchone() is None:
        return None
    r = store.replay(rid)
    labels = store.latest_labels()
    nxt = f"/replay/{rid}"
    info = [tag(r["status"], r["status"]), f"length {fmt_time(r['duration'])}"]
    if r["dead"]:
        info.append(tag("dead", "dead") + f" {esc(r['dead_reason'])}")
    if r["duplicate_of"]:
        info.append(f'duplicate of <a href="/replay/{r["duplicate_of"]}">#{r["duplicate_of"]}</a>')
    body = [f'<div class="meta">{" &middot; ".join(info)}</div>']
    if r["error"]:
        body.append(f'<div class="meta">error: {esc(r["error"])}</div>')
    body.append(f'<div class="meta dim">{esc(r["path"])}<br>{stage_summary(store.stages(rid))}</div>')
    cands = store.candidates(rid)
    body.append(f"<h2>Candidates ({len(cands)})</h2>")
    body += [candidate_card(output_dir, c, store.clip_for(c["id"]), labels.get(c["id"]), nxt) for c in cands]
    if not cands:
        body.append('<p class="dim">No candidates.</p>')
    return Path(r["path"]).name, "".join(body)


def _load(store: Store, rows: list[Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(decoded candidate, replay) for rows with `id` and `replay_id`."""
    cache: dict[int, list[dict[str, Any]]] = {}
    out = []
    for row in rows:
        cands = cache.setdefault(row["replay_id"], store.candidates(row["replay_id"]))
        out.append((next(c for c in cands if c["id"] == row["id"]), store.replay(row["replay_id"])))
    return out


def clips_page(store: Store, output_dir: Path) -> tuple[str, str]:
    labels = store.latest_labels()
    rows = store.con.execute(
        "SELECT c.id, c.replay_id FROM clips k JOIN candidates c ON c.id = k.candidate_id "
        "WHERE k.status = 'ok' ORDER BY k.created_at DESC, k.id DESC"
    ).fetchall()  # fmt: skip
    cards = [
        candidate_card(output_dir, c, store.clip_for(c["id"]), labels.get(c["id"]), "/clips", rp)
        for c, rp in _load(store, rows)
    ]
    return "Clips", "".join(cards) or '<p class="dim">No clips yet.</p>'


def search_page(store: Store, output_dir: Path, query: str) -> tuple[str, str]:
    query = query.strip()[:MAX_QUERY]
    if not query:
        return "Search", '<p class="dim">Type words from a title, summary, map or what was said.</p>'
    hits = store.search(query, SEARCH_LIMIT)
    if not hits:
        return "Search", f'<p class="dim">No clips match "{esc(query)}".</p>'
    labels = store.latest_labels()
    nxt = "/search?q=" + quote(query)
    cands = _load(store, [{"id": h["candidate_id"], "replay_id": h["replay_id"]} for h in hits])
    cards = [
        candidate_card(
            output_dir, c, store.clip_for(c["id"]), labels.get(c["id"]), nxt, rp,
            extra=f'<div class="snip">{highlight(h["snippet"], esc)} &middot; '
            f'<span class="dim">score {h["score"]:.2f}</span></div>',
        )  # fmt: skip
        for h, (c, rp) in zip(hits, cands, strict=True)
    ]
    return "Search", f'<p class="dim">{len(hits)} result(s) for "{esc(query)}".</p>' + "".join(cards)


def to_label_count(store: Store) -> int:
    return store.con.execute(
        "SELECT COUNT(*) FROM candidates c WHERE c.judge_status = 'judged' "
        "AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.candidate_id = c.id)"
    ).fetchone()[0]


def tolabel_page(store: Store, output_dir: Path) -> tuple[str, str]:
    rows = store.con.execute(
        "SELECT c.id, c.replay_id FROM candidates c WHERE c.judge_status = 'judged' "
        "AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.candidate_id = c.id) "
        "ORDER BY c.replay_id DESC, c.idx LIMIT 200"
    ).fetchall()  # fmt: skip
    cards = [
        candidate_card(output_dir, c, store.clip_for(c["id"]), None, "/tolabel", rp)
        for c, rp in _load(store, rows)
    ]
    intro = '<p class="dim">Judged candidates without a thumbs up or down (kept and discarded alike).</p>'
    return "To label", intro + ("".join(cards) or '<p class="dim">Nothing left to label.</p>')


def safe_next(nxt: str | None) -> str:
    """Only same-site absolute paths; anything else goes home."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt and "\n" not in nxt:
        return nxt
    return "/"


# server


class Handler(BaseHTTPRequestHandler):
    server_version = "autoclipper-review"
    protocol_version = "HTTP/1.1"

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not getattr(self.server, "quiet", False):
            sys.stderr.write("review: " + format % args + "\n")

    def _host_ok(self) -> bool:
        m = re.fullmatch(r"(\[[^\]]+\]|[^:]+)(?::\d+)?", (self.headers.get("Host") or "").strip().lower())
        if not m or m[1].strip("[]") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        return not origin or (urlsplit(origin).hostname or "").lower() in ALLOWED_HOSTS

    def _send(
        self, status: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8", **headers: str
    ) -> None:  # noqa: E501
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, text: str = "") -> None:
        self._send(status, page(f"{status.value} {status.phrase}", f"<p>{esc(text)}</p>"))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host header")
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        if path.startswith("/media/"):
            return self._media(path[len("/media/") :])
        query = (parse_qs(parts.query).get("q") or [""])[0][:MAX_QUERY] if path == "/search" else ""
        with Store(self.settings) as store:
            out = self._route(store, path, query)
            if out is None:
                return self._error(HTTPStatus.NOT_FOUND)
            title, body = out
            self._send(HTTPStatus.OK, page(title, body, to_label_count(store), query))

    def _route(self, store: Store, path: str, query: str = "") -> tuple[str, str] | None:
        out_dir = self.settings.output_dir
        if path == "/search":
            return search_page(store, out_dir, query)
        if path == "/":
            return replays_page(store)
        if path == "/clips":
            return clips_page(store, out_dir)
        if path == "/tolabel":
            return tolabel_page(store, out_dir)
        m = re.fullmatch(r"/replay/(\d+)", path)
        return replay_page(store, out_dir, int(m[1])) if m else None

    def do_POST(self) -> None:
        self.close_connection = True  # error replies may leave the body unread
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "unexpected Host or Origin")
        if urlsplit(self.path).path != "/label":
            return self._error(HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._error(HTTPStatus.BAD_REQUEST, "bad body")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        label = (form.get("label") or [""])[0]
        raw_id = (form.get("candidate_id") or [""])[0]
        if label not in ("up", "down") or not raw_id.isdigit():
            return self._error(HTTPStatus.BAD_REQUEST, "need candidate_id and label up|down")
        cid = int(raw_id)
        with Store(self.settings) as store:
            if store.con.execute("SELECT 1 FROM candidates WHERE id = ?", (cid,)).fetchone() is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such candidate")
            if store.latest_labels().get(cid) != label:
                store.label_candidate(cid, label)
        if self.headers.get("X-Requested-With") == "fetch":
            return self._send(HTTPStatus.OK, b'{"ok":true}', "application/json")
        self._send(HTTPStatus.SEE_OTHER, b"", Location=safe_next((form.get("next") or [""])[0]))

    def _media(self, rel: str) -> None:
        target = resolve_media(self.settings.output_dir, rel)
        if target is None:
            return self._error(HTTPStatus.NOT_FOUND)
        size = target.stat().st_size
        ctype = MEDIA_TYPES[target.suffix.lower()]
        try:
            rng = parse_range(self.headers.get("Range"), size)
        except ValueError:
            status = HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
            return self._send(status, b"", ctype, Content_Range=f"bytes */{size}")
        start, end = rng if rng else (0, size - 1)
        length = end - start + 1 if size else 0
        self.send_response(HTTPStatus.PARTIAL_CONTENT if rng else HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        try:
            with target.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def make_server(settings: Settings, sock: socket.socket, *, quiet: bool = False) -> ThreadingHTTPServer:
    """An HTTP server on an already-bound, listening socket."""
    server = ThreadingHTTPServer(sock.getsockname()[:2], Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = sock
    server.daemon_threads = True
    server.settings = settings  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]
    return server


def serve(settings: Settings, log: Callable[[str], None] = print) -> None:
    sock = bind_socket(settings.host, settings.port)
    port = sock.getsockname()[1]
    server = make_server(settings, sock)
    log(f"autoclipper review on http://{settings.host}:{port}/ (http://autoclipper.localhost/ via Caddy)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
