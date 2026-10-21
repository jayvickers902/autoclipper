"""One `claude -p` process per call, on Jay's subscription login (DESIGN.md, "Runtime judge").

No API key and no Anthropic SDK: the CLI's own claude.ai login is the only credential. The child gets an
empty working directory and only HOME and PATH, so no inherited CLAUDECODE / CLAUDE_CODE_* / API-key
variables leak in (ANTHROPIC_API_KEY included).

Under pytest, `resolve_bin` only runs scripts carrying FAKE_MARKER, so tests can never reach the real CLI.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

AUTH_RE = re.compile(
    r"not logged in|/login|authenticat|unauthori[sz]ed|invalid api key|oauth token|\b401\b", re.I
)
# Real rejection text (2.1.306): "You've hit your session limit · resets 3:40am (America/Halifax)".
LIMIT_RE = re.compile(
    r"usage limit|rate limit|hit your (?:\w+ )?limit|(?:session|weekly|5-hour) limit|limit reached", re.I
)
FAKE_MARKER = b"AUTOCLIPPER_FAKE_CLAUDE"


class LiveCallInTests(RuntimeError):
    """The real `claude` binary was about to run under pytest."""


@dataclass
class RateLimit:
    status: str = ""  # allowed / allowed_warning / rejected
    resets_at: float | None = None  # top-level resetsAt, epoch seconds
    five_hour: float | None = None
    five_hour_resets: float | None = None
    seven_day: float | None = None
    seven_day_resets: float | None = None


@dataclass
class CallResult:
    ok: bool = False
    status: str = "error"  # ok / error / timeout / invalid
    error: str = ""
    structured: dict | None = None
    is_error: bool = False
    result_text: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_s: float = 0.0
    api_duration_s: float | None = None
    rate_limit: RateLimit | None = None
    api_key_source: str | None = None
    exit_code: int | None = None
    stderr: str = ""
    stdout: str = ""
    binary_missing: bool = False
    api_error: str = ""  # assistant message `error` field, e.g. "rate_limit", "authentication_failed"

    @property
    def auth_failed(self) -> bool:
        if self.ok:
            return False
        if "auth" in self.api_error:
            return True
        return bool(AUTH_RE.search(self.result_text) or AUTH_RE.search(self.stderr[-2000:]))

    @property
    def limit_hit(self) -> bool:
        if self.ok:
            return False
        if self.api_error == "rate_limit":
            return True
        return bool(LIMIT_RE.search(self.result_text) or LIMIT_RE.search(self.stderr[-2000:]))


def minimal_env() -> dict[str, str]:
    return {
        "HOME": os.environ.get("HOME", os.path.expanduser("~")),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _is_fake(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return FAKE_MARKER in f.read(4096)
    except OSError:
        return False


def resolve_bin(name: str) -> str:
    """Resolve the configured binary. Under pytest, anything but a marked fake script is refused."""
    path = shutil.which(name, path=minimal_env()["PATH"]) or name
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real = shutil.which("claude", path=minimal_env()["PATH"])
        if _same_file(path, real) or not _is_fake(path):
            raise LiveCallInTests(f"refusing to run {name!r} under pytest (only marked fake scripts)")
    return path


def build_command(bin_path: str, model: str, system_prompt: str, schema: dict) -> list[str]:
    return [
        bin_path, "-p", "--model", model, "--safe-mode", "--tools", "", "--strict-mcp-config",
        "--no-session-persistence", "--system-prompt", system_prompt,
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip


def user_message(content: list[dict]) -> str:
    """The single stream-json user message (one line) sent on stdin."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def _f(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _text(message: object) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def parse_rate_limit(info: dict) -> RateLimit:
    w = info.get("unifiedWindows") or {}
    fh, sd = w.get("five_hour") or {}, w.get("seven_day") or {}
    return RateLimit(
        status=str(info.get("status", "")),
        resets_at=_f(info.get("resetsAt")),
        five_hour=_f(fh.get("utilization")),
        five_hour_resets=_f(fh.get("resetsAt")),
        seven_day=_f(sd.get("utilization")),
        seven_day_resets=_f(sd.get("resetsAt")),
    )


def parse_stream(stdout: str, res: CallResult) -> CallResult:
    """Fill `res` from stream-json lines; unknown or malformed lines are ignored."""
    result: dict | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        kind = d.get("type")
        if kind == "system" and d.get("subtype") == "init":
            res.api_key_source = d.get("apiKeySource")
        elif kind == "rate_limit_event" and isinstance(d.get("rate_limit_info"), dict):
            res.rate_limit = parse_rate_limit(d["rate_limit_info"])
        elif kind == "assistant" and isinstance(d.get("error"), str):
            res.api_error = d["error"]
            if not res.result_text:
                res.result_text = _text(d.get("message"))
        elif kind == "result":
            result = d
    if result is None:
        return res
    res.is_error = bool(result.get("is_error"))
    rt = result.get("result")
    if isinstance(rt, str) and rt:
        res.result_text = rt
    elif rt is not None and not isinstance(rt, str):
        res.result_text = json.dumps(rt)
    if not res.result_text and isinstance(result.get("errors"), list):
        res.result_text = "; ".join(map(str, result["errors"]))
    res.usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    res.cost_usd = _f(result.get("total_cost_usd"))
    ms = _f(result.get("duration_ms"))
    res.api_duration_s = ms / 1000 if ms is not None else None
    so = result.get("structured_output")
    res.structured = so if isinstance(so, dict) else None
    return res


def run_claude(
    bin_name: str, model: str, system_prompt: str, schema: dict, content: list[dict], timeout: float
) -> CallResult:
    """Run one call. Never raises for call failures; the result says what went wrong."""
    bin_path = resolve_bin(bin_name)
    cmd = build_command(bin_path, model, system_prompt, schema)
    res = CallResult()
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ac-judge-cwd-") as cwd:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=minimal_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True,
            )  # fmt: skip
        except OSError as e:
            res.error, res.binary_missing = f"cannot start {bin_name}: {e}", True
            return res
        try:
            out, err = proc.communicate(user_message(content), timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
            timed_out = True
    res.duration_s = round(time.monotonic() - t0, 3)
    res.stdout, res.stderr, res.exit_code = out or "", err or "", proc.returncode
    parse_stream(res.stdout, res)
    if timed_out:
        res.status, res.error = "timeout", f"timed out after {timeout:g}s (process group killed)"
    elif res.exit_code != 0:
        res.error = f"exit {res.exit_code}: {(res.result_text or res.stderr.strip())[-300:]}"
    elif res.is_error:
        res.error = f"error result: {res.result_text[-300:]}"
    elif res.structured is None:
        res.error = "no structured_output in the result" if res.result_text or res.usage else "no result line"
    else:
        res.ok, res.status = True, "ok"
    return res
