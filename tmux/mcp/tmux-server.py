"""tmux MCP server.

A typed MCP surface over tmux for pane/window automation: discovery, splitting,
sending commands, capturing output, and blocking waits (idle, pattern match,
send-and-wait).

Key invariants:
- Exec via argument vectors only (subprocess.run([...]), never shell=True). No
  user value is ever interpolated into a shell string, so there is no command
  injection surface.
- Single-session scope: the server operates only within the session it was
  launched in (inherited via $TMUX / $TMUX_PANE). No cross-session tools.
- Permission model is one user-set lock: a pane with the `@claude_lock` pane
  option set to "1" refuses writes but still allows `capture`.
- Write operations refuse the server's own pane, so the server can never type
  into the Claude Code session hosting it, and never fall back to it.
- Close is ownership-scoped: only panes this server created can be closed.
- Every blocking tool is capped at MAX_WAIT_S; a caller cannot raise it.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import subprocess

from mcp.server import Server
from mcp.types import Tool, TextContent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("tmux_mcp_server")

# One command-log file per server process, so concurrent server instances
# (e.g. separate tmux sessions) never interleave into the same file.
COMMAND_LOG_PATH = f"/tmp/tmux-mcp-commands-{os.getpid()}.log"


def _log_command(target: str, command: str) -> None:
    """Append a timestamped record of every command sent to a pane."""
    timestamp = datetime.datetime.now().isoformat()
    with open(COMMAND_LOG_PATH, "a") as f:
        f.write(f"{timestamp}\t{target}\t{command}\n")

# Shells that mean "the pane has returned to an idle prompt".
_IDLE_SHELLS = {"bash", "zsh", "sh", "fish", "-bash", "-zsh", "-sh"}

# Sentinel used by tmux_run_and_wait to recover a real exit code from the pane.
# Each call gets a unique token (pid + sequence) so the poll cannot match a
# stale marker left in the scrollback by a previous run in the same pane.
_RUN_SEQ = 0
# Matches any of our markers, for stripping them out of returned output.
_RC_STRIP_RE = re.compile(r"__TMUX_MCP_RC_[^\s_]+_\d+__")

# Hard server-side ceiling on any blocking wait. Callers cannot raise it: a tool
# call that blocks longer than this is indistinguishable from a hang. Clamped
# values are echoed back so the caller can see the ceiling applied.
MAX_WAIT_S = 120
# Floor on poll intervals, so a small value cannot spin capture-pane
# subprocesses for the whole timeout.
MIN_POLL_INTERVAL_S = 0.1

# In-memory state. The server is long-lived, so it can remember the working pane
# it created across tool calls — no tmux session env var needed.
_STATE: dict[str, str | None] = {"working_pane": None, "sibling_pane": None}
# Panes this server created, and therefore the only panes it may close.
_CREATED_PANES: set[str] = set()


class TmuxError(Exception):
    """A tmux operation failed. `code` is the structured error code returned to
    the caller, which is what a caller branches on."""

    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def _tmux(*args: str, check: bool = True) -> str:
    """Run `tmux <args>` with an argument vector and return stripped stdout."""
    proc = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise TmuxError(
            "tmux_error", proc.stderr.strip() or f"tmux {' '.join(args)} failed"
        )
    return proc.stdout.rstrip("\n")


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _self_pane() -> str | None:
    """The pane this server (and its parent Claude Code) runs in."""
    return os.environ.get("TMUX_PANE")


def _pane_alive(pane_id: str) -> bool:
    try:
        live = _tmux("list-panes", "-a", "-F", "#{pane_id}").splitlines()
    except TmuxError:
        return False
    return pane_id in live


def _canonical_pane(target: str) -> str:
    """Resolve any target form (`%23`, `sess:1.0`, `.0`, a window name) to its
    `%N` pane id, which is the only form identity comparisons are valid against."""
    return _tmux("display-message", "-p", "-t", target, "#{pane_id}")


def _resolve_sibling(window: str | None = None) -> str | None:
    """The pane in this window that isn't the caller, resolved from the live pane
    list on every call so no caller has to hold the id.

    A pin is consulted but never required: it only records a choice the user made
    between three or more candidates, which cannot be re-derived. Two panes need
    no state — the sibling is whichever one isn't self.
    """
    others = [p for p in t_list_panes(window)["panes"] if not p["is_self"]]
    pinned = _STATE["sibling_pane"]
    if pinned and any(p["pane_id"] == pinned for p in others):
        return pinned
    if not others:
        return None
    if len(others) == 1:
        return others[0]["pane_id"]
    raise TmuxError(
        "ambiguous_sibling",
        "Several candidate panes; ask the user, then pin the choice with "
        "tmux_find_sibling(pane=...).",
        {"options": others, "pane_count": len(others) + 1},
    )


def _resolve_target(target: str | None) -> str:
    """Resolve a read target: explicit > working pane (if alive) > sibling > self.

    Sibling precedes self because reading the pane this session is running in
    returns its own transcript, which is never what a caller wanted. Raises
    `ambiguous_sibling` rather than guessing between three or more panes.
    """
    if target:
        return target
    working = _STATE["working_pane"]
    if working and _pane_alive(working):
        return working
    sibling = _resolve_sibling()
    if sibling:
        return sibling
    self_pane = _self_pane()
    if self_pane:
        return self_pane
    raise TmuxError("no_target", "No target pane and no current pane ($TMUX_PANE unset)")


def _guard_write(pane_id: str) -> str:
    """Refuse a write to the server's own pane or to a locked pane. Takes an
    already-canonical pane id."""
    if pane_id == _self_pane():
        raise TmuxError(
            "self_pane",
            f"Pane {pane_id} hosts this Claude Code session; refusing to write to it.",
        )
    if _is_locked(pane_id):
        raise TmuxError(
            "locked", f"Pane {pane_id} is locked (@claude_lock=1); refusing to write."
        )
    return pane_id


def _resolve_write_target(target: str | None) -> str:
    """Resolve a write target to a guarded, canonical pane id.

    There is deliberately no fallback to `$TMUX_PANE` the way the read path has:
    a write with no target and no live working pane would land in the calling
    Claude Code session's own input.
    """
    if target:
        return _guard_write(_canonical_pane(target))
    working = _STATE["working_pane"]
    if not (working and _pane_alive(working)):
        raise TmuxError(
            "no_write_target",
            "No target pane and no live working pane; refusing to default to the calling pane.",
        )
    return _guard_write(working)


def _clamp_timeout(timeout_s: int) -> tuple[int, bool]:
    return min(timeout_s, MAX_WAIT_S), timeout_s > MAX_WAIT_S


def _is_locked(target: str) -> bool:
    """True if the pane has @claude_lock = 1."""
    try:
        val = _tmux("show-options", "-pv", "-t", target, "@claude_lock", check=False)
    except TmuxError:
        return False
    return val.strip() == "1"


def _capture(target: str, lines: int) -> str:
    return _tmux("capture-pane", "-p", "-t", target, "-S", f"-{lines}")


def _trim_blank(text: str) -> str:
    return text.strip("\n")


# --------------------------------------------------------------------------- #
# Tool implementations                                                        #
# --------------------------------------------------------------------------- #


def t_log_file() -> dict:
    return {"log_file": COMMAND_LOG_PATH}


def t_status() -> dict:
    if not _in_tmux():
        return {"in_tmux": False}
    self_pane = _self_pane()
    out: dict = {"in_tmux": True, "current_pane_id": self_pane}
    if self_pane:
        out["current_window_id"] = _tmux(
            "display-message", "-p", "-t", self_pane, "#{window_id}"
        )
        out["session_name"] = _tmux(
            "display-message", "-p", "-t", self_pane, "#{session_name}"
        )
    out["history_limit"] = _tmux("show-options", "-gv", "history-limit", check=False)
    out["working_pane_id"] = _STATE["working_pane"]
    out["max_wait_s"] = MAX_WAIT_S
    return out


def t_list_panes(window: str | None) -> dict:
    fmt = "#{pane_id}\t#{pane_index}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_active}"
    self_pane = _self_pane()
    # Anchor to this server's own pane/window when no explicit target is given.
    # Without `-t`, tmux resolves "current window" via whichever client/window
    # is active at call time, not the window this Claude Code session lives in.
    target = window or self_pane
    args = ["list-panes", "-F", fmt]
    if target:
        args += ["-t", target]
    panes = []
    for line in _tmux(*args).splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        pid, idx, w, h, cmd, path, active = parts[:7]
        panes.append(
            {
                "pane_id": pid,
                "index": int(idx),
                "width": int(w),
                "height": int(h),
                "current_command": cmd,
                "current_path": path,
                "is_active": active == "1",
                "is_self": pid == self_pane,
            }
        )
    return {"panes": panes}


def t_list_windows(session: str | None) -> dict:
    fmt = "#{window_index}\t#{window_name}\t#{window_active}\t#{window_panes}"
    # Anchor to this server's own session when no explicit target is given.
    # Without `-t`, tmux resolves "current session" via whichever client/session
    # is active at call time, not the session this Claude Code instance lives in.
    target = session or _self_pane()
    args = ["list-windows", "-F", fmt]
    if target:
        args += ["-t", target]
    windows = []
    for line in _tmux(*args).splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        idx, name, active, count = parts[:4]
        windows.append(
            {
                "window_index": int(idx),
                "name": name,
                "active": active == "1",
                "pane_count": int(count),
            }
        )
    return {"windows": windows}


def t_find_sibling(window: str | None, pane: str | None = None) -> dict:
    """Resolve "the pane next to me", pinning the answer for later calls.

    Ambiguity is refused rather than guessed: with several candidates and no
    pin, the caller gets a described option list to choose from. Picking the
    first non-self pane would write to whichever pane tmux happened to list
    first.
    """
    panes = t_list_panes(window)["panes"]
    others = [p for p in panes if not p["is_self"]]
    base = {"pane_count": len(panes)}

    if pane:
        chosen = _canonical_pane(pane)
        if not any(p["pane_id"] == chosen for p in others):
            raise TmuxError(
                "pane_not_found", f"Pane {pane} is not a sibling pane in this window."
            )
        _STATE["sibling_pane"] = chosen
        return {**base, "sibling_pane_id": chosen, "pinned": True}

    try:
        sibling = _resolve_sibling(window)
    except TmuxError as e:
        if e.code != "ambiguous_sibling":
            raise
        return {
            **base,
            "sibling_pane_id": None,
            "needs_choice": True,
            "options": e.data["options"],
            "message": "Several candidate panes; ask the user, then call again with `pane`.",
        }
    return {
        **base,
        "sibling_pane_id": sibling,
        "pinned": sibling is not None and sibling == _STATE["sibling_pane"],
    }


def _send_literal(target: str, command: str, enter: bool) -> None:
    # `-l` sends the text literally so key names inside the command (e.g. "C-c"
    # as a substring) are not interpreted. `--` guards a leading dash. This is
    # why no quoting / temp-file dance is needed.
    _log_command(target, command)
    _tmux("send-keys", "-t", target, "-l", "--", command)
    if enter:
        _tmux("send-keys", "-t", target, "Enter")


def t_split_pane(
    direction: str,
    target: str | None,
    command: str | None,
    reuse: bool,
    cwd: str | None = None,
) -> dict:
    working = _STATE["working_pane"]
    if reuse and working and _pane_alive(working):
        if command:
            _send_literal(_guard_write(working), command, enter=True)
        return {"pane_id": working, "created": False}

    flag = "-h" if direction == "h" else "-v"
    # Anchor to this server's own pane when no explicit target is given.
    # Without `-t`, tmux splits off whichever pane the client last had focused,
    # which can be a different window/session than the one Claude Code runs in.
    # The split origin is deliberately unguarded: splitting off the calling pane
    # is the normal way a sibling gets created.
    split_target = target or _self_pane()
    # `-d` leaves focus where the user put it; background work must not steal it.
    args = ["split-window", flag, "-d", "-P", "-F", "#{pane_id}"]
    if cwd:
        # Omitted when unset, so the new pane inherits the split origin's cwd.
        args += ["-c", cwd]
    if split_target:
        args += ["-t", split_target]
    pane_id = _tmux(*args)
    _STATE["working_pane"] = pane_id
    _CREATED_PANES.add(pane_id)
    if command:
        _send_literal(_guard_write(pane_id), command, enter=True)
    return {"pane_id": pane_id, "created": True}


def t_ensure_session(name: str) -> dict:
    exists = subprocess.run(
        ["tmux", "has-session", "-t", f"={name}"], capture_output=True
    ).returncode == 0
    if not exists:
        _tmux("new-session", "-d", "-s", name)
    return {"session_name": name, "created": not exists}


def t_new_window(session: str, name: str, command: str | None) -> dict:
    idx = _tmux(
        "new-window", "-t", session, "-n", name, "-P", "-F", "#{window_index}"
    )
    target = f"{session}:{name}"
    if command:
        _send_literal(target, command, enter=True)
    return {"window_index": int(idx), "target": target}


def t_close_pane(target: str | None) -> dict:
    """Close a pane this server created. Window and session teardown are not
    exposed: nothing here creates a window it also owns the lifetime of."""
    resolved = _resolve_write_target(target)
    if resolved not in _CREATED_PANES:
        raise TmuxError(
            "not_owned",
            f"Pane {resolved} was not created by this server; refusing to close it.",
        )
    _tmux("kill-pane", "-t", resolved)
    _CREATED_PANES.discard(resolved)
    for key in ("working_pane", "sibling_pane"):
        if _STATE[key] == resolved:
            _STATE[key] = None
    return {"closed": True, "pane_id": resolved}


def t_send_command(target: str | None, command: str, enter: bool = True) -> dict:
    resolved = _resolve_write_target(target)
    _send_literal(resolved, command, enter=enter)
    return {"sent": True, "pane_id": resolved}


def t_send_keys(target: str | None, keys: str) -> dict:
    resolved = _resolve_write_target(target)
    # No `-l`: these ARE key names (C-c, Escape, ...) and must be interpreted.
    _tmux("send-keys", "-t", resolved, *keys.split())
    return {"sent": True, "pane_id": resolved}


def t_capture(target: str | None, lines: int) -> dict:
    resolved = _resolve_target(target)
    raw = _capture(resolved, lines)
    text = _trim_blank(raw)
    return {
        "text": text,
        "line_count": text.count("\n") + 1 if text else 0,
        "truncated": raw.count("\n") + 1 >= lines,
    }


async def t_wait_idle(target: str | None, timeout_s: int, poll_interval_s: float) -> dict:
    resolved = _resolve_target(target)
    timeout_s, clamped = _clamp_timeout(timeout_s)
    poll_interval_s = max(poll_interval_s, MIN_POLL_INTERVAL_S)
    waited = 0.0
    last_command = ""
    capped = {"timeout_s": timeout_s, "timeout_clamped": clamped}
    while waited < timeout_s:
        cmd = _tmux("display-message", "-p", "-t", resolved, "#{pane_current_command}")
        if cmd not in _IDLE_SHELLS:
            last_command = cmd
        else:
            return {"idle": True, "timed_out": False, "last_command": last_command, "exit_code": None, **capped}
        await asyncio.sleep(poll_interval_s)
        waited += poll_interval_s
    return {"idle": False, "timed_out": True, "last_command": last_command, "exit_code": None, **capped}


def _new_output_since(baseline: str, current: str) -> tuple[str, bool]:
    """Split off the output that arrived after `baseline` was captured.

    If the pane's scrollback window pushed `baseline` off the top (more new
    lines than the capture window holds), there's nothing to diff against —
    return the full current capture and flag the rotation.
    """
    if current.startswith(baseline):
        return current[len(baseline):], False
    return current, True


async def t_wait_for_output(
    target: str | None,
    pattern: str | None,
    byte_count: int | None,
    timeout_s: int,
    poll_interval_s: float,
    capture_lines: int,
) -> dict:
    if not pattern and not byte_count:
        raise TmuxError(
            "usage_error",
            "tmux_wait_for_output requires at least one of pattern or byte_count.",
        )
    resolved = _resolve_target(target)
    timeout_s, timeout_clamped = _clamp_timeout(timeout_s)
    poll_interval_s = max(poll_interval_s, MIN_POLL_INTERVAL_S)
    rx = re.compile(pattern) if pattern else None
    baseline = _capture(resolved, capture_lines)
    waited = 0.0

    while True:
        current = _capture(resolved, capture_lines)
        new_output, rotated = _new_output_since(baseline, current)
        new_bytes = len(new_output.encode("utf-8"))

        # Only the NEW output is scanned for a match — never the stale
        # scrollback that was already there when the wait started. This is
        # what previously let a pattern match a leftover line from an earlier
        # command instead of the current one's actual output.
        matched_line = None
        if rx:
            for line in new_output.splitlines():
                if rx.search(line):
                    matched_line = line
                    break

        byte_threshold_met = byte_count is not None and new_bytes >= byte_count
        matched = matched_line is not None
        if matched or byte_threshold_met:
            return {
                "matched": matched,
                "matched_line": matched_line,
                "byte_threshold_met": byte_threshold_met,
                "new_bytes": new_bytes,
                "new_output": _trim_blank(new_output),
                "scrollback": _trim_blank(current),
                "scrollback_rotated": rotated,
                "timed_out": False,
                "timeout_s": timeout_s,
                "timeout_clamped": timeout_clamped,
            }

        if waited >= timeout_s:
            return {
                "matched": False,
                "matched_line": None,
                "byte_threshold_met": False,
                "new_bytes": new_bytes,
                "new_output": _trim_blank(new_output),
                "scrollback": _trim_blank(current),
                "scrollback_rotated": rotated,
                "timed_out": True,
                "timeout_s": timeout_s,
                "timeout_clamped": timeout_clamped,
            }

        await asyncio.sleep(poll_interval_s)
        waited += poll_interval_s


async def t_run_and_wait(target: str | None, command: str, timeout_s: int, capture_lines: int) -> dict:
    global _RUN_SEQ
    resolved = _resolve_write_target(target)
    timeout_s, timeout_clamped = _clamp_timeout(timeout_s)
    # Unique per-call token so we only match THIS run's marker, never a stale one
    # still sitting in the pane scrollback from an earlier run_and_wait.
    _RUN_SEQ += 1
    token = f"{os.getpid()}-{_RUN_SEQ}"
    marker_re = re.compile(rf"__TMUX_MCP_RC_{re.escape(token)}_(\d+)__")
    instrumented = f'{command}; printf "\\n__TMUX_MCP_RC_{token}_%s__\\n" "$?"'
    _send_literal(resolved, instrumented, enter=True)
    waited = 0.0
    while waited < timeout_s:
        out = _capture(resolved, capture_lines)
        m = marker_re.search(out)
        if m:
            rc = int(m.group(1))
            # Strip all our markers (this run's and any stale ones) for clean output.
            cleaned = _RC_STRIP_RE.sub("", out).rstrip("\n")
            return {
                "exit_code": rc,
                "output": _trim_blank(cleaned),
                "timed_out": False,
                "timeout_s": timeout_s,
                "timeout_clamped": timeout_clamped,
            }
        await asyncio.sleep(0.5)
        waited += 0.5
    return {
        "exit_code": None,
        "output": _trim_blank(_capture(resolved, capture_lines)),
        "timed_out": True,
        "timeout_s": timeout_s,
        "timeout_clamped": timeout_clamped,
    }


# --------------------------------------------------------------------------- #
# MCP wiring                                                                  #
# --------------------------------------------------------------------------- #

app = Server("tmux_mcp_server")


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="tmux_log_file",
            description="Return the path of the command log file this server instance writes every sent command to (timestamp, target pane, command).",
            inputSchema=_obj({}),
        ),
        Tool(
            name="tmux_status",
            description="Report tmux context: in_tmux, current pane/window/session, history_limit. Use first; if in_tmux is false, skip other tmux tools.",
            inputSchema=_obj({}),
        ),
        Tool(
            name="tmux_list_panes",
            description="List panes in a window (default current) with id, index, size, current command/path, is_active, is_self.",
            inputSchema=_obj({"window": {"type": "string", "description": "Window target; default current window"}}),
        ),
        Tool(
            name="tmux_list_windows",
            description="List windows in a session (default current) with index, name, active, pane_count.",
            inputSchema=_obj({"session": {"type": "string", "description": "Session name; default current session"}}),
        ),
        Tool(
            name="tmux_find_sibling",
            description="Resolve and pin 'the pane next to me'. Null if there's only one pane. With several candidates and no pin, returns needs_choice=true plus a described option per pane — ask the user, then call again with `pane` to pin the answer.",
            inputSchema=_obj({
                "window": {"type": "string"},
                "pane": {"type": "string", "description": "Pin this pane as the sibling (use after a needs_choice response)"},
            }),
        ),
        Tool(
            name="tmux_split_pane",
            description="Open a working pane (default horizontal/right split, unfocused). Reuses the live working pane if one exists; recreates it if closed. Optionally run a command in it.",
            inputSchema=_obj({
                "direction": {"type": "string", "enum": ["h", "v"], "default": "h"},
                "target": {"type": "string", "description": "Pane to split from; default current"},
                "command": {"type": "string", "description": "Command to run in the new/reused pane"},
                "reuse": {"type": "boolean", "default": True},
                "cwd": {"type": "string", "description": "Working directory for the new pane; default inherits the split origin's"},
            }),
        ),
        Tool(
            name="tmux_ensure_session",
            description="Idempotently ensure a detached session exists. Returns created=false if it already existed.",
            inputSchema=_obj({"name": {"type": "string"}}, ["name"]),
        ),
        Tool(
            name="tmux_new_window",
            description="Create a named window in a session, optionally running a command. Returns window_index and target.",
            inputSchema=_obj({
                "session": {"type": "string"},
                "name": {"type": "string"},
                "command": {"type": "string"},
            }, ["session", "name"]),
        ),
        Tool(
            name="tmux_close_pane",
            description="Close a pane this server created. Refuses a pane it did not create (not_owned), the calling pane (self_pane), and a pane with @claude_lock=1. Window and session teardown are not exposed.",
            inputSchema=_obj({
                "target": {"type": "string", "description": "Pane target; default the working pane"},
            }),
        ),
        Tool(
            name="tmux_send_command",
            description="Send a shell command (sent literally + Enter) to a pane. Returns immediately. Refuses a locked pane and the calling pane.",
            inputSchema=_obj({
                "target": {"type": "string", "description": "Pane target; default working/current pane"},
                "command": {"type": "string"},
                "enter": {"type": "boolean", "default": True},
            }, ["command"]),
        ),
        Tool(
            name="tmux_send_keys",
            description="Send control/special keys (e.g. 'C-c', 'Escape', 'Enter') to interrupt or signal a process without killing the pane. Refuses a locked pane and the calling pane.",
            inputSchema=_obj({
                "target": {"type": "string"},
                "keys": {"type": "string"},
            }, ["keys"]),
        ),
        Tool(
            name="tmux_capture",
            description="Capture pane scrollback (default 200 lines). Always allowed, even on locked panes. Omit target to read the sibling pane; it is resolved on every call, so there is no id to look up or carry.",
            inputSchema=_obj({
                "target": {"type": "string", "description": "Pane to read. Omit for the sibling pane."},
                "lines": {"type": "integer", "default": 200},
            }),
        ),
        # These block for up to timeout_s. That is contained by MAX_WAIT_S rather
        # than by disabling them: a caller cannot raise the ceiling, and the
        # applied value comes back as timeout_s / timeout_clamped.
        Tool(
            name="tmux_wait_idle",
            description=f"Block until the pane returns to a shell prompt (process exited) or timeout. For long-lived processes use tmux_wait_for_output instead. timeout_s is capped at {MAX_WAIT_S}.",
            inputSchema=_obj({
                "target": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 120, "maximum": MAX_WAIT_S},
                "poll_interval_s": {"type": "number", "default": 0.5, "minimum": MIN_POLL_INTERVAL_S},
            }),
        ),
        Tool(
            name="tmux_wait_for_output",
            description=f"Poll a pane until its output matches a regex, at least byte_count new bytes arrive, or timeout_s elapses — whichever comes first. Provide `pattern`, `byte_count`, or both. Matching/counting only considers output that arrived after this call started, never stale scrollback, so a pattern cannot match the echoed command line. Returns the new output and full scrollback in the same call, so no separate tmux_capture is needed afterward. timeout_s is capped at {MAX_WAIT_S}.",
            inputSchema=_obj({
                "target": {"type": "string"},
                "pattern": {"type": "string", "description": "Regex to search for in the new output (e.g. a server-ready line)"},
                "byte_count": {"type": "integer", "description": "New bytes required since this call started"},
                "timeout_s": {"type": "integer", "default": 60, "maximum": MAX_WAIT_S},
                "poll_interval_s": {"type": "number", "default": 0.5, "minimum": MIN_POLL_INTERVAL_S},
                "capture_lines": {"type": "integer", "default": 200, "description": "Scrollback window checked each poll"},
            }),
        ),
        Tool(
            name="tmux_run_and_wait",
            description=f"Send a command, wait for it to finish, and return its real exit code + output. Composes send/wait/capture. Refuses a locked pane and the calling pane. timeout_s is capped at {MAX_WAIT_S}.",
            inputSchema=_obj({
                "target": {"type": "string"},
                "command": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 120, "maximum": MAX_WAIT_S},
                "capture_lines": {"type": "integer", "default": 200},
            }, ["command"]),
        ),
    ]


# Tools that need tmux and must hard-fail when not in a session.
_REQUIRES_TMUX = True


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    def ok(payload: dict) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps(payload))]

    try:
        if name == "tmux_log_file":
            return ok(t_log_file())

        if name == "tmux_status":
            return ok(t_status())

        if not _in_tmux():
            return ok({"error": "not_in_tmux", "message": "Not running inside a tmux session."})

        if name == "tmux_list_panes":
            return ok(t_list_panes(arguments.get("window")))
        if name == "tmux_list_windows":
            return ok(t_list_windows(arguments.get("session")))
        if name == "tmux_find_sibling":
            return ok(t_find_sibling(arguments.get("window"), arguments.get("pane")))
        if name == "tmux_split_pane":
            return ok(t_split_pane(
                arguments.get("direction", "h"),
                arguments.get("target"),
                arguments.get("command"),
                arguments.get("reuse", True),
                arguments.get("cwd"),
            ))
        if name == "tmux_ensure_session":
            return ok(t_ensure_session(arguments["name"]))
        if name == "tmux_new_window":
            return ok(t_new_window(arguments["session"], arguments["name"], arguments.get("command")))
        if name == "tmux_close_pane":
            return ok(t_close_pane(arguments.get("target")))
        if name == "tmux_send_command":
            return ok(t_send_command(arguments.get("target"), arguments["command"], arguments.get("enter", True)))
        if name == "tmux_send_keys":
            return ok(t_send_keys(arguments.get("target"), arguments["keys"]))
        if name == "tmux_capture":
            return ok(t_capture(arguments.get("target"), arguments.get("lines", 200)))
        if name == "tmux_wait_idle":
            return ok(await t_wait_idle(
                arguments.get("target"),
                arguments.get("timeout_s", 120),
                arguments.get("poll_interval_s", 0.5),
            ))
        if name == "tmux_wait_for_output":
            return ok(await t_wait_for_output(
                arguments.get("target"),
                arguments.get("pattern"),
                arguments.get("byte_count"),
                arguments.get("timeout_s", 60),
                arguments.get("poll_interval_s", 0.5),
                arguments.get("capture_lines", 200),
            ))
        if name == "tmux_run_and_wait":
            return ok(await t_run_and_wait(
                arguments.get("target"),
                arguments["command"],
                arguments.get("timeout_s", 120),
                arguments.get("capture_lines", 200),
            ))
        raise ValueError(f"Unknown tool: {name}")

    except TmuxError as e:
        return ok({"error": e.code, "message": e.message, **e.data})
    except KeyError as e:
        return ok({"error": "missing_argument", "message": f"Missing required argument: {e}"})
    except Exception as e:
        logger.exception("tmux tool %s failed", name)
        return ok({"error": "internal_error", "message": str(e)})


async def main():
    from mcp.server.stdio import stdio_server

    logger.info("Starting tmux MCP server (in_tmux=%s, self_pane=%s)", _in_tmux(), _self_pane())
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
