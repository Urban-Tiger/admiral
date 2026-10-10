#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "pyte"]
# ///
#
# urbit ship manager.
#
#   manage your urbit ships across many computers (via ssh).
#
#   runs via uv (deps playwright + pyte, declared above); the host-side engine is
#   stdlib-only, so a ship's host needs only python3.
#
#   ./admiral.py init                 # to get started
#
#   ./admiral.py <ship> commit <desk> # to sync source code
#
#   ./admiral.py help                 # to learn more

from __future__ import annotations

import os
import re
import sys
import tty
import time
import fcntl
import struct
import signal
import socket
import threading
import termios
import selectors
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field, fields
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

Pattern = Any  # str | compiled regex

VERSION = "0.0.8"

# ── identity ──────────────────────────────────────────────────────────────────
# Name is hardcoded to "admiral". SELF is the script's real on-disk path (the rsync
# source + on-host tool copy need it). REPO is the project root — found by walking up
# from the caller's cwd to the first dir holding admiral.toml or .admiral.toml
# (git-style) — so `admiral` (via the enrich function) reads the config + desk sources
# of whatever project you're standing in, even from a subdirectory, not the script's
# install location. The dotted name is for a desk checkout that IS a ship's mount (its
# desk src is <pier>/<desk> of a ship listed in that config): vere never ingests
# dotfiles, so the config (and its +codes) stays out of clay.
SELF = Path(__file__).resolve()
NAME = "admiral"             # drives config + infra names (socket, tool copy, completion)
CLI = "admiral.py"           # basename with extension — shown in help/usage
CONFIG_NAMES = (f"{NAME}.toml", f".{NAME}.toml")
INVOKE = f"./{CLI}"          # how examples/usage render the invocation
SCRIPTS = f".{NAME}"         # test/script dir under REPO; excluded from desk syncs


def _find_repo() -> tuple[Path, str]:
    # climb from cwd to the first ancestor holding a config. Fall back to cwd and the
    # plain name when none is found, so a missing-config error (and init) names
    # ./admiral.toml in the current dir. Both names in one dir is ambiguous: refuse.
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        found = [n for n in CONFIG_NAMES if (d / n).is_file()]
        if len(found) > 1:
            sys.exit(f"{d} has both {found[0]} and {found[1]}; keep one")
        if found:
            return d, found[0]
    return cwd, CONFIG_NAMES[0]


REPO, CONFIG = _find_repo()

# config-value shapes, shared by load_config's validation and the init/add wizards.
SHIP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")  # ships.<name> map key
DESK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")          # desks[].name
CODE_RE = re.compile(r"^[a-z]{6}(-[a-z]{6}){3}$")        # +code override
TICKET_RE = re.compile(r"^~?[a-z]{6}(-[a-z]{6})+$")      # master ticket (~ optional, 2+ syllables)
MNEMONIC_RE = re.compile(r"^[a-z]+( [a-z]+)+$")          # BIP39 mnemonic (2+ lowercase words)

# per-command dojo timeouts (seconds); commit drives a full build, rest are quick.
T_COMMIT = 300  # a first build of a big desk takes minutes; a timeout aborts it
T_RUN = 15
T_DOJO = 60  # a dojo command's budget; past it the command is aborted, so builds need room
T_HARNESS = 15
T_CHOP = 900  # roll copies the snapshot into the new epoch — minutes on a big pier



# ── domain types ─────────────────────────────────────────────────────────────
@dataclass
class PierConfig:
    path: str | None = None   # pier dir; omit => .piers/<name> (defaulted in pier_dir)
    port: int | None = None   # http bind port on the box
    loom: int | None = None   # loom size (memory pages, power of 2) passed at boot
    ames: int | None = None   # ames UDP port; omit => vere picks one, and a different
                              # one each boot. pin it for ships that must reach each
                              # other: the lane a peer holds for you IS a port, so one
                              # that moves every restart is a lane that goes stale.
    local: bool = False       # boot with -L (fake networking). for a copied pier whose
                              # original still lives elsewhere: real ames from the copy
                              # would double-boot the ship. config-pinned so `run` can
                              # never forget it.


# every key `pier = { ... }` may carry. Config load rejects anything else, because
# add/edit rewrite that table from these fields alone — see the parse below.
PIER_KEYS = frozenset(f.name for f in fields(PierConfig))


@dataclass
class Ship:
    name: str                 # map key — the ship name (the selector matches this)
    pier: PierConfig          # pier dir + bind port + loom on its box
    host: str | None = None   # hosts map key; empty/None => the local machine
    http: str | None = None   # http origin playwright targets; REQUIRED for web access
    code: str | None = None   # optional +code override; skips the live dojo read
    master_ticket: str | None = None  # ship master ticket (key material; for boot)
    mnemonic: str | None = None       # BIP39 mnemonic seed phrase (key material)


@dataclass
class Desk:
    name: str
    src: str = "."            # repo subpath; defaulted to "."


@dataclass
class HostDef:
    ssh: str                  # ssh target (user@box); `ssh <ssh>` must work unattended
    root: str                 # absolute base dir for relative pier paths + desk syncs


@dataclass
class Config:
    ships: list[Ship]
    desks: list[Desk]
    hosts: dict[str, HostDef]


# ── config loading + validation ──────────────────────────────────────────────
def load_config() -> Config:
    file = REPO / CONFIG
    try:
        raw = file.read_text("utf8")
    except OSError:
        raise RuntimeError(
            f"{file} not found — create it with\n"
            f"  [ships.<name>]\n"
            f'  http = "http://localhost:<port>"\n'
            f"  pier = {{ port = <port> }}\n\n"
            f"  [[desks]]\n"
            f'  name = "<desk>"\n'
            f'  src = "."'
        )
    try:
        import tomllib
        cfg = tomllib.loads(raw)
    except Exception as e:
        raise RuntimeError(f"{CONFIG}: invalid TOML: {e}")

    def is_map(o: Any) -> bool:
        return isinstance(o, dict)

    ships_raw = cfg.get("ships")
    if not is_map(ships_raw) or not ships_raw:
        raise RuntimeError(f'{CONFIG}: "ships" must be a non-empty table of [ships.<name>] entries')
    # desks: OPTIONAL. A config with no desks is valid — it just can't commit.
    desks_raw = cfg.get("desks", [])
    if not isinstance(desks_raw, list):
        raise RuntimeError(f'{CONFIG}: "desks" must be an array of {{ name, src }} entries')

    win_re, desk_re, code_re = SHIP_NAME_RE, DESK_NAME_RE, CODE_RE

    # hosts: a table of REMOTE machines, keyed by name (the local machine is
    # implicit — a ship with no "host" runs locally, root = REPO). Validate first
    # so ships can reference them.
    host_names: set[str] = set()
    hosts: dict[str, HostDef] = {}
    hosts_raw = cfg.get("hosts")
    if hosts_raw is not None:
        if not is_map(hosts_raw):
            raise RuntimeError(f'{CONFIG}: "hosts" must be a table of [hosts.<name>] entries')
        for hname, h in hosts_raw.items():
            if hname == "local":
                raise RuntimeError(f'{CONFIG}: hosts["local"] is reserved — the local machine is implicit (omit "host", or set it to "local")')
            if not is_map(h):
                raise RuntimeError(f'{CONFIG}: hosts["{hname}"] must be a table {{ ssh, root }}')
            if not h.get("ssh") or not isinstance(h["ssh"], str):
                raise RuntimeError(f'{CONFIG}: hosts["{hname}"] missing "ssh" target')
            if not h.get("root"):
                raise RuntimeError(f'{CONFIG}: hosts["{hname}"] missing "root" (absolute base dir for piers, e.g. /home/urb)')
            if not os.path.isabs(h["root"]):
                raise RuntimeError(f'{CONFIG}: hosts["{hname}"].root "{h["root"]}" must be an absolute path')
            hosts[hname] = HostDef(ssh=h["ssh"], root=h["root"])
            host_names.add(hname)

    ships: list[Ship] = []
    for sname, ship in ships_raw.items():
        if not is_map(ship):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"] must be a table {{ pier, http, ... }}')
        if not win_re.match(sname):
            raise RuntimeError(f'{CONFIG}: ship name "{sname}" must match {win_re.pattern}')
        # host: top-level hosts key; empty/"local"/None => the local machine.
        host = ship.get("host") or None
        if host is not None and host != "local" and host not in host_names:
            raise RuntimeError(
                f'{CONFIG}: ships["{sname}"].host "{host}" is not in "hosts" '
                f'(have: {", ".join(["local", *host_names])})'
            )
        # pier: optional table of where the ship's pier lives + its boot params.
        pier = ship.get("pier") or {}
        if not is_map(pier):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].pier must be a table {{ {", ".join(PIER_KEYS)} }}')
        # Reject keys we don't know rather than ignoring them. `add`/`edit` rewrite
        # this table by re-rendering the fields below, so a key we merely ignored
        # would be silently DELETED the next time anyone edited the ship — the
        # config would lose settings without ever saying so. Refusing here means a
        # typo (or a setting from a newer admiral) is loud instead of load-bearing.
        unknown = sorted(set(pier) - PIER_KEYS)
        if unknown:
            raise RuntimeError(
                f'{CONFIG}: ships["{sname}"].pier has unknown key(s) {", ".join(unknown)} '
                f'(known: {", ".join(sorted(PIER_KEYS))}). admiral would drop these on the '
                f'next `add`/`edit`, so it refuses to run instead.')
        if pier.get("path") is not None and not isinstance(pier["path"], str):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].pier.path must be a string')
        port = pier.get("port")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].pier.port must be an integer 1-65535, got {port}')
        ames = pier.get("ames")
        if ames is not None and (not isinstance(ames, int) or isinstance(ames, bool) or ames < 1 or ames > 65535):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].pier.ames must be an integer 1-65535, got {ames}')
        loom = pier.get("loom")
        if loom is not None and (not isinstance(loom, int) or isinstance(loom, bool) or loom < 1):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].pier.loom must be a positive integer, got {loom}')
        local = pier.get("local", False)
        if not isinstance(local, bool):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].pier.local must be a boolean, got {local}')
        code = ship.get("code")
        if code is not None and (not isinstance(code, str) or not code_re.match(code)):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].code must be a +code key (e.g. "ralnyt-botwyc-batnyt-dozzod")')
        http = ship.get("http")
        if http is not None and (not isinstance(http, str) or not http.strip() or re.search(r"\s", http)):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].http must be a non-empty hostname or URL (e.g. "example.com")')
        ticket = ship.get("master-ticket")
        if ticket is not None and (not isinstance(ticket, str) or not TICKET_RE.match(ticket)):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].master-ticket must be a ticket like "~ralnyt-botwyc-batnyt-dozzod"')
        mnemonic = ship.get("mnemonic")
        if mnemonic is not None and (not isinstance(mnemonic, str) or not MNEMONIC_RE.match(mnemonic)):
            raise RuntimeError(f'{CONFIG}: ships["{sname}"].mnemonic must be a space-separated word list (BIP39 mnemonic)')

        ships.append(Ship(
            name=sname,
            host=host,
            pier=PierConfig(path=pier.get("path"), port=port, loom=loom, ames=ames, local=local),
            http=http or None,
            code=code or None,
            master_ticket=ticket or None,
            mnemonic=mnemonic or None,
        ))

    desks: list[Desk] = []
    for i, desk in enumerate(desks_raw):
        if not is_map(desk):
            raise RuntimeError(f"{CONFIG}: desks[{i}] must be a table {{ name, src }}")
        if not desk.get("name"):
            raise RuntimeError(f'{CONFIG}: desks[{i}] missing "name"')
        if not desk_re.match(desk["name"]):
            raise RuntimeError(f'{CONFIG}: desks[{i}].name "{desk["name"]}" must match {desk_re.pattern}')
        desks.append(Desk(name=desk["name"], src=desk.get("src", ".")))

    cfg = Config(ships=ships, desks=desks, hosts=hosts)
    check_desk_srcs(cfg)
    return cfg


# ── resolution helpers ────────────────────────────────────────────────────────
def pier_rel(ship: Ship) -> str:
    # the pier path as configured (or its default) — relative unless absolute/~.
    return ship.pier.path or f".piers/{ship.name}"


def pier_dir(ship: Ship, host: "Host") -> str:
    p = pier_rel(ship)
    if os.path.isabs(p) or p.startswith("~"):
        return p
    return os.path.join(host.root, p)


def ship_by_name(name: str, cfg: Config) -> Ship | None:
    return next((s for s in cfg.ships if s.name == name), None)


# ── engine: socket + on-host self-invocation ──────────────────────────────────
# This file is BOTH the frontend (reads CONFIG on the control machine) and the
# host-local pty engine (the daemon + clients). It invokes THIS SAME FILE on the
# ship's host with hidden __engine subcommands, routed through Host.exec. Only
# CONFIG is single-homed; the tool (this file) is pushed to each host.
_pushed: set[str] = set()  # hosts this process has already rsynced the tool to


SOCK_PATH_MAX = 103  # sun_path is 104 bytes on macOS, NUL included; Linux allows 107


def sock_path(ship: "Ship", host: "Host") -> str:
    # the daemon's control socket lives inside the pier dir on the ship's host —
    # a unix-domain socket, host-local: it never crosses the network (ssh is the
    # only transport).
    path = os.path.join(pier_dir(ship, host), f".{NAME}.sock")
    if len(os.fsencode(path)) > SOCK_PATH_MAX:
        raise RuntimeError(
            f"socket path is {len(os.fsencode(path))} bytes, over the {SOCK_PATH_MAX} a unix "
            f"socket allows:\n  {path}\nmove the pier (or this checkout) to a shorter path")
    return path


def remote_self(host: "Host") -> str:
    # path to THIS file on the host. Local => the file itself; remote => a pushed
    # copy under <root>/.<NAME>/<CLI>. CONFIG is never shipped — only this tool.
    if not host.ssh:
        return str(SELF)
    return os.path.join(host.root, f".{NAME}", CLI)


def ensure_self(host: "Host") -> str:
    # make sure this file exists on the host; rsync one file, once per host per
    # process. Cheap + idempotent (rsync skips when the bytes are unchanged).
    rs = remote_self(host)
    if not host.ssh or host.name in _pushed:
        return rs
    host_mkdirp(host, os.path.dirname(rs))
    local_exec("rsync", ["-az", "-e", "ssh", str(SELF), f"{host.ssh}:{rs}"])
    host.exec("chmod", ["+x", rs], check=False)
    _pushed.add(host.name)
    return rs


def vere_binary(ship: "Ship", host: "Host") -> str:
    # a booted pier keeps its own vere binary at <pier>/.run, so prefer that (the
    # host then needs no `urbit` on PATH); fall back to `urbit`.
    run = os.path.join(pier_dir(ship, host), ".run")
    return run if host_has_run(host, run) else "urbit"


def run_argv(ship: "Ship", host: "Host") -> list[str]:
    # the command the daemon holds a pty for: run the urbit pier with its configured
    # loom + http/ames ports.
    pier = pier_dir(ship, host)
    cmd = [vere_binary(ship, host)]
    if ship.pier.local:
        cmd += ["-L"]
    if ship.pier.loom is not None:
        cmd += ["--loom", str(ship.pier.loom)]
    if ship.pier.port is not None:
        cmd += ["--http-port", str(ship.pier.port)]
    if ship.pier.ames is not None:
        cmd += ["-p", str(ship.pier.ames)]
    cmd.append(pier)
    return cmd


def ship_base(ship: Ship) -> str:
    # the base origin playwright/login targets: the configured http origin (scheme
    # defaults to https). REQUIRED — there is no localhost:<port> fallback.
    if not ship.http:
        raise RuntimeError(
            f'ship "{ship.name}" has no "http" in {CONFIG} — '
            f'playwright needs an http origin to target it (no localhost:<port> fallback)'
        )
    d = ship.http.strip().rstrip("/")
    return d if re.match(r"^https?://", d, re.I) else f"https://{d}"


def web_reachable(ship: Ship) -> bool | None:
    # can we reach the ship's http UI? GET <http>/~/login and accept any non-5xx.
    # None => no "http" origin set so we didn't try (no localhost assumption);
    # False => http is set but the request failed/timed out/returned 5xx.
    try:
        base = ship_base(ship)
    except Exception:
        return None
    url = base.rstrip("/") + "/~/login"
    try:
        resp = urllib.request.urlopen(url, timeout=T_RUN)
        return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500  # 3xx/4xx still means the http server is answering
    except Exception:
        return False


def resolve_ship(name: str, cfg: Config) -> Ship:
    # every command names its ship. There is no implicit default: a command that
    # silently picked "the first ship" has built the wrong pier before.
    name = name.lstrip("~")  # tolerate a leading ~ (~sampel-palnet)
    ship = ship_by_name(name, cfg)
    if not ship:
        raise RuntimeError(
            f'no ship in {CONFIG} named "{name}" '
            f'(have: {", ".join(s.name for s in cfg.ships)})'
        )
    return ship


def select_ships(selector: str | None, cfg: Config) -> list[Ship]:
    # None only from a bare `admiral`, which shows the status of every ship.
    if selector is None:
        return cfg.ships
    return [resolve_ship(selector, cfg)]


def resolve_desk(name: str, cfg: Config) -> Desk:
    if not cfg.desks:
        raise RuntimeError(
            f'{CONFIG} has no desks — nothing to commit. '
            f'Add a [[desks]] entry to enable committing.'
        )
    if not name:
        return cfg.desks[0]
    desk = next((d for d in cfg.desks if d.name == name), None)
    if not desk:
        raise RuntimeError(
            f'no desk in {CONFIG} named "{name}" '
            f'(have: {", ".join(d.name for d in cfg.desks)})'
        )
    return desk


# ── execution: local + ssh, routed through one Host ──────────────────────────
def local_exec(file: str, args: list[str], *, input: str | None = None,
               check: bool = True, inherit: bool = False, timeout_ms: int | None = None) -> str:
    timeout = (timeout_ms / 1000) if timeout_ms else None
    if inherit:
        subprocess.run([file, *args], timeout=timeout, check=True)
        return ""
    try:
        # errors="replace": __capture streams the raw 256KB pty ring, a circular buffer
        # that can start mid-multibyte-UTF-8-char once it has wrapped (a chatty dojo full
        # of em-dashes does this fast) — strict decoding would crash on the stray 0x80.
        r = subprocess.run([file, *args], input=input, capture_output=True,
                           text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        if check is False:
            return ""
        raise
    if r.returncode != 0:
        if check is False:
            return ""
        raise RuntimeError(f"{file} exited {r.returncode}: {(r.stderr or r.stdout or '').strip()}")
    return r.stdout


def shq(s: str) -> str:
    # single-quote-escape one argv element so a remote shell reconstructs it
    # verbatim. embedded ' closes/re-opens with an escaped literal.
    return "'" + s.replace("'", "'\\''") + "'"


# ssh connection multiplexing: the FIRST exec to a host opens a master connection;
# every later exec within ControlPersist reuses it over the existing socket. This
# is what makes the ~9 probes/ship a status run fires cost one handshake. Every
# flag is bounded so one unreachable/auth-prompting host can't wedge a fan forever.
# The control socket name is DERIVED from this file's stem, never hardcoded.
SSH_MUX = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
    "-o", "ControlMaster=auto", "-o", f"ControlPath=~/.ssh/{NAME}-cm-%r@%h:%p", "-o", "ControlPersist=60",
]


@dataclass
class Host:
    name: str
    root: str                 # base dir for relative pier paths (REPO for local)
    ssh: str | None = None    # ssh target; None for local

    def exec(self, file: str, args: list[str], *, input: str | None = None,
             check: bool = True, inherit: bool = False, timeout_ms: int | None = None) -> str:
        # local short-circuits to a direct exec; remote wraps file+args into ONE
        # shell-quoted command and runs it through the local ssh client (per-arg
        # shq lets a `--sock <path>` / dojo cmd argument survive the one remote parse
        # intact). inherit -> `-tt` (pty, interactive attach); captured -> `-T`.
        if self.ssh:
            remote = " ".join(shq(x) for x in [file, *args])
            return local_exec("ssh", [("-tt" if inherit else "-T"), *SSH_MUX, self.ssh, remote],
                              input=input, check=check, inherit=inherit, timeout_ms=timeout_ms)
        return local_exec(file, args, input=input, check=check, inherit=inherit, timeout_ms=timeout_ms)


# the local machine itself — the local end of every rsync; reused as every local
# ship's host (host omitted => runs here).
LOCAL = Host("local", str(REPO))


def host_for(ship: Ship, cfg: Config) -> Host:
    # no host, or the reserved name "local", => the local machine.
    h = ship.host
    if not h or h == "local":
        return LOCAL
    d = cfg.hosts.get(h)
    if not d:
        raise RuntimeError(
            f'ship "{ship.name}" references unknown host "{h}" '
            f'(hosts: {", ".join(cfg.hosts) or "none"})'
        )
    return Host(h, d.root, d.ssh)


# ── engine helpers (host-routed: drive the daemon on the ship's host) ──────────
def eng(host: Host, ship: Ship, sub: str, *args: str, **kw) -> str:
    # invoke an engine subcommand of THIS file on the ship's host, against the
    # ship's socket. The single frontend->engine bridge.
    rs = ensure_self(host)
    return host.exec("python3", [rs, sub, "--sock", sock_path(ship, host), *args], **kw)


def local_capture_at(ship: Ship, host: Host, offset: int) -> tuple[int, str]:
    # capture_at without the subprocess. The engine protocol is a unix socket and a
    # LOCAL ship's socket is reachable from this very process, so spawning a python3
    # to re-parse this file and open it is pure overhead — 143ms measured, against a
    # driver that polls the ring every 80ms. Remote still pays it; only ssh can reach
    # a remote socket.
    sock = sock_path(ship, host)
    got = sock_capture_at(sock, offset)
    if got is not None:
        return (got[0], got[1].decode("utf-8", "replace"))
    # no daemon => ("", RING_NOMARK); a pre-C_CAPTURE_AT daemon => the whole ring under
    # RING_NOMARK, the same transparent degrade the remote path spells with its header.
    return (RING_NOMARK, sock_capture(sock).decode("utf-8", "replace"))


# The daemon ring is RAW pty bytes — full of the ANSI/cursor escapes urbit's dojo TUI
# emits. `pyte` is a pure-python terminal emulator: feed it the raw bytes, read back
# the RENDERED lines + scrollback (the cell grid a terminal maintains). It runs
# FRONTEND-side only (the host engine ships raw bytes), so it lives in the uv dep
# block and the engine stays stdlib-only. Falls back to a regex strip if absent.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ESC = re.compile(r"\x1b[@-Z\\-_]")
_CTL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

RENDER_COLS, RENDER_ROWS = 8192, 2
# The ONE geometry for every pyte render here — `capture`, a dojo output slice, the
# console log. Two knobs, and they fail in different ways; both were measured on ~lagrev
# against a 4000-char slog (`~&  (crip (reap 4.000 %x))  ~`).
#
# COLS WRAP. A logical line longer than `cols` soft-wraps into separate history entries,
# so the render splits a value urbit never split: at 200 the run came back in 200-char
# pieces, at 1024 in 1024-char pieces, at 8192 whole. Urbit streams long values with no
# explicit break (the pty here is 80 wide, yet its raw bytes hold the 4000-char run
# unbroken — vere emits the logical line and lets a terminal wrap it at display time).
# So pick a width no real line reaches. 8192x2 is 16K cells; the width is free.
#
# ROWS DESTROY. The dojo pins its editor to the BOTTOM pty row and scrolls finished
# output up into scrollback. A render screen TALLER than the pty leaves the cursor
# mid-screen, so its save/restore overwrites output in place and nothing reaches
# history. This does not truncate, it ANNIHILATES: at 8192x50 the 4000-char run and the
# slog around it were both simply absent. The daemon's pty is 24x80, so 2 and 24 both
# survive and 50 does not — 2 is not magic, it is just safely under any pty. A 2-row
# viewport clamps every absolute cursor move to the bottom and turns every line feed
# into a scroll, linearizing the whole stream into history regardless of the pty's real
# height (and collapsing backspace-overwrite spinners to nothing, which a regex strip
# cannot do — it deletes the erasure and keeps what was erased). This is
# `tmux capture-pane`. Rows is NOT a knob: taller is how you lose data.
PANE_HIST = 30000  # history >= lines a full RING_MAX burst can render to, so pyte
# doesn't drop sentinels the raw ring still holds



def _regex_clean(text: str) -> str:
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    text = text.replace("\r", "\n")
    return _CTL.sub("", text)


def clean_pane(raw: str, cols: int = RENDER_COLS, rows: int = RENDER_ROWS,
               hist: int = PANE_HIST) -> str:
    # render the raw pty stream to rendered, scrollback-aware text. A real terminal
    # emulator (cursor-aware) is the ONLY way to read a stream the dojo paints with
    # absolute cursor moves + backspace-overwrite spinners — a regex strip linearizes
    # those repaints into garbage. This is what `tmux capture-pane` does.
    try:
        import pyte
    except ImportError:
        return _regex_clean(raw)
    screen = pyte.HistoryScreen(cols, rows, history=hist, ratio=0.5)
    stream = pyte.Stream(screen)
    stream.feed(raw)

    def hrow(row) -> str:
        return "".join(row[x].data for x in range(screen.columns)).rstrip()

    lines = [hrow(r) for r in screen.history.top]
    lines += [r.rstrip() for r in screen.display]
    return "\n".join(lines)


PANE_TAIL_MIN = 16 * 1024  # floor on a bounded capture_pane window
LINE_BYTES = 512           # raw ring bytes to budget per rendered line a caller asked
# for. A dojo row is well under this once its prompt repaints and erase-to-EOL are
# counted, so a `-n 200` window that wanted 200 lines gets them with room to spare.
PROBE_TAIL = 64 * 1024     # `status` only reads the pane's last non-blank line


def capture_pane(host: Host, ship: Ship, tail_bytes: int | None = None) -> str:
    # dump the daemon's scrollback ring, rendered to clean text. `tail_bytes` bounds the
    # render to that many raw ring bytes.
    #
    # Bounding is not an optimization, it is the difference between usable and not: pyte
    # charges per CHARACTER and renders ~50KB/s (measured on a real ring), so reading the
    # last line of a 64MB ring costs ~20 minutes. Pass None only when the caller really
    # wants the whole console.
    if tail_bytes is not None:
        total = ring_mark(host, ship)
        if total >= 0:  # a pre-watermark daemon can only hand back the whole ring
            start = max(0, total - max(tail_bytes, PANE_TAIL_MIN))
            _, tail = capture_at(host, ship, start)
            if start > 0:
                # the cut lands mid-line far more often than not, and half a line
                # renders as a line — so the window's first row would be a fragment
                # of a row the console never had. Open on the next real break, the
                # same trim +attach_snapshot does for a replayed screen.
                cut = tail.find("\n")
                tail = tail[cut + 1:] if cut >= 0 else tail
            return clean_pane(tail)
    return clean_pane(eng(host, ship, "__capture", check=False))


# ── the console log ───────────────────────────────────────────────────────────
# `<sock>.pty` on the ship's host is the SOURCE OF TRUTH: raw, byte-exact, durable.
# The plaintext console log is DERIVED from it here, control-side, and is cached rather
# than authoritative — anything wrong with it costs a `--rebuild`, never data. That
# split is not a style choice: pyte cannot run on the host (stdlib-only engine), so the
# daemon can only ever append and the cook has to happen here regardless.
CONSOLE_DIR = Path.home() / ".cache" / "admiral"
CONSOLE_MIN_VERSION = "0.0.6"  # the release that started teeing `.pty`
CONSOLE_SEED_MAX = 1024 * 1024
# How much backlog a FIRST cook will chew. pyte runs ~65KB/s, so an unbounded first
# `slog` against a ship that has been up a while just hangs (a 3.3MB `.pty` is ~97s of
# silence, and a caller who times out reads the empty result as "the ship said nothing").
# Seed from the last MB instead — ~15s — and SAY what was skipped: a silent cap reads as
# full coverage, which is the same lie this whole feature exists to stop telling. The
# skipped bytes are not lost; they are in `.pty`, and `--rebuild` cooks all of them.


def console_paths(ship: Ship) -> tuple[Path, Path]:
    # (cooked log, watermark). Keyed by ship name: the cook is control-side, so a remote
    # ship's log lives HERE, not next to its pier.
    d = CONSOLE_DIR / ship.name
    return d / "console.log", d / "watermark"


def cook_pty(raw: str, stamps: list[tuple[int, float]], base: int) -> list[str]:
    # render raw pty bytes to timestamped plaintext lines.
    #
    # The renderer is doing real work here and a regex strip cannot replace it: the dojo
    # paints its spinner as `«label»` + exactly enough backspaces to erase it, so a strip
    # deletes the erasure as a control char and KEEPS the thing being erased. Replaying
    # the backspaces through a 2-row viewport erases it the way a real terminal does —
    # and that generalises to every overwrite trick vere has, including ones not found
    # yet. See RENDER_COLS/RENDER_ROWS for why the geometry is what it is.
    #
    # Timestamps come from feeding the stream in SEGMENTS bounded by `.idx` offsets and
    # draining whatever scrolled into history after each: a record (off, t) means the
    # read at `off` happened at t, so lines that appear while feeding [off_i, off_i+1)
    # are stamped t_i. The screen has to be fed incrementally anyway — it is stateful,
    # and cursor moves in a later chunk resolve against an earlier one. (Spreading lines
    # evenly across a chunk's span instead is a trap: over a whole-file cook it divides
    # HOURS by the line count and stamps every line with a fiction.)
    try:
        import pyte
    except ImportError:  # same degraded path as clean_pane: spinner litter, no crash
        return [f"{fmt_stamp(stamps[0][1] if stamps else time.time())}\t{l}"
                for l in _regex_clean(raw).split("\n") if l.strip()]
    screen = pyte.HistoryScreen(RENDER_COLS, RENDER_ROWS, history=PANE_HIST, ratio=0.5)
    stream = pyte.Stream(screen)
    # Seed the saved cursor at the bottom row before feeding a chunk that starts
    # mid-stream. The dojo pins its editor to the bottom and paints with save/restore, so
    # a chunk opening on a bare `\x1b[u` would restore against a fresh screen's default
    # (0,0) — piling later lines onto row 0 where they overwrite each other and vanish.
    # Seeding "cursor at bottom, saved" reconstructs the only state that actually
    # carries across a chunk boundary.
    stream.feed(f"\x1b[{RENDER_ROWS};1H\x1b[s")

    def drain() -> list[str]:
        # pop history rather than index into it: the deque is capped at `history`, so a
        # long cook would silently evict from the left and desync any counter we kept.
        rows = []
        while screen.history.top:
            row = screen.history.top.popleft()
            rows.append("".join(row[x].data for x in range(screen.columns)).rstrip())
        return rows

    out: list[str] = []

    def emit(rows: list[str], when: float) -> None:
        for r in rows:
            text = _PROMPT_RE.sub("", r).rstrip()
            if text:  # blank + prompt-only redraw rows carry nothing
                out.append(f"{fmt_stamp(when)}\t{text}")

    segs = stamps or [(base, time.time())]
    for i, (off, when) in enumerate(segs):
        end = segs[i + 1][0] if i + 1 < len(segs) else base + len(raw)
        chunk = raw[max(off - base, 0):max(end - base, 0)]
        if not chunk:
            continue
        stream.feed(chunk)
        emit(drain(), when)
    emit([r.rstrip() for r in screen.display], segs[-1][1])
    return out


def fmt_stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


_PROMPT_RE = re.compile(r"^~[a-z-]+:dojo> ?")


def console_sync(host: Host, ship: Ship, rebuild: bool = False,
                 note: Callable[[str], None] | None = None) -> Path:
    # pull `.pty` past our watermark, cook it, append. Returns the cooked log's path.
    log, mark = console_paths(ship)
    note = note or (lambda m: sys.stderr.write(m + "\n"))
    log.parent.mkdir(parents=True, exist_ok=True)
    # The log and its watermark are ONE piece of state: the watermark says how much of
    # `.pty` the log already contains. Cooking from 0 while keeping the old log would
    # append a second copy of everything. So any time the watermark is missing or
    # unreadable, the log is meaningless too — truncate and re-derive rather than
    # duplicate. Cheap to do, and it is the whole point of "derived, not authoritative".
    inode, offset, have_mark = 0, 0, False
    if mark.exists() and not rebuild:
        try:
            inode, offset = (int(x) for x in mark.read_text().split())
            have_mark = True
        except ValueError:
            pass
    if rebuild or not have_mark:
        log.unlink(missing_ok=True)

    out = eng(host, ship, "__pty-read", str(offset), check=False)
    head, _, body = out.partition("\n")
    parts = head.split()
    if len(parts) != 3:
        raise RuntimeError("no console log on this ship — its daemon predates the pty "
                           f"tee (v{CONSOLE_MIN_VERSION}). run: admiral {ship.name} engine-upgrade")
    cur_ino, size, nrecs = int(parts[0]), int(parts[1]), int(parts[2])
    if cur_ino == 0:
        raise RuntimeError("no console log on this ship — its daemon predates the pty "
                           f"tee (v{CONSOLE_MIN_VERSION}). run: admiral {ship.name} engine-upgrade")
    # a changed inode means `.pty` rotated under us: our offset indexes a file that is
    # now `.pty.1`, so it is meaningless. Restart from 0 rather than cook garbage — and
    # truncate, for the same reason as above: appending a fresh cook to the old log
    # duplicates it. (The rotated generation's lines are already in the log; what we lose
    # by not re-reading `.pty.1` is nothing.)
    if inode and cur_ino != inode:
        offset = 0
        log.unlink(missing_ok=True)
        out = eng(host, ship, "__pty-read", "0", check=False)
        head, _, body = out.partition("\n")
        parts = head.split()
        cur_ino, size, nrecs = int(parts[0]), int(parts[1]), int(parts[2])

    # a first cook with a big backlog: seed from the tail rather than hang (see
    # CONSOLE_SEED_MAX). Never on --rebuild — that is the command whose entire job is to
    # cook everything, however long it takes.
    if not rebuild and not have_mark and size > CONSOLE_SEED_MAX:
        seed = size - CONSOLE_SEED_MAX
        note(f"{ship.name}: console log seeded from the last "
             f"{CONSOLE_SEED_MAX // 1024}KB of {size // 1024}KB "
             f"({(size - CONSOLE_SEED_MAX) // 1024}KB of older output skipped — "
             f"`slog --rebuild` cooks all of it)")
        offset = seed
        out = eng(host, ship, "__pty-read", str(seed), check=False)
        head, _, body = out.partition("\n")
        parts = head.split()
        cur_ino, size, nrecs = int(parts[0]), int(parts[1]), int(parts[2])

    stamps: list[tuple[int, float]] = []
    for _ in range(nrecs):
        line, _, body = body.partition("\n")
        o, _, w = line.partition(" ")
        stamps.append((int(o), float(w)))
    if not body:
        return log
    # Only cook up to the last newline: a chunk can split an escape sequence, and half a
    # `\x1b[6144;1H` cooks to visible garbage AND desyncs the next chunk. Leave the
    # remainder for the next sync, which will see it whole.
    #
    # This is why an incremental cook's stamps are accurate to a CHUNK BOUNDARY, not to
    # IDX_GAP: held-back bytes cook in the next chunk and inherit its time, so a line can
    # read late (measured: 6/185 lines, median 0s, max 9.2s). The text is unaffected —
    # `--rebuild` reproduces every line — and a rebuild sees the whole file at once, so
    # its stamps are the accurate ones. That is the right way round: `.pty` is the truth.
    cut = body.rfind("\n")
    if cut < 0:
        return log
    keep, body = body[cut + 1:], body[:cut + 1]
    cooked = cook_pty(body, stamps, offset)
    if cooked:
        with log.open("a") as f:
            f.write("\n".join(cooked) + "\n")
    mark.write_text(f"{cur_ino} {size - len(keep.encode('utf-8', 'replace'))}")
    return log


# sentinel watermark meaning "no daemon, or a daemon too old to support C_CAPTURE_AT".
# Callers that get it back fall back to whole-ring capture (the pre-watermark behavior).
RING_NOMARK = -1


def capture_at(host: Host, ship: Ship, offset: int) -> tuple[int, str]:
    # raw (unrendered) capture of ring bytes past `offset` (a prior `total` watermark).
    # Returns (current_total, raw_tail). total is RING_NOMARK when the daemon predates
    # watermark support — then raw_tail is the FULL ring (identical to __capture), so the
    # caller transparently degrades to whole-ring search.
    if not host.ssh:
        return local_capture_at(ship, host, offset)
    out = eng(host, ship, "__capture-at", str(offset), check=False)
    if not out:
        return (RING_NOMARK, "")
    head, sep, tail = out.partition("\n")
    if not sep or head == "f" * 16:
        return (RING_NOMARK, tail if sep else out)
    try:
        return (int(head, 16), tail)
    except ValueError:
        return (RING_NOMARK, out)


def ring_mark(host: Host, ship: Ship) -> int:
    # current total-bytes-written watermark, or RING_NOMARK if unsupported. Asking past
    # the end (max u64) returns an empty tail + the live total — a cheap watermark probe.
    total, _ = capture_at(host, ship, (1 << 64) - 1)
    return total


def send_data(host: Host, ship: Ship, data: str) -> None:
    # inject raw bytes into the pty. Control bytes go literally: Ctrl-A = \x01,
    # Ctrl-K = \x0b, Enter = \r. Sent on the engine's stdin.
    #
    # A failed send RAISES. Swallowing it makes a dead socket look like a dojo that
    # took the keystrokes and refused the line — the driver then burns its whole
    # accept deadline and blames the line editor for a transport fault.
    if not host.ssh:
        if not sock_send(sock_path(ship, host), data.encode()):
            raise RuntimeError(f'ship "{ship.name}" daemon took no input '
                               f'(no daemon at {sock_path(ship, host)})')
        return
    eng(host, ship, "__send", input=data)


def clear_input(host: Host, ship: Ship) -> None:
    send_data(host, ship, "\x01\x0b")  # Ctrl-A Ctrl-K: home, then kill-to-end-of-line


def daemon_log_tail(host: Host, ship: Ship, n: int = 25) -> str:
    # last n lines of the daemon log (which holds the pty tail on shutdown). Used to
    # surface WHY a `run` failed — typically vere's own lock refusal.
    log = sock_path(ship, host) + ".log"
    try:
        if host.ssh:
            out = host.exec("tail", ["-n", str(n), log], check=False)
        else:
            out = "\n".join(open(log, errors="replace").read().splitlines()[-n:])
    except Exception:
        return ""
    return "\n".join(l for l in clean_pane(out).split("\n") if l.strip())


def daemon_status(host: Host, ship: Ship) -> tuple[int, int, str | None] | None:
    # (daemon_pid, child_pid, engine_version) if a daemon answers on the ship's
    # socket, else None. This is the AUTHORITATIVE liveness + the meaningful child PID:
    # the daemon owns the urbit child and reports it directly. engine_version is the
    # VERSION baked into the on-host tool copy that
    # STARTED this daemon — None when an older, pre-versioning daemon is running.
    if not host.ssh:
        return engine_ping(sock_path(ship, host))
    out = eng(host, ship, "__status", check=False).strip()
    if not out or not out.startswith("up"):
        return None
    parts = out.split()
    try:
        ver = parts[3] if len(parts) > 3 and parts[3] != "?" else None
        return (int(parts[1]), int(parts[2]), ver)
    except (IndexError, ValueError):
        return None


def pier_exists(host: Host, ship: Ship) -> bool:
    d = pier_dir(ship, host)
    if not host.ssh:
        return os.path.isdir(d)
    try:
        host.exec("test", ["-d", d])
        return True
    except Exception:
        return False


def host_has_run(host: Host, run: str) -> bool:
    # a booted pier keeps its own vere binary at <pier>/.run; prefer it so the host
    # needs no `urbit` on PATH. host-aware: stat the file on the SHIP's host (remote
    # too), not the local control machine — else a remote with no `urbit` on PATH
    # exits 127.
    if not host.ssh:
        return os.path.exists(run)
    try:
        host.exec("test", ["-x", run])
        return True
    except Exception:
        return False


def host_has(host: Host, tool: str) -> bool:
    # is a tool on the host's PATH? lets us tell "probe found nothing" apart from
    # "probe tool missing" — the latter must never read as "pier free".
    return len(host.exec("which", [tool], check=False).strip()) > 0


def host_mkdirp(host: Host, d: str) -> None:
    if not host.ssh:
        os.makedirs(d, exist_ok=True)
        return
    host.exec("mkdir", ["-p", d])


def rsync_dirs(src_host: Host, src_dir: str, dest_host: Host, dest_dir: str, flags: list[str],
               capture: bool = False) -> str:
    # rsync is a LOCAL tool that talks to the remote over ssh itself — so it never
    # routes through host.exec. The remote-ness lives in each path spec
    # (`target:dir`), plus `-e ssh` whenever either side is remote. rsync forbids
    # remote->remote, so callers keep at least one side local (doSync's src is the
    # local repo checkout).
    def q(h: Host, d: str) -> str:
        return f"{h.ssh}:{d}/" if h.ssh else f"{d}/"
    transport = [] if (not src_host.ssh and not dest_host.ssh) else ["-e", "ssh"]
    return local_exec("rsync", [*flags, *transport, q(src_host, src_dir), q(dest_host, dest_dir)],
                      inherit=not capture, check=not capture)


# ── pier liveness ─────────────────────────────────────────────────────────────
@dataclass
class HeldResult:
    held: bool
    holders: list[int]
    via: list[str]
    conclusive: bool


def _to_pids(out: str) -> list[int]:
    res = []
    for l in out.strip().split("\n"):
        l = l.strip()
        try:
            n = int(l)
        except ValueError:
            continue
        if n > 0:
            res.append(n)
    return res


def pier_held(host: Host, ship: Ship) -> HeldResult:
    # AUTHORITATIVE "a live process is using this pier" — the gate against a
    # double-boot (two runtimes on one pier corrupts it forever), so it FAILS SAFE:
    # any positive signal => held, and a held result is always trustworthy.
    # existsSync(.vere.lock) is NOT enough (the lock survives a crash and its
    # contents aren't the live PID). Two path-anchored probes instead:
    #   lsof  — who holds the .vere.lock fd (immune to stale files)
    #   pgrep — the worker whose argv carries THIS pier's absolute path
    # conclusive is false only when neither tool exists; held=True is always conclusive.
    d = pier_dir(ship, host)
    lock = os.path.join(d, ".vere.lock")
    holders: set[int] = set()
    via: list[str] = []
    probes = 0

    def add(out: str, tag: str) -> None:
        pids = _to_pids(out)
        if pids:
            holders.update(pids)
            via.append(tag)

    if not host.ssh:
        lock_present = os.path.isfile(lock)
    else:
        try:
            host.exec("test", ["-f", lock])
            lock_present = True
        except Exception:
            lock_present = False

    if host_has(host, "lsof"):
        probes += 1
        if lock_present:
            add(host.exec("lsof", ["-t", lock], check=False), "lsof")
    if host_has(host, "pgrep"):
        probes += 1
        add(host.exec("pgrep", ["-f", f"work --snap-dir {d}"], check=False), "pgrep")
    return HeldResult(len(holders) > 0, sorted(holders), via, probes > 0)


# ── port reconciliation: who actually owns this pier ──────────────────────────
# One pier (one host + pier dir) can be listed in MORE THAN ONE admiral.toml. That
# is fine ONLY when every config agrees on the http port — they then drive the same
# daemon harmlessly. A config whose pier.port DISAGREES with the port the ship is
# actually running on has the wrong idea of the ship: it would attach/login/commit
# against a port nothing is bound to. Such a config is "misconfigured" and must not
# act on the ship (not even attach) — it shows as misconfigured in status instead.
def running_http_port(host: Host, pid: int) -> tuple[bool, int | None]:
    # The --http-port the LIVE urbit child was booted with, recovered from its argv on
    # the host — the authoritative bind port, independent of WHICH config started it.
    # (read_ok, port): read_ok is False only when the argv can't be read at all (no ps,
    # or the pid is already gone), so a caller never misreads "couldn't read" as "no
    # port"; port is None when the child was booted with no --http-port (vere default).
    # -ww: never truncate the argv (the pier path trails --http-port, so an 80-col cut
    # could otherwise drop it). Works on both BSD/macOS and Linux ps.
    out = host.exec("ps", ["-ww", "-o", "args=", "-p", str(pid)], check=False).strip()
    if not out:
        return (False, None)
    m = re.search(r"--http-port[ =]+(\d+)", out)
    return (True, int(m.group(1)) if m else None)


def misconfig_msg(ship: Ship, actual: int | None) -> str:
    return (
        f'ship "{ship.name}" is misconfigured: it is running on --http-port '
        f'{shown_port(actual)} but {CONFIG} says port {shown_port(ship.pier.port)}. This pier is '
        f"managed by another config (same pier, different port) — refusing to act on it. "
        f"Align this {CONFIG}'s pier.port, or leave the ship to the config that owns it."
    )


def assert_port_match(host: Host, ship: Ship, st: tuple[int, int, str | None]) -> None:
    # raise misconfig if the running child's live --http-port disagrees with this config.
    # Fails safe: only raises on a difference we can positively prove (the live port read).
    ok, actual = running_http_port(host, st[1])
    if ok and actual != ship.pier.port:
        raise RuntimeError(misconfig_msg(ship, actual))


def guard_port(host: Host, ship: Ship) -> None:
    # Refuse to act on a RUNNING ship whose live --http-port disagrees with this config.
    # A ship that isn't up has nothing to disagree with, so this is a no-op then.
    st = daemon_status(host, ship)
    if st is not None:
        assert_port_match(host, ship, st)


def guard_not_running(ship: Ship, cfg: Config, action: str) -> None:
    # Refuse to edit a ship's config while that ship is running. The config is what
    # the running daemon was started from (pier path, port, host) — mutating or
    # removing it mid-flight desyncs the live pier from the config that owns it.
    # pier_held is the authoritative lockfile-based liveness gate and FAILS SAFE:
    # any positive signal => held, and a held result is always trustworthy, so we
    # only ever block on a running ship we can positively prove is up.
    host = host_for(ship, cfg)
    held = pier_held(host, ship)
    if not held.held:
        return
    who = ", ".join(map(str, held.holders)) or "?"
    raise RuntimeError(
        f'cannot {action} ship "{ship.name}" while it is running '
        f'(pier held by PID {who} via {", ".join(held.via)}) — '
        f'stop it first with `{INVOKE} {ship.name} exit`'
    )


# ── text helpers ──────────────────────────────────────────────────────────────
def trim_blank(lines: list[str]) -> list[str]:
    s, e = 0, len(lines)
    while s < e and not lines[s].strip():
        s += 1
    while e > s and not lines[e - 1].strip():
        e -= 1
    return lines[s:e]


def tail_n(text: str, n: int) -> str:
    return "\n".join(text.split("\n")[-n:])


# ── presentation: one color primitive + one table renderer ───────────────────
_ANSI = {"red": 31, "green": 32, "yellow": 33, "white": 37, "gray": 90, "bold": 1}


def is_tty(stream: Any) -> bool:
    # color iff the stream is a terminal and NO_COLOR is unset (the env opt-out).
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def paint(s: str, name: str, on: bool = True) -> str:
    return f"\x1b[{_ANSI[name]}m{s}\x1b[0m" if on else s


# shared status cells (status / info): tri-state mark, colored signal, verdict color.
def yn(b: Any) -> str:
    return "-" if b is None else ("✓" if b else "✗")


def sig(b: Any, on: bool) -> str:
    return yn(b) if (not on or b is None) else paint(yn(b), "green" if b else "red")


def verdict_color(s: str, on: bool) -> str:
    return paint(s, {"up": "green", "partial": "yellow"}.get(s, "red"), on)


def shown_port(p: int | None) -> str:
    return str(p) if p is not None else "(unset)"


def render_table(cols: list[str], rows: list[list[str]],
                 cell: Callable[[str], str] | None = None, stream: Any = None) -> None:
    # the single table renderer (list / status / version): header + width-aligned
    # rows. `cell` optionally colors a body cell; padding is computed on the RAW
    # text width so injected ANSI escapes never skew the columns.
    stream = stream or sys.stdout
    w = [max(len(cols[i]), *(len(r[i]) for r in rows)) for i in range(len(cols))] if rows \
        else [len(c) for c in cols]

    def fmt(cells: list[str], head: bool = False) -> str:
        return "  ".join(
            ((c if head or cell is None else cell(c)) + " " * max(0, w[i] - len(c)))
            for i, c in enumerate(cells)
        ).rstrip()

    stream.write(fmt(cols, True) + "\n")
    for r in rows:
        stream.write(fmt(r) + "\n")


# ── desk sync ─────────────────────────────────────────────────────────────────
def desk_src_dir(desk: Desk) -> str:
    # ALWAYS local — the desk source is this repo checkout, regardless of which box
    # the ship runs on; only the rsync destination is host-routed.
    src = desk.src or "."
    return os.path.join(str(REPO), ".") if src == "." else os.path.join(str(REPO), src)


def _local_mounts(desk: Desk, cfg: Config) -> list[tuple[Ship, str, str]]:
    # (ship, pier, <pier>/<desk>) realpaths for every local ship. Remote piers are
    # skipped: the desk source is always a local path, so it can never be one of them.
    out = []
    for ship in cfg.ships:
        host = host_for(ship, cfg)
        if host.ssh:
            continue
        pier = os.path.realpath(os.path.expanduser(pier_dir(ship, host)))
        out.append((ship, pier, os.path.join(pier, desk.name)))
    return out


def mount_owner(desk: Desk, cfg: Config) -> Ship | None:
    # the ship whose mounted %desk dir IS the desk source — a checkout living in a pier
    # listed in this config. That ship's |commit reads it directly (an rsync would copy
    # it onto itself); every other ship rsyncs from it as usual.
    src = os.path.realpath(desk_src_dir(desk))
    owners = [ship for ship, _, mount in _local_mounts(desk, cfg) if mount == src]
    if len(owners) > 1:
        raise RuntimeError(f'desk "{desk.name}": source {desk.src} is the %{desk.name} mount of '
                           f'{", ".join(s.name for s in owners)}; ships can\'t share a pier')
    return owners[0] if owners else None


def check_desk_srcs(cfg: Config) -> None:
    # a source inside a listed pier must be exactly that pier's %<desk> mount — anything
    # else (the pier root, another desk's mount, a subdir) would rsync one pier's files
    # over another ship's desk.
    for desk in cfg.desks:
        src = os.path.realpath(desk_src_dir(desk))
        for ship, pier, mount in _local_mounts(desk, cfg):
            if src != mount and os.path.commonpath([src, pier]) == pier:
                raise RuntimeError(
                    f'{CONFIG}: desk "{desk.name}" src {desk.src} is inside {ship.name}\'s pier '
                    f'but isn\'t its %{desk.name} mount ({mount})')
        mount_owner(desk, cfg)


def sync_flags() -> list[str]:
    # honor .gitignore so a sync publishes exactly the tracked desk source — never
    # build output, deps, or editor cruft. Also drop every dotfile and dot-dir at any
    # depth (vere never ingests them, so copying one only costs the transfer — .git,
    # the script dir, a sibling desk living beside this one), and this control file
    # and its config: infra, not desk source. --delete prunes stale dest files.
    return ["-avL", "--delete", "--filter=:- .gitignore", "--exclude=.*",
            *(f"--exclude={n}" for n in CONFIG_NAMES), f"--exclude={CLI}"]


def self_mounted(ship: Ship, desk: Desk, cfg: Config) -> bool:
    owner = mount_owner(desk, cfg)
    return bool(owner and owner.name == ship.name)


def do_sync(ship: Ship, desk: Desk, cfg: Config) -> None:
    host = host_for(ship, cfg)
    dest_dir = os.path.join(pier_dir(ship, host), desk.name)
    src = desk.src or "."
    src_dir = desk_src_dir(desk)
    if src != "." and not os.path.exists(src_dir):
        raise RuntimeError(f'desk "{desk.name}": source directory not found: {src_dir}')
    if self_mounted(ship, desk, cfg):
        sys.stderr.write(f"{src} is {ship.name}'s %{desk.name} mount; no sync\n")
        return
    host_mkdirp(host, dest_dir)
    sys.stderr.write(f"Syncing {src} -> {dest_dir if not host.ssh else host.ssh + ':' + dest_dir}...\n")
    rsync_dirs(LOCAL, src_dir, host, dest_dir, sync_flags())


def pending_deletes(ship: Ship, desk: Desk, cfg: Config) -> list[str]:
    # the files a sync would delete from the ship's mount: a dry run of do_sync's rsync.
    host = host_for(ship, cfg)
    if self_mounted(ship, desk, cfg) or not os.path.exists(desk_src_dir(desk)):
        return []
    dest_dir = os.path.join(pier_dir(ship, host), desk.name)
    out = rsync_dirs(LOCAL, desk_src_dir(desk), host, dest_dir, ["-n", "-i", *sync_flags()],
                     capture=True)
    return [l.split(None, 1)[1] for l in out.splitlines() if l.startswith("*deleting")]


# ── dojo driver: drive one dojo pane (host-routed) ────────────────────────────
DOJO_PROMPT_RE = re.compile(r":dojo[^>]*>\s*$")  # a live dojo prompt at end of line
DOJO_INPUT_MAX = 1000  # the pty drops a line's keystrokes past byte 1023 (measured)
DOJO_OUTPUT_MAX = 400  # transcript lines `dojo` prints; slog keeps every one
DOJO_RENDER_MAX = 512 * 1024  # transcript bytes pyte renders; the rest is regex-cleaned

# The glyph after `:dojo` on the input row says what the dojo is doing:
#
#   ~zod:dojo>               idle — or running a thread, painted identically
#   ~zod:dojo<               an open entry: a heredoc, an unclosed rune
#   ~zod:dojo: nuke? (y/N)   a generator asking a question
#
# and a pane that repaints nothing at all is arvo mid-event, keystrokes queued behind it.
PROMPT_ROW = re.compile(r"^~[a-z][a-z0-9-]*:dojo[^ <>:]*(>|<|: )(.*)$")
ASK_ROW = re.compile(r"^~[a-z][a-z0-9-]*:dojo[^ <>:]*: ")
YES_NO = re.compile(r"\((?:y/N|Y/n|y/n)\)")
BACKSPACE = "\x7f"  # backs out of an open entry, cancels a question, aborts a thread
INTERRUPT = "\x03"  # interrupts an arvo event mid-computation; a thread ignores it
SPINNER = re.compile(r"[|/\\-]\x08")  # vere paints this while an event computes


def check_dojo_input(command: str) -> None:
    # refuse what the dojo cannot take as one typed line, before a keystroke is sent.
    if "\n" in command or "\r" in command:
        raise RuntimeError(
            "dojo input has a newline. The dojo reads one line: the first part runs and\n"
            "the rest types into whatever that left open. Put multi-line source in a desk\n"
            "file and read it from there.")
    if len(command) > DOJO_INPUT_MAX:
        raise RuntimeError(
            f"dojo input is {len(command)} chars; admiral types at most {DOJO_INPUT_MAX} "
            f"(the terminal drops\nkeystrokes past 1023). Split it across dojo variables, "
            f"one call each\n(=a \"...\"  =b \"...\"  then (weld a b)), or put it in a desk file.")


def prompt_state(raw: str) -> tuple[str, str] | None:
    # the last prompt painted in `raw`: ("idle" | "open" | "ask", the text after it).
    for line in reversed(_regex_clean(raw).split("\n")):
        m = PROMPT_ROW.match(line.strip())
        if m:
            return ({">": "idle", "<": "open", ": ": "ask"}[m.group(1)], m.group(2))
    return None


def dojo_prompt(host: Host, ship: Ship) -> tuple[str, str] | None:
    # empty the input row and read the prompt the dojo shows. Ctrl-A Ctrl-K repaints a
    # row that held text; on a row already empty it only bells, and the prompt is the
    # last one painted before. No reaction within a second means arvo is mid-event and
    # the keys are queued behind it: None.
    mark = ring_mark(host, ship)
    clear_input(host, ship)
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        time.sleep(0.08)
        _, raw = capture_at(host, ship, mark)
        state = prompt_state(raw)
        if state:
            return state
        if BELL in raw:
            _, tail = capture_at(host, ship, max(0, mark - 8192))
            return prompt_state(tail) or ("idle", "")
    return None


def dojo_probe(host: Host, ship: Ship, total: int) -> tuple[str, int, str]:
    # type PROBE and read the reaction past `total` as a pair:
    #
    #   idle   a bell and no busy notice
    #   busy   "dojo: busy" — a thread is running
    #   mute   neither — arvo is computing and the key is queued
    #
    # The bell alone is not enough: a dojo mid-thread bells at the refused byte too and
    # prints its busy notice out of the same event, measured as
    #   `~zod:dojo> )` … \x07 … `dojo: busy (press backspace to abort)`
    # inside one 400ms window. Returns (verdict, total after, reaction bytes).
    send_data(host, ship, PROBE)
    start = time.monotonic()
    ptotal, reaction = total, ""
    while time.monotonic() < start + 1.0:
        time.sleep(0.06)
        ptotal, praw = capture_at(host, ship, total)
        if praw:
            reaction = praw
            break
    if reaction:
        # ONE settle read, not a poll: the bell and the notice come out of the same arvo
        # event a few ms apart, so the only risk is a capture landing mid-burst.
        time.sleep(0.25)
        ptotal, reaction = capture_at(host, ship, total)
    if "dojo: busy" in reaction:
        return ("busy", ptotal, reaction)
    if BELL in reaction:
        return ("idle", ptotal, reaction)
    return ("mute", ptotal, reaction)


def dojo_abort(host: Host, ship: Ship, verdict: str) -> None:
    # stop what the dojo is doing: backspace aborts a thread, Ctrl-C interrupts an event
    # still computing. An interrupted command comes back as text on the input row, so
    # empty the row after.
    send_data(host, ship, INTERRUPT if verdict == "mute" else BACKSPACE)
    time.sleep(0.8)
    clear_input(host, ship)


def dojo_settle(host: Host, ship: Ship, until: float, log: Callable[[str], None]) -> None:
    # wait for an eval someone else left running; abort it once `until` passes.
    aborts = 0
    while True:
        verdict, _, _ = dojo_probe(host, ship, ring_mark(host, ship))
        if verdict == "idle":
            return
        if time.monotonic() >= until:
            if aborts == 2:
                raise RuntimeError(f"the dojo is still {verdict} after two aborts; "
                                   f"`{INVOKE} {ship.name} slog -n 40` shows what it is doing")
            log(f"aborted {'a running thread' if verdict == 'busy' else 'an event still computing'}"
                f" left in the dojo")
            dojo_abort(host, ship, verdict)
            aborts += 1
            until = time.monotonic() + 5
            continue
        time.sleep(0.5)


def dojo_ready(host: Host, ship: Ship, wait: float, log: Callable[[str], None]) -> None:
    # bring the dojo to an empty idle prompt, whatever the last driver left there:
    #
    #   open   a continuation entry: backspace backs out one level
    #   ask    a generator's question: backspace cancels it
    #   None   arvo mid-event: wait up to `wait`, then interrupt
    #
    # A thread still running paints the idle prompt; the accept loop meets it as a busy
    # notice and settles it there.
    until = time.monotonic() + wait
    for _ in range(10):
        state = dojo_prompt(host, ship)
        if state is None:
            dojo_settle(host, ship, until, log)
            continue
        mode, text = state
        if mode == "idle":
            return
        log("backed out of an open dojo entry" if mode == "open"
            else f"cancelled a dojo question nobody answered: {text.strip()}")
        send_data(host, ship, BACKSPACE)
        time.sleep(0.3)
    raise RuntimeError("the dojo would not come back to an empty prompt; "
                       f"`{INVOKE} {ship.name} slog -n 40` shows what it is doing")


def dojo_reject_report(command: str, painted: str, pane: str) -> str:
    # The dojo never committed the command. Say so in the terms the caller can act on:
    # WHAT was sent, what the dojo actually took, and where the two diverge.
    if painted and painted != command:
        where = f"char {len(painted) + 1}"
        detail = (f"ABORT: dojo rejected input at {where} (the dojo's line editor parses as\n"
                  f"you type and drops keystrokes it cannot parse)\n"
                  f"  sent: {command}\n"
                  f"  echo: {painted}\n"
                  f"        {' ' * len(painted)}^ dropped here")
    else:
        detail = (f"ABORT: dojo never committed the command (no `> ` echo came back)\n"
                  f"  sent: {command}\n"
                  f"a syntax error the line editor refuses will do this, as will a dojo\n"
                  f"that stopped accepting input.")
    if re.search(r"/[^\s]*_", command):
        detail += ("\na `_` inside a path literal does not parse in the dojo; build the path "
                   "from a cord:\n  (stab '/lib/a_b/hoon')")
    return detail + "\nthe command did NOT run.\n" + pane


PROBE = ")"  # a keystroke the dojo's parser rejects at any empty prompt. It is how the
# driver asks a question the pane cannot answer (a thread mid-run repaints a bare prompt
# byte-identical to idle): see dojo_probe.
BELL = "\x07"

DOJO_NOISE = re.compile(r"^(dojo: busy \(press backspace to abort\)|interrupt|"
                        r"%dy-no-prompt|\)+|[|/\\-])$")


def render_transcript(raw: str) -> str:
    # pyte renders ~65KB/s, so a huge transcript renders its head and regex-cleans the rest.
    if len(raw) <= DOJO_RENDER_MAX:
        return clean_pane(raw)
    return clean_pane(raw[:DOJO_RENDER_MAX]) + "\n" + _regex_clean(raw[DOJO_RENDER_MAX:])


def truncate_output(text: str, ship: Ship) -> str:
    lines = text.split("\n")
    if len(lines) <= DOJO_OUTPUT_MAX:
        return text
    more = len(lines) - DOJO_OUTPUT_MAX
    return "\n".join(lines[:DOJO_OUTPUT_MAX] +
                     [f"({more} more lines — `{INVOKE} {ship.name} slog` has them all)"])


def dojo_one(ship: Ship, command: str, cfg: Config, *, timeout: int = T_DOJO,
             desk: Desk | None = None, log: Callable[[str], None] | None = None,
             yes: bool = False) -> str:
    # Sentinel-free dojo drive. The daemon ring records every pty byte and capture_at
    # hands back (total, tail-past-watermark) — so a watermark taken just before the
    # command bounds this command's ENTIRE transcript, sync ~& included.
    #
    #   ready    dojo_ready empties the row and backs out of whatever the last driver
    #            left: an open entry, a question, an event still computing.
    #   accept   the dojo commits a line by echoing `> <command>` into scrollback. A
    #            busy notice means a thread was running: settle it, type the line again.
    #   reject   "dojo: failed to process input", or the whole line painted on a quiet
    #            prompt row with no echo behind it.
    #   done     bare prompt + quiet ring, CONFIRMED by the probe. On a chatty ship the
    #            ring is never quiet, so the probe also fires every 3s.
    #
    # Nothing is left behind: a timeout or Ctrl-C aborts the command before raising, and
    # a question the command asks is answered (--yes) or cancelled.
    check_dojo_input(command)
    host = host_for(ship, cfg)
    if log is None:
        log = lambda m: sys.stderr.write(m + "\n")
    if daemon_status(host, ship) is None:
        raise RuntimeError(f'ship "{ship.name}" daemon not running — `{INVOKE} {ship.name} run` first')
    guard_port(host, ship)  # don't drive a pier this config has the wrong port for

    # rsync the desk source while the dojo settles; joined before |commit needs the
    # files on disk.
    sync_err: list[Exception] = []
    sync_thread = None
    if desk:
        def _do_sync() -> None:
            try:
                do_sync(ship, desk, cfg)
            except Exception as e:
                sync_err.append(e)
        sync_thread = threading.Thread(target=_do_sync)
        sync_thread.start()

    prompt = re.compile(r"^(?:~[a-z][a-z0-9-]*:dojo[^ >]*> ?)+")

    if ring_mark(host, ship) < 0:
        # a daemon predating C_CAPTURE_AT answers every capture with the WHOLE ring under
        # a RING_NOMARK total, and every invariant below breaks at once. Refuse plainly
        # rather than pretend to degrade.
        if sync_thread is not None:
            sync_thread.join()
        raise RuntimeError(
            f'ship "{ship.name}" runs a daemon too old for watermarked capture — '
            f'`{INVOKE} {ship.name} engine-upgrade` (hot-swaps the engine in place, no restart)')

    try:
        dojo_ready(host, ship, timeout, log)
    finally:
        if sync_thread is not None:
            sync_thread.join()
    if sync_err:
        raise sync_err[0]

    mark = ring_mark(host, ship)
    send_data(host, ship, command + "\r")
    echo = f"> {command}"
    busy_from = 0  # index into `raw` past which a "dojo: busy" notice answers OUR send
    accepted, raw = False, ""
    try:
        # accept loop: cheap regex-cleaned reads, no pyte. The commit echo is durable
        # (scrollback), so a substring scan of the watermarked tail finds it.
        t_send = time.monotonic()
        last_total, last_change = -1, t_send
        deadline = t_send + max(8, timeout)
        painted, resends = "", 0
        while time.monotonic() < deadline:
            total, raw = capture_at(host, ship, mark)
            now = time.monotonic()
            if total != last_total:
                last_total, last_change = total, now
            rows = [prompt.sub("", l).rstrip() for l in _regex_clean(SPINNER.sub("", raw)).split("\n")]
            if any(r == echo for r in rows):
                accepted = True
                break
            for r in rows:
                bare = r[2:] if r.startswith("> ") else r
                if bare and command.startswith(bare) and len(bare) > len(painted):
                    painted = bare
            if SPINNER.search(raw) and painted in ("", command):
                # a pure computation runs inside the Enter's own event and paints and
                # echoes only when it finishes. The spinner behind a full paint, or behind
                # no paint at all (the line arrived as one batch), is that event. Behind a
                # PARTIAL paint it is a slow keystroke, and the line is not in yet.
                accepted = True
                break
            if any(r == "dojo: failed to process input" for r in rows):
                raise RuntimeError(
                    "ABORT: dojo refused the line (dojo: failed to process input)\n"
                    f"  sent: {command}\n"
                    "the line parsed as keystrokes but the dojo would not run it.\n"
                    "the command did NOT run.\n" + tail_n(clean_pane(raw), 20))
            quiet = now - last_change
            if "dojo: busy" in raw[busy_from:]:
                # a thread was running when the line arrived, and the dojo drops input
                # typed during one. Wait it out (aborting it past the timeout), then type
                # the line again. Scan only PAST busy_from: a notice provoked by an
                # earlier send stays in the transcript, and a sticky predicate would
                # land the command two or three times.
                if resends == 3:
                    break
                dojo_settle(host, ship, t_send + timeout, log)
                clear_input(host, ship)
                # the transcript restarts at the resend: what the settle printed (an
                # aborted thread's `! cancel`) was never this command's output
                mark, busy_from = ring_mark(host, ship), 0
                send_data(host, ship, command + "\r")
                resends += 1
                painted, last_change = "", time.monotonic()
                deadline = last_change + max(8, timeout)
            elif quiet >= 1.0 and now - t_send >= 1.2 and painted and (
                    painted == command or BELL in raw):
                # the line painted in full (or the editor belled) on a quiet ring with no
                # commit echo: refused. Keystrokes and the Enter travel the same queue in
                # order, so text that has been ECHOED was echoed by an arvo that already
                # processed the Enter. A PARTIAL paint with no bell is arvo still working
                # through the keystrokes — waiting on it is not a refusal.
                raise RuntimeError(dojo_reject_report(command, painted, tail_n(clean_pane(raw), 20)))
            time.sleep(0.08)
        if not accepted:
            raise RuntimeError(dojo_reject_report(command, painted, tail_n(clean_pane(raw), 20)))

        # the command only starts RUNNING here, so it gets the whole timeout.
        deadline = time.monotonic() + timeout

        def emit(text: str) -> str:
            # everything past the watermark is this command's transcript; drop only what
            # the drive itself put there. Input-row paints of the command (whole or
            # partial, from typing and resends) can only sit BEFORE the commit echo —
            # after it, an identical row is real output (`[1 2 3]` prints itself).
            stripped = [prompt.sub("", l).rstrip() for l in text.split("\n")]
            echo_i = next((i for i, s in enumerate(stripped) if s == echo), -1)
            out = []
            for i, s in enumerate(stripped):
                if not s or s == echo or DOJO_NOISE.match(s) or ASK_ROW.match(s):
                    continue
                if i < echo_i and command.startswith(s):
                    continue
                out.append(s)
            return "\n".join(trim_blank(out))

        # completion loop: on bare prompt + quiet (or every 3s on a chatty ship), probe
        # to confirm the dojo is idle.
        last_probe, asked_from = 0.0, 0
        while time.monotonic() < deadline:
            total, raw = capture_at(host, ship, mark)
            now = time.monotonic()
            if total != last_total:
                last_total, last_change = total, now
            tail = raw[max(asked_from, len(raw) - 8192):]
            state = prompt_state(tail)
            ask = state
            if state and state[0] == "open" and now - last_change >= 0.3:
                # the line parsed but never closed: a heredoc, an unclosed rune. The dojo
                # waits for input that is not coming, so back out and say so.
                send_data(host, ship, BACKSPACE)
                time.sleep(0.3)
                raise RuntimeError("ABORT: the command left an open entry (an unclosed heredoc "
                                   "or rune); admiral backed out of it.\nthe command did NOT run.")
            if ask and ask[0] == "ask" and now - last_change >= 0.3:
                question = ask[1].strip()
                if yes and YES_NO.search(question):
                    log(f"answered y: {question}")
                    asked_from = len(raw)  # the answered row is history now
                    send_data(host, ship, "y\r")
                    last_change = time.monotonic()
                    continue
                send_data(host, ship, BACKSPACE)
                time.sleep(0.3)
                hint = ("pass --yes to answer y, or give the generator =hard &\n"
                        "  (|nuke %app, =hard &)" if YES_NO.search(question) else
                        f"it wants typed input; answer it from `{INVOKE} {ship.name} attach`")
                raise RuntimeError(f"ABORT: the command asked a question and admiral cancelled it:\n"
                                   f"  {question}\n{hint}\n"
                                   + render_transcript(raw).rstrip())
            # the last prompt painted, not the last row: an app that ~&-prints on every
            # event paints its junk BELOW the live prompt.
            state = prompt_state(raw[-8192:])
            prompt_bare = bool(state) and state[0] == "idle" and not state[1].strip()
            grace = 1.5 if ">=" in raw else 0.25  # effects can trail a poke ack
            quiet_enough = now - last_change >= grace
            if prompt_bare and (quiet_enough or now - last_probe >= 3.0) and now - last_probe >= 0.8:
                verdict, ptotal, reaction = dojo_probe(host, ship, total)
                last_probe = time.monotonic()
                if verdict == "idle":
                    # `raw` is exactly [mark, total) and `reaction` exactly [total, ptotal),
                    # so their concatenation is the whole transcript as of now.
                    log("OK")
                    return emit(render_transcript(raw + reaction))
                # busy, mute, or somebody else's output — not done; re-arm the quiet window.
                last_total, last_change = ptotal, time.monotonic()
            time.sleep(0.1)
        verdict, _, _ = dojo_probe(host, ship, ring_mark(host, ship))
        if verdict != "idle":
            dojo_abort(host, ship, verdict)
        raise RuntimeError(f"TIMEOUT after {timeout}s"
                           + (" — aborted the command" if verdict != "idle" else "") + "\n"
                           + tail_n(render_transcript(raw), 30))
    except KeyboardInterrupt:
        # Ctrl-C or SIGTERM: leave the dojo as we found it. A command still running is
        # aborted; one typed but not taken is wiped off the row.
        if accepted:
            dojo_abort(host, ship, "mute" if SPINNER.search(raw[-256:]) else "busy")
        else:
            clear_input(host, ship)
        raise


def fetch_code(ship: Ship, cfg: Config, log: Callable[[str], None] | None = None) -> str:
    # read +code (changes only on `|code %reset`). Returns the config override when
    # set (no dojo round-trip), else reads it live from the dojo.
    if ship.code:
        return ship.code
    out = dojo_one(ship, "+code", cfg, log=log)
    m = re.search(r"[a-z]{6}(-[a-z]{6}){3}", out)
    if not m:
        raise RuntimeError(f"+code returned no access key (got: {out!r})")
    return m.group(0)


# ── fan: run a per-ship fn across selected ships in parallel ──────────────────
@dataclass
class FanResult:
    ship: str
    ok: bool
    log: str
    value: Any = None
    err: str | None = None


def fan(ships: list[Ship], cfg: Config,
        fn: Callable[[Ship, Host, Callable[[str], None]], Any]) -> list[FanResult]:
    def work(ship: Ship) -> FanResult:
        host = host_for(ship, cfg)
        lines: list[str] = []
        log = lambda m: lines.append(m)
        try:
            value = fn(ship, host, log)
            return FanResult(ship.name, True, "\n".join(lines), value=value)
        except Exception as e:
            return FanResult(ship.name, False, "\n".join(lines), err=str(e))
    # one ship runs inline, so Ctrl-C lands in its own thread and can clean up.
    if len(ships) == 1:
        return [work(ships[0])]
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(ships)))) as ex:
        return list(ex.map(work, ships))


def fan_exit(results: list[FanResult]) -> int:
    # the one exit-code rule for every fanned command: nonzero iff any ship failed.
    return 1 if any(not r.ok for r in results) else 0


def report_dojo(results: list[FanResult]) -> int:
    # status log -> stderr, command output -> stdout. returns the exit code.
    many = len(results) > 1
    for r in results:
        if many:
            sys.stderr.write(f"=== ship: {r.ship} ===\n")
        err = "\n".join(x for x in [r.log, "" if r.ok else (r.err or "")] if x)
        if err:
            sys.stderr.write(err + "\n")
        if r.ok and r.value:
            sys.stdout.write(r.value + "\n")
    return fan_exit(results)


# ── probes: read a ship's actual state (read-only) ────────────────────────────
@dataclass
class ShipState:
    ship: str
    host: str
    daemon: bool                     # the pty-holder daemon answers + owns the urbit child
    pane: bool
    dojo: bool                       # last pane line is a dojo prompt (cheap liveness)
    pier: bool
    holders: list[int]               # the urbit child PID the daemon reports (the pier holder)
    verdict: str
    responsive: bool | None = None   # --probe only: (add 2 2) round-trips
    http: bool | None = None         # the ship's "http" origin /~/login is reachable
    misconfigured: bool = False      # live --http-port disagrees with this config's pier.port
    running_port: int | None = None  # the --http-port the ship is ACTUALLY running on
    version: str | None = None       # engine VERSION baked into the daemon; None = legacy/down


def probe_ship(ship: Ship, cfg: Config, host: Host, probe: bool = False) -> ShipState:
    # one engine round trip for liveness, one for the pane. The daemon's status is
    # authoritative: it owns the urbit child, so "held" + the holder PID come free —
    # no lsof/pgrep guessing, no stale-lockfile ambiguity.
    st = daemon_status(host, ship)
    daemon_up = st is not None
    holders = [st[1]] if st else []

    # is this config the rightful owner of the running pier? (only meaningful when up —
    # a down ship has no live port to disagree with). See guard_port / misconfig_msg.
    misconfigured = False
    running_port: int | None = None
    if daemon_up:
        ok, running_port = running_http_port(host, st[1])
        misconfigured = ok and running_port != ship.pier.port

    pane_text = capture_pane(host, ship, PROBE_TAIL) if daemon_up else ""
    pane = len(pane_text.strip()) > 0
    nonblank = [l for l in pane_text.split("\n") if l.strip()]
    last = nonblank[-1] if nonblank else ""
    dojo = bool(DOJO_PROMPT_RE.search(last))
    pier = pier_exists(host, ship)

    responsive: bool | None = None
    if probe and dojo:
        try:
            responsive = dojo_one(ship, "(add 2 2)", cfg).strip() == "4"
        except Exception:
            responsive = False
    # http check from the local machine (a public origin resolves the same
    # everywhere). None when no "http" is set — no localhost assumption.
    http = web_reachable(ship)

    if not daemon_up:
        verdict = "down"
    elif misconfigured:
        verdict = "misconfigured"    # running, but on a port this config doesn't expect
    elif not dojo:
        verdict = "partial"          # daemon up but no dojo prompt (booting/wedged)
    elif probe and responsive is not True:
        verdict = "partial"
    elif http is False:
        verdict = "partial"
    else:
        verdict = "up"
    # here the daemon owns the urbit child, so "daemon up" IS "pier
    # held" — holders carries the child PID for display.
    return ShipState(ship.name, host.name, daemon_up, pane, dojo, pier,
                     holders, verdict, responsive, http, misconfigured, running_port,
                     version=st[2] if st else None)


# ── command framework ─────────────────────────────────────────────────────────
@dataclass
class Flag:
    name: str            # canonical "--probe" / "-t"
    kind: str            # 'bool' | 'int' | 'str'
    desc: str
    alias: str | None = None
    ph: str | None = None  # value placeholder for help/usage, e.g. "<secs>"


@dataclass
class Pos:
    name: str            # placeholder, e.g. "CMD" / "desk" / "file.py"
    required: bool
    desc: str
    variadic: bool = False


@dataclass
class Cmd:
    name: str
    summary: str
    flags: list[Flag]
    positionals: list[Pos]
    run: Callable[["Parsed"], Any]
    details: str | None = None
    examples: list[str] | None = None
    complete: Callable[[Config | None], list[str]] | None = None
    notes: Callable[[Config | None], str] | None = None


@dataclass
class Parsed:
    cmd: Cmd
    selector: str | None
    cfg: Config
    flags: dict[str, Any]
    pos: list[str]


def flag_brief(f: Flag) -> str:
    return f"[{f.name}{'' if f.kind == 'bool' else ' ' + (f.ph or '<val>')}]"


def pos_brief(p: Pos) -> str:
    inner = f"{p.name}..." if p.variadic else p.name
    return f"<{inner}>" if p.required else f"[{inner}]"


def usage_line(c: Cmd) -> str:
    return " ".join([
        "Usage:", INVOKE, "<ship>", c.name,
        *[flag_brief(f) for f in c.flags], *[pos_brief(p) for p in c.positionals],
    ])


class UsageError(Exception):
    def __init__(self, cmd: Cmd, msg: str):
        super().__init__(f"{msg}\n{usage_line(cmd)}\nSee: {INVOKE} help {cmd.name}")


def find_flag(c: Cmd, tok: str) -> Flag | None:
    return next((f for f in c.flags if f.name == tok or f.alias == tok), None)


def int_val(c: Cmd, flag: str, raw: str | None) -> int:
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        raise UsageError(c, f"{flag} needs a positive number (got {'nothing' if raw is None else repr(raw)})")
    return n


def parse_args(c: Cmd, argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    # A token starting with "-" is a flag (value flags then swallow the NEXT token
    # verbatim, so a negative value like `-S -2000` is fine); else a positional.
    flags: dict[str, Any] = {}
    pos: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            # everything after `--` is positional, whatever it starts with. Urbit names
            # every thread `-name`, so without this `dojo '-time ~s1'` and `dojo
            # '-khan-eval …'` are unreachable — the parser eats the thread as a flag.
            pos.extend(argv[i + 1:])
            break
        if a.startswith("-"):
            f = find_flag(c, a)
            if not f:
                raise UsageError(c, f"Unknown flag: {a}")
            if f.kind == "bool":
                flags[f.name] = True
            else:
                i += 1
                raw = argv[i] if i < len(argv) else None
                if raw is None:
                    raise UsageError(c, f"{a} needs a value ({f.ph or '<val>'})")
                flags[f.name] = int_val(c, a, raw) if f.kind == "int" else raw
        else:
            pos.append(a)
        i += 1
    required = [p for p in c.positionals if p.required]
    if len(pos) < len(required):
        raise UsageError(c, f"missing required argument: {pos_brief(required[len(pos)])}")
    if not any(p.variadic for p in c.positionals) and len(pos) > len(c.positionals):
        raise UsageError(c, f"unexpected argument: {pos[len(c.positionals)]}")
    return flags, pos


# ── command implementations ───────────────────────────────────────────────────
def cmd_dojo(a: Parsed) -> None:
    command = a.pos[0]
    timeout = a.flags.get("-t") or T_DOJO
    yes = bool(a.flags.get("--yes"))

    def work(ship, host, log):
        return truncate_output(dojo_one(ship, command, a.cfg, timeout=timeout, log=log, yes=yes), ship)

    sys.exit(report_dojo(fan(select_ships(a.selector, a.cfg), a.cfg, work)))


COMMIT_ERROR = re.compile(r"^(crud: |syntax error|bail: |-find\.|-mint|nest-fail|mint-|"
                          r"gall: .*fail|.*%error-|\[%error)")
AGENT_LOADED = re.compile(r"^gall: (?:reloading|booted|installing|reviving) %(\S+)")


def commit_verdict(desk: Desk, out: str) -> tuple[bool, str]:
    # one line on what |commit did, read off its transcript: (succeeded, the line).
    lines = out.split("\n")
    errors = [l for l in lines if COMMIT_ERROR.match(l)]
    if errors:
        # the compiler's own line ("syntax error at [7 19] in /app/x/hoon") beats clay's
        # "crud: %into event failed" wrapper around it.
        errors.sort(key=lambda l: l.startswith(("crud: ", "bail: ")))
        return (False, f"commit %{desk.name} FAILED: " + "\n  ".join(dict.fromkeys(errors[:3])))
    added = sum(l.startswith("+ /") for l in lines)
    deleted = sum(l.startswith("- /") for l in lines)
    changed = sum(l.startswith(": /") for l in lines)
    files = added + deleted + changed
    if not files:
        return (True, f"commit %{desk.name}: no changes")
    agents = list(dict.fromkeys(m.group(1) for l in lines if (m := AGENT_LOADED.match(l))))
    counts = " ".join(f"{sign}{n}" for sign, n in (("+", added), ("-", deleted), (":", changed)) if n)
    loaded = ("reloaded " + " ".join(f"%{x}" for x in agents)) if agents else "nothing reloaded"
    return (True, f"commit %{desk.name}: {files} file{'s' if files > 1 else ''} ({counts}), {loaded}")


def desk_suspended(ship: Ship, desk: Desk, cfg: Config, log: Callable[[str], None]) -> bool:
    # a desk whose bill names agents, but which +vats reports suspended, runs nothing:
    # its commits land in clay and deploy nowhere.
    vats = dojo_one(ship, f"+vats %{desk.name}", cfg, log=lambda m: None)
    status = re.search(r"app status:\s+(\S+)", vats)
    bill = re.search(r"/desk/bill:\s+~\[", vats)
    return bool(status and status.group(1) == "suspended" and bill)


def cmd_commit(a: Parsed) -> None:
    desk = resolve_desk(a.pos[0], a.cfg)
    remount = bool(a.flags.get("--remount"))
    timeout = a.flags.get("-t") or T_COMMIT
    ship = resolve_ship(a.selector, a.cfg)
    if remount and self_mounted(ship, desk, a.cfg):
        # |unmount deletes the mount's tracked files and subdirs — here, the checkout.
        raise RuntimeError(f"--remount refused: %{desk.name}'s source is the mount on "
                           f"{ship.name}, and |unmount would delete it")

    def work(ship, host, log):
        # The desk stays mounted across commits, so rsync writes straight into the live
        # unix-synced mount. A sync that DELETES races that mount: clay writes the file
        # back before |commit reads the diff, and the delete never lands. So a delete
        # remounts first — |unmount -> |mount -> rsync -> |commit — and so does
        # --remount (a first commit, or a wedged mount).
        deletes = pending_deletes(ship, desk, a.cfg)
        if deletes:
            log(f"the sync deletes {len(deletes)} file{'s' if len(deletes) > 1 else ''} "
                f"({', '.join(deletes[:3])}{', …' if len(deletes) > 3 else ''}); remounting")
        if remount or deletes:
            dojo_one(ship, f"|unmount %{desk.name}", a.cfg, log=log)
            dojo_one(ship, f"|mount %{desk.name}", a.cfg, log=log)
        out = dojo_one(ship, f"|commit %{desk.name}", a.cfg, timeout=timeout, desk=desk, log=log)
        ok, verdict = commit_verdict(desk, out)
        if ok and desk_suspended(ship, desk, a.cfg, log):
            verdict += (f"\nwarning: %{desk.name} is suspended, so nothing it commits runs. "
                        f"Start it: {INVOKE} {ship.name} dojo '|revive %{desk.name}'")
        return (ok, out, verdict)

    [r] = fan([ship], a.cfg, work)
    if r.log:
        sys.stderr.write(r.log + "\n")
    if not r.ok:
        sys.stderr.write((r.err or "") + "\n")
        sys.exit(1)
    ok, out, verdict = r.value
    if out:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()  # the verdict is the LAST line, even through a pipe
    sys.stderr.write(paint(verdict, "green" if ok else "red", is_tty(sys.stderr)) + "\n")
    sys.exit(0 if ok else 1)


def cmd_code(a: Parsed) -> None:
    results = fan(select_ships(a.selector, a.cfg), a.cfg,
                  lambda ship, host, log: fetch_code(ship, a.cfg, log))
    sys.exit(report_dojo(results))


def cmd_exec(a: Parsed) -> None:
    # Run a shell command on the machine a ship lives on. Everything admiral
    # already knows — which host, over which multiplexed ssh connection, at
    # which pier path — is exactly what a script driving that host has to
    # re-derive from the config by hand.
    command = a.pos[0]
    timeout = a.flags.get("-t") or T_RUN

    def work(ship, host, log):
        cmd = command.replace("{pier}", pier_dir(ship, host)).replace("{root}", host.root)
        return host.exec("sh", ["-c", cmd], timeout_ms=timeout * 1000).rstrip("\n")

    results = fan(select_ships(a.selector, a.cfg), a.cfg, work)
    sys.exit(report_dojo(results))


def cmd_slog(a: Parsed) -> None:
    # the durable console log: everything the ship printed, timestamped, grepable.
    # Unlike `capture` (a render of the daemon's in-memory ring) this survives the
    # daemon, so it answers "what did it say 20 minutes ago" and "why did it not boot".
    nlines = a.flags.get("-n") or 0
    follow = bool(a.flags.get("-f"))
    rebuild = bool(a.flags.get("--rebuild"))
    raw = bool(a.flags.get("--raw"))
    since = a.flags.get("--since")
    count = a.flags.get("--count")
    if count:
        try:
            counter = re.compile(count)
        except re.error as e:
            die(f"--count: bad regex {count!r} ({e})")

    def work(ship, host, log):
        if raw:
            out = eng(host, ship, "__pty-read", "0", check=False)
            head, _, body = out.partition("\n")
            for _ in range(int(head.split()[2]) if len(head.split()) == 3 else 0):
                _, _, body = body.partition("\n")
            return body
        path = console_sync(host, ship, rebuild=rebuild)
        text = path.read_text(errors="replace") if path.exists() else ""
        if since:
            cutoff = fmt_stamp(time.time() - parse_since(since))
            text = "\n".join(l for l in text.split("\n") if l[:23] >= cutoff)
        text = tail_n(text.rstrip("\n"), nlines) if nlines else text
        if count:
            # a count is a METRIC: one number on stdout, so a monitor can read it
            # without re-implementing the window logic (--since/-n) out here.
            return str(sum(1 for l in text.split("\n") if counter.search(l)))
        return text

    if follow:
        return slog_follow(a, work)
    results = fan(select_ships(a.selector, a.cfg), a.cfg, work)
    many = len(results) > 1
    for r in results:
        if many:
            sys.stdout.write(f"=== ship: {r.ship} ===\n")
        if r.ok and r.value is not None:
            sys.stdout.write(r.value.rstrip("\n") + "\n")
        elif not r.ok:
            sys.stderr.write(f"{r.ship}: {r.err}\n")
    sys.exit(fan_exit(results))


def parse_since(s: str) -> float:
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if not m:
        die(f"--since: want <n>[smhd] (e.g. 5m, 2h), got {s!r}")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def slog_follow(a: Parsed, work) -> None:
    # poll-and-print. The tee is a FILE, so following it is just re-syncing the
    # watermark — no subscription, no reconnect, and it resumes cleanly across a
    # daemon restart because the file (and our offset into it) outlive the process.
    ships = select_ships(a.selector, a.cfg)
    if len(ships) != 1:
        die("slog -f: follow one ship at a time")
    ship = ships[0]
    host = host_for(ship, a.cfg)
    seen = 0
    try:
        while True:
            console_sync(host, ship)
            log, _ = console_paths(ship)
            text = log.read_text(errors="replace") if log.exists() else ""
            if len(text) > seen:
                sys.stdout.write(text[seen:])
                sys.stdout.flush()
                seen = len(text)
            time.sleep(1.0)
    except KeyboardInterrupt:
        return


def cmd_capture(a: Parsed) -> None:
    # routed through the one scheduler so capture handles + reports per-ship
    # failures like every other multi-ship command.
    nlines = a.flags.get("-n") or 0

    def work(ship, host, log):
        # -n wants the LAST n lines, so bound the render to a ring tail. ~512 bytes/line
        # is an order of magnitude over a typical dojo row.
        if nlines:
            return tail_n(capture_pane(host, ship, LINE_BYTES * nlines).rstrip("\n"), nlines)
        return capture_pane(host, ship)  # the daemon's whole scrollback ring buffer

    results = fan(select_ships(a.selector, a.cfg), a.cfg, work)
    many = len(results) > 1
    for r in results:
        if many:
            sys.stdout.write(f"=== ship: {r.ship} ===\n")
        if r.ok and r.value is not None:
            sys.stdout.write(r.value.rstrip("\n") + "\n")
        elif not r.ok:
            sys.stderr.write(f"{r.ship}: {r.err}\n")
    sys.exit(fan_exit(results))


def cmd_status(a: Parsed) -> None:
    probe = bool(a.flags.get("--probe"))
    cfg = a.cfg
    ships = select_ships(a.selector, cfg)
    results = fan(ships, cfg,
                  lambda ship, host, log: probe_ship(ship, cfg, host, probe))
    print_status(ships, results, cfg)
    sys.exit(fan_exit(results))


def print_status(ships: list[Ship], results: list[FanResult], cfg: Config) -> None:
    # One card per ship: the live signals (STATE / version / daemon / dojo / http)
    # joined with the static config inventory (host:port, pier, code) — everything
    # knowable about a ship in one place. A card (not a wide table) so a long pier
    # path can't push the line past 80 cols. fan() preserves input order, so ships
    # zip 1:1 with results.
    on = is_tty(sys.stdout)

    def ver_cell(v: ShipState) -> str:
        # version baked into the running daemon vs this control file's VERSION.
        if not v.daemon:
            return paint("-", "red", on)
        if v.version is None:
            return paint("legacy (stale)", "yellow", on)
        cur = v.version == VERSION
        return paint(f"v{v.version} ({'current' if cur else 'stale'})",
                     "green" if cur else "yellow", on)

    namew = max((len(s.name) for s in ships), default=0)
    statew = max((len(r.value.verdict) for r in results if r.ok and r.value),
                 default=0)
    statew = max(statew, len("error"))

    out: list[str] = ["", f"  {paint('config', 'gray', on)} {REPO / CONFIG}", ""]
    for ship, r in zip(ships, results):
        host = host_for(ship, cfg)
        host_cell = f"{host.name}:{ship.pier.port}" if ship.pier.port is not None else host.name
        pier = pier_rel(ship)
        code = "set" if ship.code else "live"
        v: ShipState | None = r.value if (r.ok and r.value is not None) else None

        # pad the PLAIN verdict to width, then color, so ANSI codes never skew it.
        word = v.verdict if v else "error"
        state = verdict_color(word, on) + " " * (statew - len(word))
        ver = ver_cell(v) if v else paint(f"error: {r.err or ''}", "red", on)
        signals = (f"daemon {sig(v.daemon, on)}   dojo {sig(v.dojo, on)}   http {sig(v.http, on)}"
                   if v else "daemon -   dojo -   http -")

        out.append(f"  {paint(ship.name, 'bold', on)}{' ' * (namew - len(ship.name))}   {state}   {ver}")
        out.append(f"    {signals}")
        out.append(f"    host {host_cell}   pier {pier}   code {code}")
        if v and v.misconfigured:
            out.append("    " + paint(
                f"↳ running on port {shown_port(v.running_port)}, not {shown_port(ship.pier.port)} "
                f"— another {CONFIG} owns this pier", "red", on))
        out.append("")
    sys.stdout.write("\n".join(out) + "\n")


def cmd_info(a: Parsed) -> None:
    cfg = a.cfg
    ship = resolve_ship(a.selector, cfg)
    host = host_for(ship, cfg)
    st = probe_ship(ship, cfg, host, probe=True)

    on = is_tty(sys.stdout)

    pier_extra = [
        f"port {ship.pier.port}" if ship.pier.port is not None else None,
        f"loom {ship.pier.loom}" if ship.pier.loom is not None else None,
    ]
    pier_extra = [x for x in pier_extra if x]
    # daemon up == pier held (the daemon owns the urbit child); show the held child PID inline.
    daemon_str = f"{sig(st.daemon, on)} (pid {', '.join(map(str, st.holders))})" if st.holders else sig(st.daemon, on)

    # pier disk usage: total, event log, and one row per epoch — numbered oldest
    # first, so which epochs a chop would delete (all but the last two) reads off
    # the list directly.
    size_rows: list[tuple[str, str]] = [("size", "-")]
    if st.pier:
        pier_kb = du_kb(host, [pier_dir(ship, host)])[0]
        epochs = epoch_names(host, ship)
        log_dir = event_log_dir(ship, host)
        log_kb = log_size_kb(host, ship)
        epoch_kb = du_kb(host, [os.path.join(log_dir, e) for e in epochs])
        size_rows = [
            ("size", print_size(pier_kb)),
            ("log", f"{print_size(log_kb)}, {len(epochs)} epoch{'s' if len(epochs) != 1 else ''}"),
        ]
        name_width = max((len(e) for e in epochs), default=0)
        size_rows += [
            ("", f"{i}  {name.ljust(name_width)}  {print_size(kb)}")
            for i, (name, kb) in enumerate(zip(epochs, epoch_kb), 1)
        ]

    rows: list[tuple[str, str]] = [
        ("ship", ship.name),
        ("host", f"{host.name} ({host.ssh}:{host.root})" if host.ssh else f"local ({host.root})"),
        ("pier", f"{pier_dir(ship, host)} ({', '.join(pier_extra)})" if pier_extra else pier_dir(ship, host)),
        *size_rows,
        ("sock", sock_path(ship, host)),
        ("http", ship.http or "-"),
        ("code", ship.code or "(live from dojo)"),
        ("master ticket", "set" if ship.master_ticket else "-"),
        ("mnemonic", "set" if ship.mnemonic else "-"),
        ("", ""),
        ("verdict", verdict_color(st.verdict, on)),
        ("daemon", daemon_str),
        ("pane", sig(st.pane, on)),
        ("dojo", sig(st.dojo, on)),
        ("pier dir", sig(st.pier, on)),
        ("responsive", sig(st.responsive, on)),
        ("web", sig(st.http, on)),
    ]
    if st.misconfigured:
        rows.append(("misconfig", paint(
            f"running on port {shown_port(st.running_port)}, but {CONFIG} says {shown_port(ship.pier.port)} "
            f"— another config owns this pier", "red", on)))
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        if not k and not v:
            sys.stdout.write("\n")
            continue
        sys.stdout.write(k.ljust(w) + "  " + v + "\n")


def cmd_attach(a: Parsed) -> None:
    # Attach the terminal to the ship's dojo by running THIS file's __attach engine
    # client ON the ship's host: local => a direct exec that inherits your terminal;
    # remote => ssh -tt (inherit allocates the pty) so the remote client is
    # interactive. The client connects to the daemon's socket, sets its pty raw, and
    # relays I/O both ways. Detach with Ctrl-\ — the daemon and ship keep running.
    cfg = a.cfg
    ship = resolve_ship(a.selector, cfg)
    host = host_for(ship, cfg)
    if daemon_status(host, ship) is None:
        raise RuntimeError(
            f'ship "{ship.name}" daemon not running — `{INVOKE} {ship.name} run` first')
    guard_port(host, ship)  # a wrong-port config may not even attach to this pier
    eng(host, ship, "__attach", inherit=True)


def daemon_start(ship: Ship, host: Host) -> tuple[int, int, str | None]:
    # forkpty the urbit pier under a fresh daemon and wait for it to answer. Callers
    # must have checked the pier exists and pushed the tool (ensure_self). Raises with
    # the pty log tail if it never comes up — typically vere's own lock refusal.
    host_mkdirp(host, os.path.dirname(sock_path(ship, host)))
    eng(host, ship, "__daemon", "--", *run_argv(ship, host), check=False)
    for _ in range(10):
        time.sleep(1)
        st = daemon_status(host, ship)
        if st:
            return st
    # vere refuses to boot a pier another runtime already holds (its lock is the
    # authoritative double-boot guard — we don't reimplement it). Surface its message
    # so an "already running elsewhere" reads clearly.
    tail = daemon_log_tail(host, ship)
    hint = "\n  last output:\n" + "\n".join("    " + l for l in tail.split("\n")[-12:]) if tail else ""
    raise RuntimeError(f"ship did not come up (urbit exited at boot — likely the pier is already "
                       f"running, or locked).{hint}")


def pid_alive(host: Host, pid: int | None) -> bool:
    # is pid alive on the host? `kill -0` exits 0 when it is, nonzero when it isn't —
    # works local and over ssh, no parsing.
    if not pid:
        return False
    try:
        host.exec("kill", ["-0", str(pid)])
        return True
    except Exception:
        return False


def reboot_safe(host: Host, ship: Ship, king: int | None) -> bool:
    # Is it SAFE to boot a fresh runtime on this pier? Later than it looks. Two traps:
    #   • the SERF releases .vere.lock within a beat of the kill (~0.2s), but the KING
    #     (the forkpty child) ignores SIGTERM and lingers until the daemon's grace
    #     expires and SIGKILL lands (~8s) — and it holds the http port + runtime the
    #     whole time, so a boot inside that window collides and exits 1.
    #   • during its kill loop the daemon stops answering status, so daemon_status
    #     reads "down" while the king is very much alive.
    # So gate on the KING pid being reaped AND the pier lock released — never on the
    # socket going quiet.
    return not pid_alive(host, king) and not pier_held(host, ship).held


def daemon_stop(ship: Ship, host: Host, timeout: int = 30) -> bool:
    # bring a ship down cleanly (the daemon SIGTERMs its urbit group for a snapshot,
    # then exits) and wait until the pier is actually safe to reboot (see reboot_safe).
    # True once it is. Capture the king pid BEFORE the kill — that's the liveness
    # signal we wait on, since the daemon goes unresponsive mid-kill.
    st = daemon_status(host, ship)
    king = st[1] if st else None
    if reboot_safe(host, ship, king):
        return True
    eng(host, ship, "__kill", check=False)
    for _ in range(timeout):
        time.sleep(1)
        if reboot_safe(host, ship, king):
            return True
    # still not down: SIGKILL the king plus any process still holding the lock (a serf
    # that escaped the king's group), then re-check. Targeted — these pids are this
    # pier's king + lock holders, not a broad sweep.
    held = pier_held(host, ship)
    stragglers = list(dict.fromkeys([*([king] if king else []), *held.holders]))
    if stragglers:
        host.exec("kill", ["-KILL", *map(str, stragglers)], check=False)
        for _ in range(10):
            time.sleep(1)
            if reboot_safe(host, ship, king):
                return True
    return False


def cmd_run(a: Parsed) -> None:
    # turn a ship ON: push the tool to the host, start the pty-holder daemon (which
    # forkpty's `urbit <pier>` and serves the socket), wait for it to answer. The
    # pier must already exist — use `boot` to create one for the first time.
    def work(ship, host, log):
        if not pier_exists(host, ship):
            raise RuntimeError(f'no pier at {pier_dir(ship, host)} — `{INVOKE} {ship.name} boot` first')
        ensure_self(host)
        st = daemon_status(host, ship)
        if st:
            assert_port_match(host, ship, st)
            return f"already running (daemon {st[0]} child {st[1]})"
        st = daemon_start(ship, host)
        return f"running (daemon {st[0]} child {st[1]})"

    sys.exit(report_dojo(fan(select_ships(a.selector, a.cfg), a.cfg, work)))


def cmd_exit(a: Parsed) -> None:
    # turn a ship OFF: tell the daemon to SIGTERM its urbit process group (king +
    # serf) for a clean snapshot, escalating to SIGKILL, then exit. The daemon owns
    # the pty master, so this is how a ship is brought down cleanly.
    def work(ship, host, log):
        if daemon_status(host, ship) is None:
            return "already exited"
        guard_port(host, ship)  # don't shut down a pier another config owns
        eng(host, ship, "__kill", check=False)
        return "exited"

    sys.exit(report_dojo(fan(select_ships(a.selector, a.cfg), a.cfg, work)))


def cmd_version(a: Parsed) -> None:
    # Three versions per ship: the admiral engine its daemon runs (against this file's),
    # the vere runtime, and the kelvin its base desk is built for.
    cfg = a.cfg

    def work(ship, host, log):
        st = daemon_status(host, ship)
        vere = host.exec(vere_binary(ship, host), ["--version"], check=False).split("\n")[0]
        vere = vere.removeprefix("urbit ").strip() or "-"
        kelvin = "-"
        if st is not None:
            m = re.search(r"\d+", dojo_one(ship, "zuse", cfg, log=lambda m: None))
            kelvin = m.group(0) if m else "?"
        return (st, vere, kelvin)

    results = fan(select_ships(a.selector, cfg), cfg, work)
    print_versions(results)
    sys.exit(fan_exit(results))


def version_status(st: tuple[int, int, str | None] | None) -> tuple[str, str]:
    # (running, status) cells for one ship's daemon_status. status is one of
    # current / stale / down, where a pre-versioning daemon reports as "legacy".
    if st is None:
        return ("-", "down")
    if st[2] is None:
        return ("legacy", "stale")
    return (st[2], "current" if st[2] == VERSION else "stale")


def print_versions(results: list[FanResult]) -> None:
    on = is_tty(sys.stdout)
    cols = ["SHIP", "ENGINE", "LATEST", "STATUS", "VERE", "KELVIN"]
    rows: list[list[str]] = []
    for r in results:
        if not r.ok:
            rows.append([r.ship, "?", VERSION, f"error: {r.err or ''}", "?", "?"])
            continue
        st, vere, kelvin = r.value
        running, status = version_status(st)
        rows.append([r.ship, running, VERSION, status, vere, kelvin])

    def cell(s: str) -> str:
        if s == "current":
            return paint(s, "green", on)
        if s == "stale":
            return paint(s, "yellow", on)
        if s == "down" or s.startswith("error"):
            return paint(s, "red", on)
        return s

    render_table(cols, rows, cell)


def cmd_engine_upgrade(a: Parsed) -> None:
    # Bring each ship's on-host engine up to THIS control file's version. First rsync
    # the tool to the host, then ask the running daemon to swap its code IN PLACE — it
    # re-execs the new tool while keeping urbit and the pty alive, so there is no
    # restart and no downtime. A pre-versioning daemon has no swap handler; we detect
    # that (version doesn't change) and fall back to a full restart, after which every
    # future upgrade is in-place. A current ship is left untouched; a stopped ship just
    # gets its on-host tool refreshed.
    def work(ship, host, log):
        st = daemon_status(host, ship)
        running = st[2] if st else None
        if st:
            assert_port_match(host, ship, st)
        log("pushing tool")
        ensure_self(host)  # rsync the new bytes to remote_self BEFORE the daemon re-execs it
        if st is None:
            return f"on-host tool refreshed; daemon not running (start with `{INVOKE} {ship.name} run`)"
        if running == VERSION:
            return f"already current (v{VERSION})"
        old = running or "legacy"
        # in-place swap: signal the daemon, then poll for its reported version to flip.
        log(f"swapping engine in place -> v{VERSION}")
        eng(host, ship, "__upgrade", check=False)
        for _ in range(10):
            time.sleep(1)
            now = daemon_status(host, ship)
            if now and now[2] == VERSION:
                return f"upgraded {old} -> v{VERSION} in place (daemon {now[0]} child {now[1]}, urbit not restarted)"
        # version never flipped: a legacy daemon ignored the frame (or the exec
        # failed). Fall back to a clean restart.
        log("in-place swap unavailable (legacy daemon); restarting")
        if not daemon_stop(ship, host):
            raise RuntimeError("ship did not shut down for upgrade (still running) — try `exit` then `run`")
        now = daemon_start(ship, host)
        return f"upgraded {old} -> v{VERSION} via restart (daemon {now[0]} child {now[1]})"

    sys.exit(report_dojo(fan(select_ships(a.selector, a.cfg), a.cfg, work)))


def event_log_dir(ship: Ship, host: Host) -> str:
    return os.path.join(pier_dir(ship, host), ".urb", "log")


def epoch_names(host: Host, ship: Ship) -> list[str]:
    # the epoch dirs (0i<first-event>) in the pier's event log, oldest first.
    out = host.exec("ls", [event_log_dir(ship, host)], check=False)
    return sorted((n for n in out.split() if n.startswith("0i")), key=lambda n: int(n[2:]))


def du_kb(host: Host, paths: list[str]) -> list[int]:
    # disk usage per path in KB, one du call; 0 for a path du could not size.
    # paths must not overlap — GNU du silently drops an arg whose files were
    # already counted under an earlier one (BSD du prints both).
    if not paths:
        return []
    sizes: dict[str, int] = {}
    for line in host.exec("du", ["-sk", *paths], check=False).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            sizes[parts[1]] = int(parts[0])
    return [sizes.get(p, 0) for p in paths]


def print_size(kb: int) -> str:
    return f"{kb / 1024 / 1024:.1f} GB" if kb >= 1024 * 1024 else f"{kb // 1024} MB"


def log_size_kb(host: Host, ship: Ship) -> int:
    return du_kb(host, [event_log_dir(ship, host)])[0]


def cmd_chop(a: Parsed) -> None:
    # Shrink each ship's pier by truncating its event log — offline, so this is the
    # one maintenance command with downtime. Order matters and is roll FIRST: roll
    # seals the current epoch and opens a fresh one, shunting older epochs out;
    # chop then deletes every epoch older than the two latest. Chop always keeps
    # two, so the first pass on a young pier may reclaim nothing — recurring runs
    # converge. The ship is stopped for the surgery and restarted after (even when
    # roll/chop fail), unless --stay-down or it was already down.
    stay_down = bool(a.flags.get("--stay-down"))

    def work(ship, host, log):
        if not pier_exists(host, ship):
            raise RuntimeError(f"no pier at {pier_dir(ship, host)} — nothing to chop")
        st = daemon_status(host, ship)
        if st:
            assert_port_match(host, ship, st)
        was_running = st is not None
        if was_running:
            log("stopping for chop")
            if not daemon_stop(ship, host):
                raise RuntimeError(f"ship did not shut down for chop — try `{INVOKE} {ship.name} exit`, then rerun")

        binary = vere_binary(ship, host)
        # a docked <pier>/.run self-locates its pier and rejects an explicit one
        # ("invalid command"); only a bare `urbit` needs the pier argument.
        target = [] if binary != "urbit" else [pier_dir(ship, host)]
        loom = ["--loom", str(ship.pier.loom)] if ship.pier.loom is not None else []
        before, before_kb = epoch_names(host, ship), log_size_kb(host, ship)
        failure: Exception | None = None
        try:
            log("rolling a new epoch")
            host.exec(binary, ["roll", *loom, *target], timeout_ms=T_CHOP * 1000)
            log("chopping old epochs")
            host.exec(binary, ["chop", *loom, *target], timeout_ms=T_CHOP * 1000)
        except Exception as e:
            failure = e
        after, after_kb = epoch_names(host, ship), log_size_kb(host, ship)

        restarted = ""
        if was_running and not stay_down:
            log("restarting")
            now = daemon_start(ship, host)
            restarted = f", restarted (daemon {now[0]} child {now[1]})"
        if failure is not None:
            raise RuntimeError(f"roll/chop failed{restarted or ' — ship left down'}: {failure}")

        deleted = [e for e in before if e not in after]
        size = f"{before_kb // 1024} MB -> {after_kb // 1024} MB"
        if deleted:
            return f"rolled + chopped {len(deleted)} epoch{'s' if len(deleted) != 1 else ''}, {size}{restarted}"
        return f"rolled; nothing to chop yet ({len(after)} epochs, two are always kept), {size}{restarted}"

    sys.exit(report_dojo(fan(select_ships(a.selector, a.cfg), a.cfg, work)))


def cmd_vere_upgrade(a: Parsed) -> None:
    # Upgrade each ship's vere runtime binary with vere's offline `next` utility —
    # it downloads the next binary on the pier's release channel (<pier>/.bin/pace)
    # and installs it, so a docked pier's .run is replaced. The pier lock means the
    # ship is stopped for the upgrade and restarted after (even when next fails),
    # unless --stay-down or it was already down.
    stay_down = bool(a.flags.get("--stay-down"))

    def work(ship, host, log):
        if not pier_exists(host, ship):
            raise RuntimeError(f"no pier at {pier_dir(ship, host)} — nothing to upgrade")
        st = daemon_status(host, ship)
        if st:
            assert_port_match(host, ship, st)
        was_running = st is not None
        if was_running:
            log("stopping for next")
            if not daemon_stop(ship, host):
                raise RuntimeError(f"ship did not shut down for next — try `{INVOKE} {ship.name} exit`, then rerun")

        binary = vere_binary(ship, host)
        # a docked <pier>/.run self-locates its pier and rejects an explicit one
        # ("invalid command"); only a bare `urbit` needs the pier argument.
        target = [] if binary != "urbit" else [pier_dir(ship, host)]
        failure: Exception | None = None
        out = ""
        try:
            log("downloading next runtime")
            out = host.exec(binary, ["next", *target], timeout_ms=T_CHOP * 1000).strip()
        except Exception as e:
            failure = e

        restarted = ""
        if was_running and not stay_down:
            log("restarting")
            now = daemon_start(ship, host)
            restarted = f", restarted (daemon {now[0]} child {now[1]})"
        if failure is not None:
            raise RuntimeError(f"next failed{restarted or ' — ship left down'}: {failure}")
        summary = out.splitlines()[-1] if out else "next complete"
        return f"{summary}{restarted}"

    sys.exit(report_dojo(fan(select_ships(a.selector, a.cfg), a.cfg, work)))


def cmd_boot(a: Parsed) -> None:
    # create a pier for the FIRST time (from a keyfile / as a comet / fakezod), then
    # leave it ready for `run`. STUB — not implemented yet.
    raise RuntimeError(
        "boot (first-time pier creation) is not implemented yet.\n"
        "  For now, create the pier manually (e.g. `urbit -w <name> -k <keyfile>` or a\n"
        f"  comet/fakezod), then `{INVOKE} {resolve_ship(a.selector, a.cfg).name} run`.")


def ensure_chromium() -> None:
    home = Path.home()
    dirs = [home / "Library/Caches/ms-playwright", home / ".cache/ms-playwright"]
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")  # honor the cache redirect, if set
    if env and env != "0":
        dirs.insert(0, Path(env))

    def has(d: Path) -> bool:
        try:
            return any(n.startswith("chromium-") for n in os.listdir(d))
        except OSError:
            return False

    if not any(has(d) for d in dirs):
        sys.stderr.write("Installing Chromium for Playwright...\n")
        subprocess.run(["playwright", "install", "chromium"], check=True)


def cmd_test(a: Parsed) -> None:
    headed = bool(a.flags.get("--headed"))
    files = a.pos  # required+variadic => guaranteed >=1 by parse_args
    cfg = a.cfg
    ship = resolve_ship(a.selector, cfg)
    ensure_chromium()
    status = 0
    for t in files:
        file = t
        if not os.path.exists(file):
            file = os.path.join(str(REPO), SCRIPTS, t)
        sys.stderr.write(f"=== running {file} on {ship.name} ===\n")
        # pass the ship + headed flag as argv (read by setup via argv_flag) — no env vars
        pass_args = [file, "--ship", ship.name, *(["--headed"] if headed else [])]
        # admiral's own directory on the path, so a test can `from admiral import with_ship`
        path = os.pathsep.join(x for x in [str(SELF.parent), os.environ.get("PYTHONPATH")] if x)
        try:
            subprocess.run([sys.executable, *pass_args], check=True,
                           env={**os.environ, "PYTHONPATH": path})
        except subprocess.CalledProcessError:
            sys.stderr.write(f"=== FAILED: {file} ===\n")
            status = 1
    sys.exit(status)


# ── shell tab-completion (hidden subcommands) ─────────────────────────────────
def completion_test_files() -> list[str]:
    try:
        return [n for n in os.listdir(os.path.join(str(REPO), SCRIPTS))
                if n.endswith(".py") and n != CLI]
    except OSError:
        return []


def cmd_complete(words: list[str]) -> None:
    # words = everything typed after the CLI name; the last element is the word
    # under the cursor (possibly ''), the rest are completed. Prefix-matched.
    # MUST stay crash-free — a thrown error during a Tab press is terminal noise.
    partial = words[-1] if words else ""
    before = words[:-1]

    def emit(cands: list[str]) -> None:
        hits = [c for c in cands if c.startswith(partial)]
        if hits:
            sys.stdout.write("\n".join(hits) + "\n")

    try:
        cfg: Config | None = load_config()
    except Exception:
        cfg = None
    cmd_names = [c.name for c in COMMANDS]

    if len(before) == 0:
        ships = [s.name for s in cfg.ships] if cfg else []
        return emit([*ships, "help", "init", "enrich", "unenrich"])
    if len(before) == 1:
        if before[0] == "help":
            return emit(cmd_names)
        return emit([*cmd_names, "help"])
    sub = before[1]
    if sub == "help":
        return emit(cmd_names)
    cmd = next((c for c in COMMANDS if c.name == sub), None)
    if not cmd:
        return
    flag_names = []
    for f in cmd.flags:
        flag_names.extend([f.name, f.alias] if f.alias else [f.name])
    return emit([*(cmd.complete(cfg) if cmd.complete else []), *flag_names])


# ── shell install body (the combined `installed` feature) ─────────────────────
# `installed` bundles TWO things into ONE rc managed block: a function named <NAME>
# (so you can type `<NAME> ...` instead of `<INVOKE> ...`) plus tab-completion for it.
# A function (not an alias) is required — zsh expands aliases on the command line
# before completion dispatch, so compdef never fires for an aliased name.
def _complete_fn(shell: str) -> str:
    # the completer function + compdef binding every form you might type
    # (`<NAME>`, `<CLI>`, `<INVOKE>`) — the shell matches the command word exactly.
    exe = f'"{sys.executable}" "{SELF}"'
    fn = f"_{NAME}_complete"
    names = " ".join(dict.fromkeys([NAME, CLI, INVOKE]))
    if shell == "bash":
        tmpl = r"""__FN__() {
  local IFS=$'\n'
  local words=("${COMP_WORDS[@]:1}")
  COMPREPLY=( $(__EXE__ __complete "${words[@]}") )
}
complete -F __FN__ __NAMES__"""
    elif shell == "zsh":
        tmpl = r"""autoload -U compinit && compinit 2>/dev/null
__FN__() {
  local -a cands
  cands=(${(f)"$(__EXE__ __complete "${(@)words[2,CURRENT]}")"})
  compadd -- $cands
}
compdef __FN__ __NAMES__"""
    else:
        raise RuntimeError(f"unsupported shell {shell!r}")
    return tmpl.replace("__NAMES__", names).replace("__EXE__", exe).replace("__FN__", fn)


def _installed_install(shell: str) -> str:
    exe = f'"{sys.executable}" "{SELF}"'
    # `function NAME { }` (keyword form) + a prior `unalias`: a plain `NAME() {…}` fails to
    # parse if NAME is already an alias in the live shell (the alias expands at parse time);
    # the `function` keyword bypasses that, and `unalias` clears any stale alias so the
    # function — not the alias — wins command lookup. Works in both bash and zsh.
    func = f'unalias {NAME} 2>/dev/null\nfunction {NAME} {{ {exe} "$@"; }}'
    return f"{func}\n{_complete_fn(shell)}"


def _installed_uninstall(shell: str) -> str:
    names = " ".join(dict.fromkeys([NAME, CLI, INVOKE]))
    drop = f"complete -r {names} 2>/dev/null" if shell == "bash" else f"compdef -d {names} 2>/dev/null"
    return f"unset -f {NAME} 2>/dev/null\n{drop}"


# ── rc-file managed blocks (persistence) ──────────────────────────────────────
# A feature's install body is written between sentinels so toggles can add/remove
# it idempotently; presence of the opening marker == "persisted ON".
RC_FILES = {"bash": "~/.bashrc", "zsh": "~/.zshrc"}


def rc_path(shell: str, override: str | None = None) -> Path:
    if override:
        return Path(os.path.expanduser(override))
    f = RC_FILES.get(shell)
    if not f:
        raise RuntimeError(f"no rc file known for shell {shell!r} — pass --rc <path>")
    return Path(os.path.expanduser(f))


def _rc_markers(tag: str) -> tuple[str, str]:
    return (f"# >>> {NAME} {tag} >>>", f"# <<< {NAME} {tag} <<<")


def _rc_strip(text: str, tag: str) -> str:
    beg, end = _rc_markers(tag)
    pat = re.compile(rf"\n*{re.escape(beg)}.*?{re.escape(end)}\n?", re.DOTALL)
    return pat.sub("", text)


def rc_present(p: Path, tag: str) -> bool:
    return p.exists() and _rc_markers(tag)[0] in p.read_text()


def rc_write_block(p: Path, tag: str, body: str) -> None:
    beg, end = _rc_markers(tag)
    text = _rc_strip(p.read_text(), tag).rstrip("\n") if p.exists() else ""
    block = f"{beg}\n{body.strip()}\n{end}\n"
    p.write_text((text + "\n\n" + block) if text else block)


def rc_remove_block(p: Path, tag: str) -> None:
    if p.exists():
        p.write_text(_rc_strip(p.read_text(), tag))


def _toggle_opts(args: list[str], cmd: str) -> tuple[str, bool, str | None]:
    # parse [bash|zsh] [--no-persist] [--rc PATH]; shell defaults to $SHELL's basename.
    shell, persist, override = None, True, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-persist":
            persist = False
        elif a == "--rc":
            i += 1
            if i >= len(args):
                raise RuntimeError(f"{cmd}: --rc needs a path")
            override = args[i]
        elif a in ("bash", "zsh"):
            shell = a
        else:
            raise RuntimeError(f"{cmd}: unexpected arg {a!r} (usage: {cmd} [bash|zsh] [--no-persist] [--rc PATH])")
        i += 1
    shell = shell or os.path.basename(os.environ.get("SHELL", ""))
    if shell not in ("bash", "zsh"):
        raise RuntimeError(f'{cmd}: specify a shell — eval "$({INVOKE} {cmd} <bash|zsh>)"')
    return shell, persist, override


def cmd_enrich(args: list[str]) -> str:
    # Enrich the shell with the combined feature (command alias + tab-completion) as ONE rc
    # managed block, and emit a snippet the CURRENT shell must `eval` for immediate effect.
    # Idempotent: re-enriching refreshes the block. `--no-persist` affects only this shell.
    shell, persist, override = _toggle_opts(args, "enrich")
    if not persist:
        sys.stderr.write(f"{NAME}: enriched in this shell only (not saved to an rc file)\n")
        return _installed_install(shell) + "\n"
    p = rc_path(shell, override)
    refresh = rc_present(p, "installed")
    rc_write_block(p, "installed", _installed_install(shell))
    sys.stderr.write(
        f"{NAME}: shell enriched with command alias + tab-completion\n"
        f"  {'refreshed in' if refresh else 'saved to'}: {p}\n"
        f"  active now in this shell, and in every new shell\n"
        f"  run `{NAME} help` to get started\n")
    return _installed_install(shell) + "\n"


def cmd_unenrich(args: list[str]) -> str:
    # Remove the managed block and emit an uninstall snippet for the CURRENT shell.
    # `--no-persist` only cleans this shell.
    shell, persist, override = _toggle_opts(args, "unenrich")
    if not persist:
        sys.stderr.write(f"{NAME}: enrichment removed from this shell only\n")
        return _installed_uninstall(shell) + "\n"
    p = rc_path(shell, override)
    if rc_present(p, "installed"):
        rc_remove_block(p, "installed")
        sys.stderr.write(
            f"{NAME}: shell enrichment removed\n"
            f"  removed from: {p}\n"
            f"  cleared from this shell too\n")
    else:
        sys.stderr.write(
            f"{NAME}: was not enriched in {p}\n"
            f"  cleared this shell anyway\n")
    return _installed_uninstall(shell) + "\n"


# ── interactive wizard: step-by-step prompts + TOML emit (init / add) ─────────
# init builds admiral.toml from scratch; add appends a ship to an existing one. Both
# share one ship-prompt flow (wizard_ship) and one set of input primitives below. The
# primitives read a line at a time from stdin; Ctrl-C / EOF aborts the whole wizard.
class WizardAbort(Exception):
    pass


def _readline(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise WizardAbort()


def ask(label: str, *, default: str | None = None, optional: bool = False,
        validate: Callable[[str], str | None] | None = None) -> str | None:
    # one required/optional free-text prompt. default shown in [..]; empty input takes
    # the default (or, when optional with no default, returns None). validate returns an
    # error string to re-ask, or None to accept.
    suffix = f" [{default}]" if default is not None else (" (optional)" if optional else "")
    while True:
        raw = _readline(f"  {label}{suffix}: ").strip()
        if not raw:
            if default is not None:
                raw = default
            elif optional:
                return None
            else:
                sys.stderr.write("    required — enter a value\n")
                continue
        if validate:
            err = validate(raw)
            if err:
                sys.stderr.write(f"    {err}\n")
                continue
        return raw


def ask_int(label: str, *, default: int | None = None, lo: int = 1, hi: int = 2**31,
            optional: bool = False) -> int | None:
    def v(s: str) -> str | None:
        try:
            n = int(s)
        except ValueError:
            return f"must be a whole number"
        if n < lo or n > hi:
            return f"must be between {lo} and {hi}"
        return None
    out = ask(label, default=(str(default) if default is not None else None),
              optional=optional, validate=v)
    return int(out) if out is not None else None


def ask_yn(label: str, *, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        raw = _readline(f"  {label} [{d}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        sys.stderr.write("    answer y or n\n")


def ask_choice(label: str, options: list[str], *, default: str | None = None) -> str:
    # pick one of options by number or exact name; default is one of the options.
    while True:
        sys.stderr.write(f"  {label}\n")
        for i, o in enumerate(options, 1):
            mark = "  (default)" if o == default else ""
            sys.stderr.write(f"    {i}) {o}{mark}\n")
        raw = _readline("  choice: ").strip()
        if not raw and default is not None:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        if raw in options:
            return raw
        sys.stderr.write("    pick a listed number or name\n")


# ── TOML emit (hand-rolled: stdlib has no writer; our schema is small + fixed) ──
def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_host_block(name: str, ssh: str, root: str) -> str:
    return f"[hosts.{name}]\nssh = {_toml_str(ssh)}\nroot = {_toml_str(root)}"


def render_ship_block(name: str, *, host: str | None, http: str | None,
                      path: str | None, port: int | None, loom: int | None,
                      code: str | None, ames: int | None = None,
                      local: bool = False,
                      master_ticket: str | None = None,
                      mnemonic: str | None = None) -> str:
    lines = [f"[ships.{name}]"]
    if http:
        lines.append(f"http = {_toml_str(http)}")
    if host and host != "local":
        lines.append(f"host = {_toml_str(host)}")
    pier = []
    if path:
        pier.append(f"path = {_toml_str(path)}")
    if port is not None:
        pier.append(f"port = {port}")
    if ames is not None:
        pier.append(f"ames = {ames}")
    if loom is not None:
        pier.append(f"loom = {loom}")
    if local:
        pier.append("local = true")
    if pier:
        lines.append("pier = { " + ", ".join(pier) + " }")
    if code:
        lines.append(f"code = {_toml_str(code)}")
    if master_ticket:
        lines.append(f"master-ticket = {_toml_str(master_ticket)}")
    if mnemonic:
        lines.append(f"mnemonic = {_toml_str(mnemonic)}")
    return "\n".join(lines)


def render_desk_block(name: str, src: str) -> str:
    return f"[[desks]]\nname = {_toml_str(name)}\nsrc = {_toml_str(src)}"


# ── ship sub-wizard (shared by init + add) ────────────────────────────────────
@dataclass
class HostDraft:
    name: str
    ssh: str
    root: str


@dataclass
class ShipDraft:
    block: str            # the [ships.<name>] TOML
    name: str
    new_host: HostDraft | None = None  # a remote host defined inline, to also emit


def _wizard_host() -> HostDraft:
    sys.stderr.write("  new remote host:\n")
    name = ask("host name (label, e.g. drain)",
               validate=lambda s: None if SHIP_NAME_RE.match(s) else "letters/digits/_/- , not starting with a digit")
    ssh = ask("ssh target (user@box; `ssh <target>` must work unattended)")
    root = ask("root (absolute base dir for piers, e.g. /home/urb)",
               validate=lambda s: None if os.path.isabs(s) else "must be an absolute path")
    return HostDraft(name, ssh, root)


def wizard_ship(name: str, existing: Ship | None, existing_hosts: list[str]) -> ShipDraft:
    # prompt for one ship's fields (the NAME is already chosen — it's the selector).
    # `existing` is the current record when EDITING (its values become the [defaults]),
    # or None when adding. Returns the rendered block + any new host drafted inline.
    new_host: HostDraft | None = None
    cur_host = (existing.host if existing else None) or "local"
    host_choice = cur_host
    opts = ["local", *existing_hosts, "define a new remote host…"]
    if len(opts) > 1:
        host_choice = ask_choice("where does it run?", opts, default=cur_host if cur_host in opts else "local")
        if host_choice == opts[-1]:
            new_host = _wizard_host()
            host_choice = new_host.name

    cur_port = existing.pier.port if existing else None
    port = ask_int("http port on the box", default=cur_port if cur_port is not None else 80,
                   lo=1, hi=65535, optional=True)
    cur_http = existing.http if existing else None
    http_default = cur_http or (f"http://localhost:{port}" if (port and host_choice == "local") else None)
    http = ask("http origin playwright/login targets", default=http_default, optional=True,
               validate=lambda s: None if s.strip() and not re.search(r"\s", s) else "no spaces; a host or URL")
    loom = ask_int("loom size (memory pages, power of 2; blank = vere default)",
                   default=(existing.pier.loom if existing else None), lo=1, optional=True)
    path = ask("pier path (relative to the host root, or absolute)",
               default=(existing.pier.path if existing else None) or f".piers/{name}", optional=True)
    code = ask("+code override (skips the live dojo read; blank = read live)",
               default=(existing.code if existing else None), optional=True,
               validate=lambda s: None if CODE_RE.match(s) else "a +code key, e.g. ralnyt-botwyc-batnyt-dozzod")
    ticket = ask("master ticket (key material; blank = none)",
                 default=(existing.master_ticket if existing else None), optional=True,
                 validate=lambda s: None if TICKET_RE.match(s) else "a ticket, e.g. ~ralnyt-botwyc-batnyt-dozzod")
    mnemonic = ask("mnemonic (BIP39 seed words; blank = none)",
                   default=(existing.mnemonic if existing else None), optional=True,
                   validate=lambda s: None if MNEMONIC_RE.match(s) else "a space-separated word list")

    # ames and local aren't prompted for — both are niche — but an existing ship's
    # pinned ames port and its -L pin must survive being re-rendered here, or the wizard
    # would quietly unset them. Dropping `local` is the dangerous one: the next `run`
    # boots a copied pier onto REAL ames and double-boots the ship it was copied from,
    # which is the entire failure the flag exists to make unforgettable.
    block = render_ship_block(name, host=host_choice, http=http, path=path,
                              port=port, ames=(existing.pier.ames if existing else None),
                              loom=loom, code=code,
                              local=(existing.pier.local if existing else False),
                              master_ticket=ticket, mnemonic=mnemonic)
    return ShipDraft(block=block, name=name, new_host=new_host)


# ── non-interactive driver: same TOML emit, fields from --flags (for LLMs/scripts) ─
# add/edit/delete run their wizard when given no flags in a tty; pass any --flag (or run
# without a tty) and they go non-interactive instead — the flags carry exactly the values
# the wizard would have prompted for, validated through the same rules. The scriptable
# path: `admiral sampel-palnet add --host drain --port 8080`. (init keeps its own bare-key
# flag set since it is a top-level verb that also takes --name/--desk/--src.)
INIT_FLAGS = {"name", "host", "http", "port", "ames", "loom", "local", "path", "code",
              "master-ticket", "mnemonic", "ssh", "root", "desk", "src"}

# the per-ship field flags, shared by the `add` and `edit` Cmd entries. All are strings
# (port/loom are range-checked when applied) so add/edit can carry every editable field.
SHIP_FIELD_FLAGS: list[Flag] = [
    Flag("--host", "str", "host the ship runs on (a hosts.<name>, or 'local')", ph="<host>"),
    Flag("--ssh", "str", "ssh target when defining a NEW remote --host (needs --root)", ph="<user@box>"),
    Flag("--root", "str", "absolute base dir when defining a NEW remote --host (needs --ssh)", ph="<dir>"),
    Flag("--http", "str", "http origin playwright/login targets", ph="<url>"),
    Flag("--port", "str", "http bind port on the box (1-65535)", ph="<port>"),
    Flag("--ames", "str", "ames UDP port; pin it so peers' lanes survive a restart", ph="<port>"),
    Flag("--loom", "str", "loom size (memory pages, power of 2)", ph="<pages>"),
    Flag("--local", "str", "boot with -L (fake networking) — pin it for a COPIED pier "
                           "whose original still runs elsewhere", ph="<true|false>"),
    Flag("--path", "str", "pier path (relative to the host root, or absolute)", ph="<path>"),
    Flag("--code", "str", "+code override (skips the live dojo read)", ph="<code>"),
    Flag("--master-ticket", "str", "ship master ticket (key material)", ph="<ticket>"),
    Flag("--mnemonic", "str", "BIP39 mnemonic seed phrase (quote it)", ph="<words>"),
]

_UNSET = object()  # "flag not provided" — distinct from an explicit empty/None value


def _kv_flags(verb: str, args: list[str], allowed: set[str]) -> dict[str, str]:
    # parse `--key value` pairs for the top-level `init` verb (it doesn't use the Cmd
    # framework). Rejects unknown flags + bare positionals so a typo never silently no-ops.
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("--"):
            raise RuntimeError(f"{verb}: unexpected argument {a!r} (flags look like --name <val>; see `{INVOKE} help`)")
        key = a[2:]
        if key not in allowed:
            raise RuntimeError(f"{verb}: unknown flag {a!r} (allowed: {', '.join('--' + k for k in sorted(allowed))})")
        i += 1
        if i >= len(args):
            raise RuntimeError(f"{verb}: {a} needs a value")
        out[key] = args[i]
        i += 1
    return out


def coerce_int(verb: str, key: str, raw: Any, lo: int, hi: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"{verb}: --{key} must be a whole number (got {raw!r})")
    if n < lo or n > hi:
        raise RuntimeError(f"{verb}: --{key} must be between {lo} and {hi} (got {n})")
    return n


def coerce_bool(verb: str, key: str, raw: Any) -> bool:
    s = str(raw).strip().lower()
    if s in ("true", "yes", "on", "1"):
        return True
    if s in ("false", "no", "off", "0", ""):
        return False
    raise RuntimeError(f"{verb}: --{key} must be true or false (got {raw!r})")


def resolve_upsert_name(selector: str, cfg: Config, *, must_exist: bool) -> str:
    # the selector for add/edit/delete IS the ship name.
    name = selector.lstrip("~")
    if not SHIP_NAME_RE.match(name):
        raise RuntimeError(f"ship name {name!r} must match {SHIP_NAME_RE.pattern}")
    if must_exist and ship_by_name(name, cfg) is None:
        raise RuntimeError(f'no ship named "{name}" in {CONFIG} (have: {", ".join(s.name for s in cfg.ships)})')
    return name


def ship_draft_upsert(verb: str, name: str, get: Callable[[str], Any],
                      existing: Ship | None, existing_hosts: list[str]) -> ShipDraft:
    # build a ShipDraft for an UPSERT: each field takes its provided value (via get(key),
    # which returns _UNSET when the flag was omitted), else the existing record's value
    # when editing, else the unset default. --host may be "local", an existing host, or a
    # NEW host name (then --ssh + --root are required to define it inline).
    def pick(key: str, cur: Any) -> Any:
        v = get(key)
        return cur if v is _UNSET else v

    ssh, root = get("ssh"), get("root")
    ssh = None if ssh is _UNSET else ssh
    root = None if root is _UNSET else root
    host = pick("host", (existing.host if existing else None)) or "local"
    new_host: HostDraft | None = None
    if host == "local":
        if ssh or root:
            raise RuntimeError(f"{verb}: --ssh/--root only apply when defining a NEW remote --host")
    elif host in existing_hosts:
        if ssh or root:
            raise RuntimeError(f"{verb}: host {host!r} already exists — drop --ssh/--root (they only define a new host)")
    else:
        if not ssh or not root:
            raise RuntimeError(f"{verb}: host {host!r} is new — pass --ssh and --root to define it "
                               f"(or pick an existing host: {', '.join(['local', *existing_hosts])})")
        if not SHIP_NAME_RE.match(host):
            raise RuntimeError(f"{verb}: host name {host!r} must match {SHIP_NAME_RE.pattern}")
        if not os.path.isabs(root):
            raise RuntimeError(f"{verb}: --root {root!r} must be an absolute path")
        new_host = HostDraft(host, ssh, root)

    port = pick("port", existing.pier.port if existing else None)
    if port is not None and not isinstance(port, int):
        port = coerce_int(verb, "port", port, 1, 65535)
    ames = pick("ames", existing.pier.ames if existing else None)
    if ames is not None and not isinstance(ames, int):
        ames = coerce_int(verb, "ames", ames, 1, 65535)
    loom = pick("loom", existing.pier.loom if existing else None)
    if loom is not None and not isinstance(loom, int):
        loom = coerce_int(verb, "loom", loom, 1, 2**31)
    local = pick("local", existing.pier.local if existing else False)
    if not isinstance(local, bool):
        local = coerce_bool(verb, "local", local)
    http = pick("http", existing.http if existing else None)
    if http is not None and (not http.strip() or re.search(r"\s", http)):
        raise RuntimeError(f"{verb}: --http must be a non-empty host or URL with no spaces (got {http!r})")
    path = pick("path", existing.pier.path if existing else None)
    code = pick("code", existing.code if existing else None)
    if code is not None and not CODE_RE.match(code):
        raise RuntimeError(f"{verb}: --code must be a +code key, e.g. ralnyt-botwyc-batnyt-dozzod (got {code!r})")
    ticket = pick("master-ticket", existing.master_ticket if existing else None)
    if ticket is not None and not TICKET_RE.match(ticket):
        raise RuntimeError(f"{verb}: --master-ticket must be a ticket, e.g. ~ralnyt-botwyc-batnyt-dozzod (got {ticket!r})")
    mnemonic = pick("mnemonic", existing.mnemonic if existing else None)
    if mnemonic is not None and not MNEMONIC_RE.match(mnemonic):
        raise RuntimeError(f"{verb}: --mnemonic must be a space-separated word list (got {mnemonic!r})")

    block = render_ship_block(name, host=host, http=http, path=path, port=port, ames=ames,
                              loom=loom, code=code, local=local,
                              master_ticket=ticket, mnemonic=mnemonic)
    return ShipDraft(block=block, name=name, new_host=new_host)


def upsert_ship_block(text: str, name: str, block: str) -> str:
    # replace the [ships.<name>] table IN PLACE with `block` (preserving its position +
    # the rest of the file's comments/formatting), or append it when the ship is new.
    hdr = re.compile(r"^\s*\[ships\." + re.escape(name) + r"\]\s*(#.*)?$")
    table = re.compile(r"^\s*\[")
    lines = text.split("\n")
    out: list[str] = []
    i, done = 0, False
    while i < len(lines):
        if not done and hdr.match(lines[i]):
            out.extend(block.split("\n"))
            out.append("")  # keep a blank line before whatever followed the old block
            i += 1
            while i < len(lines) and not table.match(lines[i]):
                i += 1
            done = True
        else:
            out.append(lines[i])
            i += 1
    if not done:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n") + "\n"


def remove_ship_block(text: str, name: str) -> str:
    # textually drop the [ships.<name>] table from the config — header line through the
    # line before the next top-level [table]/[[array]] (or EOF). Editing the text (not
    # re-rendering) preserves comments + every other ship's hand formatting. The pier on
    # disk is never touched; this only edits config.
    hdr = re.compile(r"^\s*\[ships\." + re.escape(name) + r"\]\s*(#.*)?$")
    table = re.compile(r"^\s*\[")
    lines = text.split("\n")
    out: list[str] = []
    i, removed = 0, False
    while i < len(lines):
        if hdr.match(lines[i]):
            removed = True
            i += 1
            while i < len(lines) and not table.match(lines[i]):
                i += 1
        else:
            out.append(lines[i])
            i += 1
    if not removed:
        raise RuntimeError(f"no [ships.{name}] block found in {CONFIG}")
    # collapse the blank-line run the removal can leave behind
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n") + "\n"


def _config_target() -> Path:
    # init writes into the current directory; add edits the config found by walk-up.
    return Path.cwd() / CONFIG


def _validate_written(p: Path) -> None:
    # re-parse what we just wrote through the real loader so a malformed wizard result
    # surfaces immediately, not on the next command.
    try:
        load_config()
    except Exception as e:
        raise RuntimeError(f"wrote {p} but it failed validation: {e}")


def cmd_init(args: list[str]) -> None:
    # Wizard (or --flags): create admiral.toml in the current directory (one host? + one
    # ship + one desk), the minimum a working config needs. With no flags in a terminal it
    # prompts; pass --name ... (or run without a tty) to drive it non-interactively.
    target = _config_target()
    if target.exists():
        raise RuntimeError(f"{target} already exists — use `{INVOKE} <ship> add` to add a ship to it")
    kv = _kv_flags("init", args, INIT_FLAGS)

    if kv or not sys.stdin.isatty():
        if not kv:
            raise RuntimeError(f"init needs --flags when not run in a terminal "
                               f"(e.g. {INVOKE} init --name sampel-palnet --http http://localhost:80 --port 80)")
        name = (kv.get("name") or "").lstrip("~")
        if not name:
            raise RuntimeError("init: --name is required (the first ship's name, without ~, e.g. sampel-palnet)")
        ship = ship_draft_upsert("init", name, lambda k: kv.get(k, _UNSET), None, [])
        # --desk defaults to "base"; pass --desk "" for a deskless config (no commits).
        desk_name = kv.get("desk", "base")
        if desk_name and not DESK_NAME_RE.match(desk_name):
            raise RuntimeError(f"init: --desk {desk_name!r} must match {DESK_NAME_RE.pattern}")
        desk_src = kv.get("src", ".")
    else:
        sys.stderr.write(f"\nCreating {target}\n  (Ctrl-C to abort; press Enter to accept a [default])\n\n")
        try:
            name = ask("ship name (without ~, e.g. sampel-palnet)",
                       validate=lambda s: None if SHIP_NAME_RE.match(s) else "letters/digits/_/- , not starting with a digit")
            ship = wizard_ship(name, None, [])
            sys.stderr.write("\n  a desk publishes a directory of this repo into the ship (blank = no desk, no commits):\n")
            desk_name = ask("desk name", default="",
                            validate=lambda s: None if (not s or DESK_NAME_RE.match(s)) else "lowercase letters/digits/- , not starting with a digit")
            desk_src = ask("desk source (repo subpath to sync; '.' = repo root)", default=".") if desk_name else "."
        except WizardAbort:
            sys.stderr.write("\naborted — nothing written\n")
            sys.exit(1)

    blocks = [f"# {NAME} config — see `{INVOKE} help` and this script's header for the full schema."]
    if ship.new_host:
        h = ship.new_host
        blocks.append(render_host_block(h.name, h.ssh, h.root))
    blocks.append(ship.block)
    if desk_name:
        blocks.append(render_desk_block(desk_name, desk_src))
    target.write_text("\n\n".join(blocks) + "\n")
    _validate_written(target)
    sys.stderr.write(
        f"\n{NAME}: wrote {target}\n"
        f"  ship {ship.name}, {f'desk {desk_name}' if desk_name else 'no desk (add [[desks]] to enable commits)'}\n"
        f"  next: `{INVOKE} {ship.name} run` (the pier must already exist; `boot` is a stub)\n")


def cmd_add(a: Parsed) -> None:
    # Upsert one ship (the selector is its name): add it when new, or edit its fields when
    # it already exists — `add` and `edit` are the same command. No flags in a terminal =>
    # the wizard prompts (existing values prefill as [defaults]); pass --flags (or run
    # without a tty) => non-interactive, changing only the fields you name.
    cfg = a.cfg
    name = resolve_upsert_name(a.selector, cfg, must_exist=False)
    existing = ship_by_name(name, cfg)
    if existing:
        guard_not_running(existing, cfg, "edit")  # config is frozen while the ship runs
    verb = "edit" if existing else "add"
    target = REPO / CONFIG
    existing_hosts = list(cfg.hosts)

    if a.flags or not sys.stdin.isatty():
        ship = ship_draft_upsert(verb, name, lambda k: a.flags.get(f"--{k}", _UNSET), existing, existing_hosts)
    else:
        action = "Editing" if existing else "Adding"
        sys.stderr.write(f"\n{action} ship {name} in {target}\n  (Ctrl-C to abort; Enter keeps a [default])\n\n")
        try:
            ship = wizard_ship(name, existing, existing_hosts)
        except WizardAbort:
            sys.stderr.write("\naborted — nothing written\n")
            sys.exit(1)

    text = target.read_text()
    if existing:
        text = upsert_ship_block(text, name, ship.block)
        if ship.new_host:
            h = ship.new_host
            text = text.rstrip("\n") + "\n\n" + render_host_block(h.name, h.ssh, h.root) + "\n"
        target.write_text(text)
    else:
        add = []
        if ship.new_host:
            h = ship.new_host
            add.append(render_host_block(h.name, h.ssh, h.root))
        add.append(ship.block)
        target.write_text(text.rstrip("\n") + "\n\n" + "\n\n".join(add) + "\n")
    _validate_written(target)
    sys.stderr.write(f"\n{NAME}: {'updated' if existing else 'added'} ship {name} in {target}\n")


def cmd_delete(a: Parsed) -> None:
    # The opposite of `add`: remove a ship (the selector) from the config. Leaves the pier
    # on disk untouched — this only edits admiral.toml. In a terminal it confirms first;
    # pass --yes (or run without a tty) to skip the prompt.
    cfg = a.cfg
    if len(cfg.ships) == 1:
        raise RuntimeError(f"delete: \"{cfg.ships[0].name}\" is the only ship; {CONFIG} must list at least one "
                           f"(add another first, or edit the file directly)")
    name = resolve_upsert_name(a.selector, cfg, must_exist=True)
    guard_not_running(resolve_ship(name, cfg), cfg, "delete")  # config is frozen while the ship runs
    target = REPO / CONFIG

    if not a.flags.get("--yes") and sys.stdin.isatty():
        try:
            if not ask_yn(f"delete ship {name} from {CONFIG}? (pier on disk left in place)", default=False):
                sys.stderr.write("\naborted — nothing written\n")
                sys.exit(1)
        except WizardAbort:
            sys.stderr.write("\naborted — nothing written\n")
            sys.exit(1)

    target.write_text(remove_ship_block(target.read_text(), name))
    _validate_written(target)
    sys.stderr.write(f"\n{NAME}: removed ship {name} from {target} (pier on disk left in place)\n")


# ── command registry ──────────────────────────────────────────────────────────
COMMANDS: list[Cmd] = [
    Cmd(
        name="status", summary="the ship's state and config (the default command)",
        flags=[Flag("--probe", "bool", "also require (add 2 2) to return 4, and a live process to hold the pier")],
        positionals=[],
        details=("Probe the ship and print one card: every live signal, then the static config.\n\n"
                 "  STATE    up / partial / down / misconfigured\n"
                 "  version  the admiral engine its daemon runs: current, stale, or legacy\n"
                 "  daemon   the daemon holding the ship's terminal answers on its socket\n"
                 "  dojo     a dojo prompt is on screen\n"
                 "  http     the ship's web address answers GET /~/login\n"
                 "  host     the machine, and the http port the ship binds\n"
                 "  pier     the pier directory\n"
                 "  code     set: +code comes from the config; live: read from the dojo\n\n"
                 "misconfigured: the ship runs on a different http port than this config's "
                 "pier.port, so the pier belongs to another admiral.toml. This config refuses to "
                 "touch it, even to attach.\n\n"
                 f"`{INVOKE}` alone shows every ship in the config; `{INVOKE} <ship>` shows one."),
        examples=[INVOKE, f"{INVOKE} sampel-palnet", f"{INVOKE} sampel-palnet status --probe"],
        run=cmd_status,
    ),
    Cmd(
        name="info", summary="everything admiral knows about the ship",
        flags=[], positionals=[],
        details=("The config block (host, pier dir, port, loom, socket, http, code), then a full "
                 "--probe status: the verdict and every raw signal behind it. daemon shows the "
                 "urbit process id."),
        examples=[f"{INVOKE} sampel-palnet info"],
        run=cmd_info,
    ),
    Cmd(
        name="dojo", summary="run a dojo command and print what it printed",
        flags=[Flag("-t", "int", f"seconds to wait for the dojo and for CMD (default {T_DOJO})", ph="<secs>"),
               Flag("--yes", "bool", "answer y when CMD asks a (y/N) question")],
        positionals=[Pos("CMD", True, "the dojo command, quoted as one argument")],
        details=("Type CMD into the ship's dojo and print what came back: the result, then "
                 "everything the ship printed while it ran (~&, +dbug, slogs).\n\n"
                 "Before typing, admiral clears whatever the dojo was left holding: a half-typed "
                 "line, an unclosed entry, a question nobody answered, an eval still running. A "
                 "running eval gets -t seconds to finish, then it is aborted. When CMD runs past "
                 "-t, it is aborted too, and so is a CMD you Ctrl-C. Nothing is left for the next "
                 "command to trip over.\n\n"
                 "Effects that come back later are not waited for: |hi is an ames round trip, so "
                 "its answer lands after dojo returns. Read those with slog.\n\n"
                 "What the dojo can't take:\n\n"
                 "  a newline          one line only; put multi-line source in a desk file\n"
                 f"  over {DOJO_INPUT_MAX} chars    refused; build it from dojo variables, one call\n"
                 "                     each: =a \"...\"  =b \"...\"  then (weld a b)\n"
                 "  a leading -        every thread is -name; put -- before CMD:\n"
                 "                     dojo -- '-time ~s1'\n"
                 "  _ in a path        /lib/a_b/hoon does not parse; write (stab '/lib/a_b/hoon')\n"
                 "  a (y/N) question   cancelled unless you pass --yes; or give the generator\n"
                 "                     =hard & (|nuke %app, =hard &)\n"
                 f"  a huge value       printing stops at {DOJO_OUTPUT_MAX} lines and slog keeps the rest,\n"
                 "                     but the ship still pays to print it all: print a (lent ...)\n"
                 "                     or one key, not a whole map"),
        examples=[f"{INVOKE} sampel-palnet dojo '(add 2 2)'", f"{INVOKE} sampel-palnet dojo -t 60 '+vats'",
                  f"{INVOKE} sampel-palnet dojo -- '-time ~s1'",
                  f"{INVOKE} sampel-palnet dojo --yes '|nuke %my-app'"],
        run=cmd_dojo,
    ),
    Cmd(
        name="commit", summary="sync a desk's source into the ship and |commit it",
        flags=[Flag("--remount", "bool", "|unmount and |mount the desk first, even with nothing to delete"),
               Flag("-t", "int", f"seconds to wait for the commit and its build (default {T_COMMIT})", ph="<secs>")],
        positionals=[Pos("desk", True, "a desk from the config's [[desks]] list")],
        details=("Copy the desk's source into the ship's mounted desk (rsync, honoring "
                 ".gitignore, deleting what the source no longer has), then |commit it. The "
                 "desk's name and source directory come from the config's [[desks]] list.\n\n"
                 "A sync that deletes files remounts the desk first. A live mount writes a "
                 "deleted file back before |commit can see it, so the delete never lands "
                 "without the remount. --remount forces one on any commit.\n\n"
                 "The last line says what happened: files added (+), deleted (-) and changed "
                 "(:), and the agents that reloaded; or the build error, with a nonzero exit. A "
                 "desk whose agents are suspended gets a warning: what it commits runs nowhere.\n\n"
                 "A new desk:\n\n"
                 f"  {INVOKE} <ship> dojo '|new-desk %mydesk, =gall &'\n"
                 f"  {INVOKE} <ship> dojo '|mount %mydesk'\n"
                 "  copy <pier>/mydesk into your source directory, then list it in admiral.toml:\n"
                 "    [[desks]]\n"
                 "    name = \"mydesk\"\n"
                 "    src = \"mydesk\"\n"
                 f"  {INVOKE} <ship> commit mydesk\n"
                 f"  {INVOKE} <ship> dojo '|install our %mydesk'\n\n"
                 "When the source IS the ship's mounted desk (a checkout inside the pier, config "
                 "in .admiral.toml), that ship skips the rsync and refuses --remount."),
        examples=[f"{INVOKE} sampel-palnet commit base", f"{INVOKE} sampel-palnet commit --remount base"],
        complete=lambda cfg: [d.name for d in cfg.desks] if cfg else [],
        run=cmd_commit,
    ),
    Cmd(
        name="code", summary="print the ship's +code, its web login",
        flags=[], positionals=[],
        details=("Print +code, the password for the ship's web interface. The config's code "
                 "field wins when set (no dojo round trip); otherwise it is read from the dojo."),
        examples=[f"{INVOKE} sampel-palnet code"],
        run=cmd_code,
    ),
    Cmd(
        name="boot", summary="create a pier (not yet: how to do it by hand)",
        flags=[], positionals=[],
        details=("Not implemented yet. Create the pier with the urbit binary, stop it, then "
                 "hand it to admiral:\n\n"
                 "  a fake ship, for development:\n"
                 "    urbit -F zod -c piers/zod\n"
                 "  a real ship, from its keyfile:\n"
                 "    urbit -w sampel-palnet -k sampel-palnet.key -c piers/sampel-palnet\n"
                 "  a comet:\n"
                 "    urbit -c piers/comet\n\n"
                 "Once it reaches the dojo, stop it with Ctrl-D in that terminal. Then:\n\n"
                 f"  {INVOKE} sampel-palnet add --path piers/sampel-palnet --port 8080 \\\n"
                 "      --http http://localhost:8080\n"
                 f"  {INVOKE} sampel-palnet run"),
        run=cmd_boot,
    ),
    Cmd(
        name="run", summary="turn the ship on",
        flags=[], positionals=[],
        details=("Start the daemon that holds the ship: it runs `urbit <pier>` in a terminal of "
                 "its own (with the configured loom and http port) and serves a control socket. "
                 "The daemon outlives your shell. The pier must already exist (help boot). An "
                 "already-running ship is reported as such."),
        examples=[f"{INVOKE} sampel-palnet run"],
        run=cmd_run,
    ),
    Cmd(
        name="exit", summary="turn the ship off",
        flags=[], positionals=[],
        details=("SIGTERM the urbit process group for a clean snapshot, escalating to SIGKILL, "
                 "then stop the daemon. An already-stopped ship is reported as such."),
        examples=[f"{INVOKE} sampel-palnet exit"],
        run=cmd_exit,
    ),
    Cmd(
        name="version", summary="the ship's admiral engine, vere, and kelvin",
        flags=[], positionals=[],
        details=("Three versions:\n\n"
                 "  ENGINE  the admiral engine the ship's daemon runs\n"
                 f"  LATEST  this file's engine (v{VERSION})\n"
                 "  STATUS  current, stale (older, or a pre-versioning 'legacy' daemon), or down\n"
                 "  VERE    the urbit runtime in the pier\n"
                 "  KELVIN  the kelvin the ship's base desk is built for (read from the dojo)\n\n"
                 "engine-upgrade brings a stale ENGINE current; vere-upgrade moves VERE."),
        examples=[f"{INVOKE} sampel-palnet version"],
        run=cmd_version,
    ),
    Cmd(
        name="engine-upgrade", summary="swap in this file's admiral engine, no restart",
        flags=[], positionals=[],
        details=("Copy this file to the ship's host and have the running daemon swap to it in "
                 "place: it re-execs the new code while urbit and its terminal stay alive (same "
                 "process, no restart, no downtime). A pre-versioning 'legacy' daemon cannot swap, "
                 "so it restarts once; every upgrade after is in place. A current ship is left "
                 "alone; a stopped ship just gets the new file.\n\n"
                 "This upgrades admiral, not urbit. For urbit's runtime, see vere-upgrade."),
        examples=[f"{INVOKE} sampel-palnet engine-upgrade"],
        run=cmd_engine_upgrade,
    ),
    Cmd(
        name="vere-upgrade", summary="upgrade the pier's urbit runtime (vere)",
        flags=[Flag("--stay-down", "bool", "leave the ship off afterward")],
        positionals=[],
        details=("Upgrade the pier's vere binary with vere's own `next` utility: it checks the "
                 "pier's release channel (<pier>/.bin/pace, normally 'live'), downloads the next "
                 "version if there is one, and installs it into the pier. The pier must be offline "
                 "for it, so the ship is stopped and restarted after; a ship already down stays "
                 "down. An already-current runtime reports 'up to date'."),
        examples=[f"{INVOKE} sampel-palnet vere-upgrade", f"{INVOKE} sampel-palnet vere-upgrade --stay-down"],
        run=cmd_vere_upgrade,
    ),
    Cmd(
        name="chop", summary="shrink the pier: roll an epoch, chop old ones",
        flags=[Flag("--stay-down", "bool", "leave the ship off afterward")],
        positionals=[],
        details=("Truncate the event log with vere's offline utilities, in the order that makes "
                 "them work: `roll` first (seal the current epoch, open a fresh one), then `chop` "
                 "(delete every epoch older than the two latest). The ship is stopped for it and "
                 "restarted after; a ship already down stays down.\n\n"
                 "chop keeps the two latest epochs, so a first pass on a young pier may reclaim "
                 "nothing; the roll still counts, and the next run chops everything before it. "
                 "Deleted epochs can't be replayed (the snapshot stays authoritative), and roll "
                 "needs free disk for its snapshot copy before chop frees any."),
        examples=[f"{INVOKE} sampel-palnet chop", f"{INVOKE} sampel-palnet chop --stay-down"],
        run=cmd_chop,
    ),
    Cmd(
        name="slog", summary="everything the ship printed, timestamped",
        flags=[Flag("-n", "int", "keep only the last n lines", ph="<n>"),
               Flag("-f", "bool", "follow: print new lines as they land"),
               Flag("--since", "str", "only lines newer than this (5m, 2h, 1d)", ph="<age>"),
               Flag("--count", "str", "print how many lines match this regex, not the lines", ph="<regex>"),
               Flag("--rebuild", "bool", "re-derive the whole log from .pty"),
               Flag("--raw", "bool", "dump .pty unrendered (ANSI and all)")],
        positionals=[],
        details=("Everything the ship's console printed: slogs (~&, +dbug, |hi) and vere's own "
                 "output too (boot, loom, a crash on the way up). One <timestamp>\\t<text> line "
                 "each, so grep is the intended reader.\n\n"
                 "slog is how you read what dojo returned before: |hi's answer, an effect that "
                 "landed late, a crash, why a ship failed to boot.\n\n"
                 "It is not capture. capture renders the daemon's in-memory scrollback, which dies "
                 "with the daemon and evicts as it fills. slog is derived from an on-disk copy of "
                 "the terminal stream, so it outlives the process. A daemon too old to keep that "
                 "copy says so, and engine-upgrade fixes it.\n\n"
                 "The log is a cache of <sock>.pty on the ship's host. --rebuild re-derives it "
                 "from scratch, with the most accurate timestamps. --raw prints the stream "
                 "unrendered for eyeballing escape sequences (invalid bytes become U+FFFD).\n\n"
                 "--count prints one number instead of the lines: how many match the regex "
                 "inside the -n / --since window. Dropped packets an hour, crashes a day."),
        examples=[f"{INVOKE} sampel-palnet slog -n 50", f"{INVOKE} sampel-palnet slog -f",
                  f"{INVOKE} sampel-palnet slog --since 5m",
                  f"{INVOKE} sampel-palnet slog --since 1h --count 'packet dropped'"],
        run=cmd_slog,
    ),
    Cmd(
        name="exec", summary="run a shell command on the ship's host",
        flags=[Flag("-t", "int", f"command timeout in seconds (default {T_RUN})", ph="<secs>")],
        positionals=[Pos("CMD", True, "the shell command, quoted as one argument")],
        details=("Run CMD in a shell on the machine the ship runs on, over the same ssh "
                 "connection every other command uses. A local ship runs it here. It never "
                 "touches the ship: it is for the host around it (free, df, ps, a systemd unit).\n\n"
                 "Two placeholders expand first: {pier} is the pier directory, {root} the host's "
                 "base directory."),
        examples=[f"{INVOKE} sampel-palnet exec 'free -m'",
                  f"{INVOKE} sampel-palnet exec 'du -sh {{pier}}'",
                  f"{INVOKE} sampel-palnet exec 'systemctl is-active caddy'"],
        run=cmd_exec,
    ),
    Cmd(
        name="capture", summary="print the dojo's current screen and scrollback",
        flags=[Flag("-n", "int", "keep only the last n lines", ph="<n>")],
        positionals=[],
        details=("Render the daemon's in-memory scrollback to text. -n trims to the last n lines "
                 "and renders only a bounded tail (about 512 bytes a line), so very long rows can "
                 "come back short of n.\n\n"
                 "Prefer slog. The scrollback is memory: it dies with the daemon and evicts once "
                 "full. It answers 'what is on screen now', not 'what did it say earlier'."),
        examples=[f"{INVOKE} sampel-palnet capture -n 50"],
        run=cmd_capture,
    ),
    Cmd(
        name="attach", summary="a live dojo in your terminal (Ctrl-\\ to detach)",
        flags=[], positionals=[],
        details=("Connect your terminal to the ship's dojo until you detach.\n\n"
                 "Ctrl-\\ (control + backslash) detaches: you are back in your shell, and the "
                 "ship keeps running. Reattach later to the same session.\n\n"
                 "Ctrl-D is not sent: urbit shuts the ship down on it. To stop the ship, use exit."),
        examples=[f"{INVOKE} sampel-palnet attach"],
        run=cmd_attach,
    ),
    Cmd(
        name="test", summary="run Playwright tests against the ship",
        flags=[Flag("--headed", "bool", "show the browser window (default: headless)")],
        positionals=[Pos("file.py", True, f"one or more test files (bare names resolve under {SCRIPTS}/)", variadic=True)],
        details=("Run each test file with --ship <ship> (and --headed when given). Exits nonzero "
                 "if any test fails.\n\n"
                 "A test is a python script using admiral's harness:\n\n"
                 "  from admiral import with_ship\n"
                 "  with with_ship() as s:       # logged in with the ship's +code\n"
                 "      s.goto(\"/\")\n"
                 "      s.shot(\"home\")\n\n"
                 "with_anon() is the same without logging in. s.page is the Playwright page, and "
                 "s.dojo.run(cmd) drives the ship's dojo."),
        examples=[f"{INVOKE} sampel-palnet test login.py", f"{INVOKE} sampel-palnet test --headed login.py nav.py"],
        complete=lambda cfg: completion_test_files(),
        notes=lambda cfg: (f"Test files in {SCRIPTS}/:\n" + "\n".join("  " + f for f in completion_test_files())
                           if completion_test_files() else ""),
        run=cmd_test,
    ),
    Cmd(
        name="add", summary="add the ship to the config, or change its fields",
        flags=SHIP_FIELD_FLAGS, positionals=[],
        details=("Create the ship's [ships.<name>] entry, or edit an existing one (add and edit "
                 "are the same command). With no flags in a terminal, a wizard asks for each "
                 "field; with flags, only those fields change. A new remote --host needs --ssh "
                 "and --root to define it. The pier on disk is never touched.\n\n"
                 "--master-ticket and --mnemonic are stored in admiral.toml as plain text."),
        examples=[f"{INVOKE} sampel-palnet add --path piers/sampel-palnet --port 8080 --http http://localhost:8080",
                  f"{INVOKE} marnec-dozzod add --host drain --ssh urb@box --root /home/urb --port 8080"],
        complete=lambda cfg: (["local", *cfg.hosts] if cfg else []),
        run=cmd_add,
    ),
    Cmd(
        name="edit", summary="the same as add",
        flags=SHIP_FIELD_FLAGS, positionals=[],
        details="The same command as add: with flags, change only those fields; with none, the wizard.",
        examples=[f"{INVOKE} sampel-palnet edit --port 8080",
                  f"{INVOKE} sampel-palnet edit --code ralnyt-botwyc-batnyt-dozzod"],
        complete=lambda cfg: (["local", *cfg.hosts] if cfg else []),
        run=cmd_add,
    ),
    Cmd(
        name="delete", summary="remove the ship from the config; its pier stays",
        flags=[Flag("--yes", "bool", "skip the confirm prompt (implied without a terminal)")],
        positionals=[],
        details=("Remove the ship's [ships.<name>] entry from admiral.toml. The pier on disk is "
                 "not touched. Asks first in a terminal. The last ship in a config can't be removed."),
        examples=[f"{INVOKE} sampel-palnet delete", f"{INVOKE} marnec-dozzod delete --yes"],
        run=cmd_delete,
    ),
]

# commands that take no ship: `help <name>` explains them like the rest.
TOPICS: dict[str, tuple[str, str, str, list[str]]] = {
    "init": (f"{INVOKE} init [--name <ship> --path <pier> --port <port> --http <url> --desk <desk> --src <dir> ...]",
             "create admiral.toml in this directory",
             ("Write a new admiral.toml here with one ship and, optionally, one desk. In a "
              "terminal with no flags it is a wizard; with flags (or no terminal) it takes "
              f"--name plus any field add takes, and --desk / --src for the desk. Refuses when "
              f"admiral.toml already exists: use `{INVOKE} <ship> add` for more ships.\n\n"
              "admiral finds its config by walking up from the current directory to the nearest "
              "admiral.toml (or .admiral.toml), like git finds .git."),
             [f"{INVOKE} init",
              f"{INVOKE} init --name zod --path piers/zod --port 8080 --http http://localhost:8080"]),
    "enrich": (f"{INVOKE} enrich [bash|zsh] [--no-persist] [--rc <file>]",
               "add an `admiral` command and tab completion to your shell",
               ("Print shell code that defines an `admiral` function (so you type `admiral`, "
                "not ./admiral.py) and tab completion for ships, commands, desks and flags. By "
                "default it is also saved to ~/.bashrc or ~/.zshrc; --no-persist keeps it to "
                "this shell, --rc picks another file. Undo with unenrich."),
               ['eval "$(./admiral.py enrich)"']),
    "unenrich": (f"{INVOKE} unenrich [bash|zsh] [--no-persist] [--rc <file>]",
                 "remove what enrich added",
                 "Print shell code that removes the `admiral` function and its completion, and "
                 "take the block back out of the rc file enrich wrote it to.",
                 ['eval "$(./admiral.py unenrich)"']),
}


# ── help rendering ─────────────────────────────────────────────────────────────
HELP_WIDTH = 78


def wrap(text: str, width: int) -> list[str]:
    # flow each paragraph to `width`. A line that starts with a space is preformatted
    # (a table, a code sample) and passes through as written.
    out: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        if para.startswith(" "):
            out.append(para)
            continue
        line = ""
        for word in para.split():
            if line and len(line + " " + word) > width:
                out.append(line)
                line = word
            else:
                line = (line + " " + word) if line else word
        if line:
            out.append(line)
    return out


def label_wrap(label: str, w: int, desc: str, width: int) -> list[str]:
    body = wrap(desc, max(10, width - (w + 2)))
    return [(label.ljust(w) if i == 0 else " " * w) + "  " + l for i, l in enumerate(body)]


def help_menu() -> None:
    color = is_tty(sys.stderr)
    red = lambda s: paint(s, "red", color)
    gray = lambda s: paint(s, "gray", color)
    bold = lambda s: paint(s, "bold", color)
    shell = os.path.basename(os.environ.get("SHELL", ""))

    try:
        enriched = rc_present(rc_path(shell), "installed")  # persisted truth, not the live env flag
    except Exception:
        enriched = False
    white = lambda s: paint(s, "white", color)
    installed_state = white("ON") if enriched else gray("OFF")
    enrich_cmd = gray(f'eval "$({INVOKE} unenrich)"') if enriched else gray(f'eval "$({INVOKE} enrich)"')
    enrich_blurb = "" if enriched else gray(
        f"\n\n  enrichment adds a `{NAME}` command and tab completion to your shell,\n"
        f"  so you type: {NAME} sam<TAB> at<TAB>\n"
        f"  instead of:  {INVOKE} sampel-palnet attach")
    inv = INVOKE
    config_file = os.path.join(str(REPO), CONFIG)
    config_line = (f"Config: {config_file}" if os.path.exists(config_file)
                   else red(f"No config here yet: {INVOKE} init creates one."))

    def sig(c: Cmd) -> str:
        # collapse a long flag list to "[opts]" so one verbose command (add/edit) can't
        # blow out the whole menu's column width; `help <cmd>` shows every flag in full.
        flags = ["[opts]"] if len(c.flags) > 1 else [flag_brief(f) for f in c.flags]
        return " ".join([c.name, *flags, *[pos_brief(p) for p in c.positionals]])

    sigs = [sig(c) for c in COMMANDS]
    w = max(*(len(s) for s in sigs), len("help [command]"))
    rows = "\n".join(f"{sigs[i].ljust(w)}  {c.summary}" for i, c in enumerate(COMMANDS))
    sys.stderr.write(f"""
{bold(f"{CLI}: urbit ship manager")} {gray(f"v{VERSION}")}

Usage

  {gray(f"{inv} <ship> <command> [opts]")}

  <ship> is a ship's name from {CONFIG}, with or without its ~.
  {gray(inv)} alone shows every ship.

Getting started

  1. create a pier with the urbit binary       {gray(f"{inv} help boot")}
  2. {gray(f"{inv} init")}                         write {CONFIG} for it
  3. {gray(f"{inv} <ship> run")}                   turn it on
  4. {gray(f"{inv} <ship> code")}                  its web login password
  5. open the ship's http address and log in with that code
  6. {gray(f"{inv} <ship> dojo '(add 2 2)'")}      run a dojo command
  7. {gray(f"{inv} <ship> attach")}                a live dojo (Ctrl-\\ detaches)

Commands

{rows}
{'help [command]'.ljust(w)}  this menu, or everything about one command

Without a ship: {gray("init")}, {gray("enrich")}, {gray("unenrich")}. {gray(f"{inv} help <name>")} explains each.

{config_line}

shell enrichment {installed_state}. paste to toggle: {enrich_cmd}{enrich_blurb}

""")


def cmd_help(name: str | None = None) -> None:
    if not name:
        return help_menu()
    c = next((x for x in COMMANDS if x.name == name), None)
    W = HELP_WIDTH
    color = is_tty(sys.stderr)
    bold = lambda s: paint(s, "bold", color)
    gray = lambda s: paint(s, "gray", color)
    if not c:
        if name not in TOPICS:
            sys.stderr.write(f'no command "{name}"; {INVOKE} help lists them\n')
            sys.exit(1)
        usage, summary, details, examples = TOPICS[name]
        out = ["", f"{bold(f'{CLI} {name}')} — {summary}", "", f"Usage: {usage}", ""]
        out += wrap(details, W) + ["", "Examples"] + ["  " + gray(e) for e in examples] + [""]
        sys.stderr.write("\n".join(out) + "\n")
        return
    try:
        cfg: Config | None = load_config()
    except Exception:
        cfg = None
    out: list[str] = [""]
    head = f"{CLI} {c.name}"
    for i, l in enumerate(wrap(f"{head} — {c.summary}", W)):
        out.append(l.replace(head, bold(head)) if i == 0 else l)
    out += ["", usage_line(c), ""]
    if c.flags:
        out.append("Flags")
        w = max(len(flag_brief(f)) for f in c.flags)
        for f in c.flags:
            out += label_wrap("  " + flag_brief(f), w + 2, f.desc + (f" (alias {f.alias})" if f.alias else ""), W)
        out.append("")
    if c.positionals:
        out.append("Arguments")
        w = max(len(pos_brief(p)) for p in c.positionals)
        for p in c.positionals:
            out += label_wrap("  " + pos_brief(p), w + 2, p.desc, W)
        out.append("")
    if c.details:
        out += wrap(c.details, W) + [""]
    notes = c.notes(cfg) if c.notes else None
    if notes:
        out += notes.split("\n") + [""]
    if c.examples:
        out.append("Examples")
        for e in c.examples:
            out.append("  " + gray(e))
        out.append("")
    sys.stderr.write("\n".join(out) + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# PLAYWRIGHT HARNESS (library mode)
#
# Wrap a test in with_ship (authed) or with_anon (no login); both launch a browser,
# build the helper `s`, and always tear the browser down. The `s` object exposes raw
# Playwright handles (s.page/ctx/browser), s.goto/shot/post/assert_no_page_errors,
# and s.dojo to read the ship's dojo pane (run/tail/mark/since/runlog/grep/
# assert_clean) — assert frontend with locators, backend with s.dojo.since(mark)
# against a marker dropped before the action.
#
#   screen reader   s.by_role / click_role / fill_role / expect_name / a11y_tree,
#                   s.assert_a11y (axe-core, needs network)
#   console         s.console_tag(surface) / console_records / console_report /
#                   assert_console_clean; an app sets s.console_blame to sort
#                   lines by kind and s.console_fatal to pick which kinds fail
#   crash check     with_ship(crash_check=True) fails a test that passed in the
#                   browser while the ship crashed; s.absolve() forgives a crash
#                   the test provoked on purpose
#
#   from <thismodule> import with_ship
#   with with_ship() as s:
#       mark = s.dojo.mark()
#       s.goto("/foo"); s.shot("foo")
#       s.dojo.assert_clean(mark, [r"crud", r"%bail"])
# ═══════════════════════════════════════════════════════════════════════════════


def argv_flag(name: str) -> str | None:
    # read `--flag value` from this process's argv (test options arrive as argv)
    try:
        i = sys.argv.index(name)
    except ValueError:
        return None
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else None


def run_dojo(ship: Ship, cmd: str, timeout_secs: int | None = None) -> str:
    # run a dojo command synchronously by re-invoking ourselves as `<ship> dojo`.
    #
    # `--` before CMD, always. Every urbit thread is named -<thread>, so without it
    # `run("-time ~s5")` dies in the ARGUMENT PARSER with "Unknown flag: -time" and
    # never reaches the ship — a failure the test then reads as a dojo fault.
    #
    # The outer timeout is a backstop, not the budget: dojo_one owns the real deadline
    # and can legitimately spend accept + run, so only a wedged ssh or a hung daemon
    # should ever trip this. Without it a test hangs forever instead of failing.
    t = timeout_secs if timeout_secs is not None else T_DOJO
    r = subprocess.run([sys.executable, str(SELF), ship.name, "dojo", "-t", str(t), "--", cmd],
                       capture_output=True, text=True, cwd=str(REPO), timeout=2 * t + 30)
    if r.returncode != 0:
        raise RuntimeError(f"dojo failed: {(r.stderr or r.stdout or '').strip()}")
    return r.stdout


def harness_capture(ship: Ship, cfg: Config, lines: int = 200) -> str:
    host = host_for(ship, cfg)
    return tail_n(capture_pane(host, ship, LINE_BYTES * lines).rstrip("\n"), lines)


class Dojo:
    def __init__(self, ship: Ship, cfg: Config):
        self.ship = ship
        self.cfg = cfg
        self._mark_n = 0
        self.run_mark = ""

    def run(self, cmd: str, timeout_secs: int | None = None) -> str:
        return run_dojo(self.ship, cmd, timeout_secs)

    def tail(self, n: int = 40) -> str:
        return "\n".join(harness_capture(self.ship, self.cfg, n).rstrip("\n").split("\n")[-n:])

    def mark(self) -> str:
        mid = f"HARNESS-{os.getpid()}-{self._mark_n}-{int(time.time() * 1000)}"
        self._mark_n += 1
        run_dojo(self.ship, f"'{mid}'", T_HARNESS)
        # the first mark a test places doubles as the run-start mark, so a later
        # runlog()/grep() captures from here without a second round-trip.
        if not self.run_mark:
            self.run_mark = mid
        return mid

    def since(self, mid: str, n: int = 5000) -> str:
        txt = harness_capture(self.ship, self.cfg, n)
        i = txt.rfind(mid)
        return txt if i < 0 else txt[i + len(mid):]

    def runlog(self, n: int = 5000) -> str:
        # lazy: the run-start mark is a dojo round-trip, so only pay for it the
        # first time a test actually reads the ambient log. A browser-only test
        # never calls runlog/grep and so never touches the dojo at setup.
        if not self.run_mark:
            self.run_mark = self.mark()
        return self.since(self.run_mark, n)

    def grep(self, mark: str | None, patterns: list[Pattern], n: int = 5000) -> list[str]:
        hay = self.since(mark, n) if mark else self.runlog(n)
        pats = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        return [l for l in hay.split("\n") if any(p.search(l) for p in pats)]

    # ── bracketed capture ────────────────────────────────────────────────────
    # `since`/`grep` take a single START mark and read to the end of the ring, so
    # under a CONCURRENT dojo writer (another agent spamming the pane) two things
    # break: (1) trailing spam after our output bloats the window, and (2) if the
    # ~4000-line ring evicts the start mark, `rfind` returns -1 and `since`
    # silently hands back the WHOLE buffer (pure spam) — a false miss, no error.
    #
    # `between` brackets the window with BOTH a start and an end sentinel and
    # reads only the slice between them, so interleaved spam outside the bracket
    # is excluded and an evicted sentinel is a hard error, not a silent miss.
    # Pair with `mark()` before and after the dojo work, then capture promptly:
    #
    #       a = s.dojo.mark()
    #       ... clicks / s.dojo.run(...) ...
    #       b = s.dojo.mark()
    #       hits = s.dojo.grep_between(a, b, [r"dong!", r"ding!"])
    def between(self, start: str, end: str, n: int = 5000, tries: int = 4) -> str:
        # a capture taken mid-burst (a concurrent writer streaming into the pane)
        # can render torn — the end sentinel not yet flushed, or the snapshot
        # caught between cursor moves. Re-capture a few times to ride out the
        # burst into the next lull. The start sentinel, once evicted from the ring,
        # is gone for good (oldest bytes drop first), so a missing START is a hard
        # error immediately; only a missing END is worth retrying.
        for attempt in range(tries):
            txt = harness_capture(self.ship, self.cfg, n)
            i = txt.find(start)
            if i < 0:
                raise RuntimeError(
                    f"dojo start sentinel {start!r} not in the captured window — "
                    "either it scrolled out of the ring, or it fell outside the "
                    f"{LINE_BYTES}-bytes-per-line tail this capture rendered "
                    f"(n={n}). Shrink the bracket (fewer/cheaper ops between the "
                    "two mark() calls) or raise n.")
            i += len(start)
            j = txt.find(end, i)
            if j >= 0:
                return txt[i:j]
            if attempt < tries - 1:
                time.sleep(1)
        raise RuntimeError(
            f"dojo end sentinel {end!r} not found after the start sentinel in "
            f"{tries} captures — end mark never printed (did mark() run after the "
            "action?), or the pane never settled")

    def grep_between(self, start: str, end: str, patterns: list[Pattern],
                     n: int = 5000) -> list[str]:
        hay = self.between(start, end, n)
        pats = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        return [l for l in hay.split("\n") if any(p.search(l) for p in pats)]

    def assert_clean_between(self, start: str, end: str,
                             patterns: list[Pattern], n: int = 5000) -> None:
        hits = self.grep_between(start, end, patterns, n)
        if hits:
            raise RuntimeError("dojo not clean:\n" + "\n".join(hits))

    def assert_clean(self, mark: str | None, patterns: list[Pattern], n: int = 5000) -> None:
        hits = self.grep(mark, patterns, n)
        if hits:
            raise RuntimeError("dojo not clean:\n" + "\n".join(hits))


# pinned axe-core for assert_a11y. add_script_tag pulls it from the CDN, so the audit
# needs network; the role and tree helpers are offline.
AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"

# dojo lines that mean the ship crashed, whatever the browser showed
CRASH_PATTERNS: list[Pattern] = [
    r"thread failed",
    r"\bbail\b",
    r"bail:",
    r"ford:",
    r"syntax error",
    r"nest-fail",
    r"mint-vain",
    r"mint-nice",
    r"-find\.",
    r"fish-loop",
]

# console lines from landscape's login and channel teardown, not the app's
CONSOLE_BENIGN = (
    "geteventid",
    "pikes",
    "/~/channel",
    "channel-abort",
    "failed to load resource",
)


class Harness:
    def __init__(self, pw, browser, ctx, page, base, cfg, ship, code, auth):
        self.pw = pw
        self.browser = browser
        self.ctx = ctx
        self.page = page
        self.base = base
        self.cfg = cfg
        self.ship = ship
        self.code = code
        self.auth = auth
        self.page_errors: list[str] = []
        self.dojo = Dojo(ship, cfg)
        self._shot_n = 0
        self._shots_dir = os.path.join(str(REPO), SCRIPTS, "shots")
        self.console: list[dict] = []
        self.console_surface = "(untagged)"
        self.console_benign = CONSOLE_BENIGN
        self.console_blame: dict[str, tuple[str, ...]] = {}
        self.console_fatal: set[str] | None = None  # None: every kind fails
        self._crash_mark: str | None = None
        self._crash_patterns: list[Pattern] = []
        page.on("pageerror", self._on_pageerror)
        page.on("console", self._on_console)

    # SSE pages (datastar @get) hold a connection open, so 'networkidle' never
    # fires — default to domcontentloaded.
    def goto(self, path: str, **o):
        url = self.base + (path if path.startswith("/") else "/" + path)
        return self.page.goto(url, **{"wait_until": "domcontentloaded", **o})

    def shot(self, name: str | None = None) -> str:
        os.makedirs(self._shots_dir, exist_ok=True)
        fname = os.path.join(self._shots_dir, f"{name or f'shot-{self._shot_n}'}.png")
        if not name:
            self._shot_n += 1
        self.page.screenshot(path=fname)
        return fname

    def post(self, path: str, form: dict):
        url = self.base + (path if path.startswith("/") else "/" + path)
        return self.ctx.request.post(url, form=form)

    def assert_no_page_errors(self) -> None:
        if self.page_errors:
            raise RuntimeError("page errors:\n" + "\n".join(self.page_errors))

    # ── screen reader ───────────────────────────────────────────────────────────
    # Find controls by ARIA role + accessible name: what a control IS, not a CSS chain.
    # A name matches as a case-insensitive substring unless exact=True. A miss raises
    # with the a11y tree, so a broken name explains itself without a screenshot.

    def by_role(self, role: str, name: str | None = None, exact: bool = False, **kw):
        if name is None:
            return self.page.get_by_role(role, **kw)
        return self.page.get_by_role(role, name=name, exact=exact, **kw)

    def click_role(self, role: str, name: str, exact: bool = False, **kw):
        found = self.expect_name(role, name, exact=exact, **kw)
        n = found.count()
        if n > 1:
            raise AssertionError(f"click_role: {n} controls match {role} named {name!r} — "
                                 "narrow the name or pass exact=True")
        found.click()
        return found

    def fill_role(self, name: str, value: str, exact: bool = False):
        found = self.expect_name("textbox", name, exact=exact).first
        found.fill(value)
        return found

    def expect_name(self, role: str, name: str, exact: bool = False, **kw):
        found = self.by_role(role, name, exact=exact, **kw)
        if found.count() == 0:
            raise AssertionError(f"no {role} named {name!r}\n--- a11y tree ---\n{self.a11y_tree()}")
        return found

    def a11y_tree(self, root: str = "body") -> str:
        # the tree a screen reader walks, one indented line per node. The cheap stand-in
        # for a screenshot when the question is structural: present, named, checked.
        found = self.page.locator(root).first
        if found.count() == 0:
            return f"(no element matches {root!r})"
        return found.aria_snapshot() or "(empty a11y tree)"

    def audit_a11y(self, impacts=("critical", "serious"), root: str = "document") -> list[dict]:
        try:
            self.page.add_script_tag(url=AXE_CDN)
        except Exception as e:
            raise RuntimeError(f"audit_a11y: could not load axe-core from {AXE_CDN} (offline?): {e}")
        result = self.page.evaluate(
            "async (root) => await axe.run("
            "  root === 'document' ? document : document.querySelector(root),"
            "  {resultTypes: ['violations']})",
            root)
        return [v for v in result.get("violations", []) if v.get("impact") in set(impacts)]

    def assert_a11y(self, impacts=("critical", "serious"), root: str = "document") -> None:
        violations = self.audit_a11y(impacts=impacts, root=root)
        if violations:
            lines = []
            for v in violations:
                nodes = "; ".join(n.get("target", [""])[0] for n in v.get("nodes", [])[:5])
                lines.append(f"  [{v['impact']}] {v['id']}: {v['help']} → {nodes}")
            raise AssertionError("a11y violations:\n" + "\n".join(lines))

    # ── console ─────────────────────────────────────────────────────────────────
    # Every console error and warning plus every uncaught throw, tagged with the
    # surface that was on screen. Sorting happens when read, so an app can set
    # console_blame / console_benign / console_fatal any time before it asks.

    def _on_pageerror(self, e) -> None:
        message = getattr(e, "message", str(e))
        self.page_errors.append(message)
        self.console.append({"severity": "pageerror", "text": message, "where": "",
                             "surface": self.console_surface})

    def _on_console(self, m) -> None:
        if m.type not in ("error", "warning"):
            return
        text = m.text
        where = ""
        try:
            location = m.location or {}
            if location.get("url"):
                where = f"{location['url']}:{location.get('lineNumber', '')}"
            # a library often logs its detail as a second argument, not in .text
            for argument in (m.args or [])[1:]:
                value = argument.json_value()
                if value not in (None, {}, ""):
                    text += f" | {value}"
        except Exception:
            pass
        self.console.append({"severity": m.type, "text": text, "where": where,
                             "surface": self.console_surface})

    def console_tag(self, surface: str) -> None:
        # every record from here on is blamed on `surface`, until the next tag
        self.console_surface = surface

    def console_kind(self, text: str) -> str | None:
        # None for a benign line; else the first console_blame kind whose marker
        # matches, else "other"
        low = text.lower()
        if any(b in low for b in self.console_benign):
            return None
        return next((kind for kind, markers in self.console_blame.items()
                     if any(m in low for m in markers)), "other")

    def console_records(self, kind: str | None = None) -> list[dict]:
        out = []
        for r in self.console:
            k = self.console_kind(r["text"])
            if k is not None and (kind is None or k == kind):
                out.append({**r, "kind": k})
        return out

    def console_summary(self, record: dict) -> str:
        # one report line per record; an app overrides this to pull out what matters
        return " ".join(record["text"].split())[:160]

    def console_report(self) -> dict[str, int]:
        records = self.console_records()
        counts: dict[str, int] = {}
        for r in records:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        print("\n=== console report ===")
        print("   ".join(f"{k}: {n}" for k, n in counts.items()) or "clean")
        for kind in counts:
            print(f"\n-- {kind} --")
            print(f"{'surface':<22}{'severity':<11}message")
            for r in records:
                if r["kind"] == kind:
                    print(f"{r['surface']:<22}{r['severity']:<11}{self.console_summary(r)}")
        print("======================\n")
        return counts

    def assert_console_clean(self) -> None:
        counts = self.console_report()
        fatal = {k: n for k, n in counts.items()
                 if self.console_fatal is None or k in self.console_fatal}
        if fatal:
            raise RuntimeError(f"console errors ({', '.join(f'{k}: {n}' for k, n in fatal.items())}) "
                               "— see the report above")

    # ── crash check ─────────────────────────────────────────────────────────────
    # A server-side crash often leaves the browser looking healthy: the SSE stops and
    # later assertions pass against the last good DOM. The dojo saw it.

    def watch_crashes(self, patterns: list[Pattern]) -> None:
        self._crash_patterns = patterns
        self._crash_mark = self.dojo.mark()

    def absolve(self) -> None:
        # forgive everything printed so far: the test provoked that crash on purpose
        if self._crash_mark is None:
            raise RuntimeError("absolve: this run has no crash check (with_ship(crash_check=True))")
        self._crash_mark = self.dojo.mark()

    def check_crashes(self) -> None:
        if self._crash_mark is None:
            return
        hits = self.dojo.grep(self._crash_mark, self._crash_patterns)
        if hits:
            raise RuntimeError("test passed in the browser but the ship crashed during it:\n"
                               + "\n".join(hits[:20]))

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self.pw.stop()


def _login(ctx, base: str, code: str) -> None:
    # POST +code to plant the urbauth cookie, then inject it into the browser
    # context. We use plain urllib + ctx.add_cookies (NOT ctx.request.post) so we
    # control redirect handling and read the Set-Cookie off urbit's login response
    # directly — no dependency on the client's cookie-jar parsing.
    data = urllib.parse.urlencode({"password": code}).encode()
    req = urllib.request.Request(f"{base}/~/login", data=data, method="POST",
                                 headers={"content-type": "application/x-www-form-urlencoded"})

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=T_RUN)
        headers = resp.headers
    except urllib.error.HTTPError as e:
        if e.code >= 400:
            raise RuntimeError(f"login failed: {e.code} {e.read().decode(errors='replace')}")
        headers = e.headers  # 3xx lands here (NoRedirect); cookie is still on it
    cookies = headers.get_all("set-cookie") or ([headers.get("set-cookie")] if headers.get("set-cookie") else [])
    set_cookie = cookies[0] if cookies else ""
    m = re.match(r"([^=]+)=([^;]+)", set_cookie or "")
    if not m:
        raise RuntimeError(f'login: no urbauth cookie in response (set-cookie: "{set_cookie}")')
    ctx.add_cookies([{"name": m.group(1), "value": m.group(2), "url": base}])


def setup(*, ship: str | None = None, auth: bool = True, base: str | None = None,
          headless: bool | None = None, log_console: bool = False) -> Harness:
    cfg = load_config()
    name = ship or argv_flag("--ship")
    if not name:
        raise RuntimeError(f"no ship to test against: pass --ship <name> ({INVOKE} <ship> test does)")
    ship_obj = resolve_ship(name, cfg)
    if headless is None:
        headless = "--headed" not in sys.argv
    base = (base or ship_base(ship_obj)).rstrip("/")
    code = fetch_code(ship_obj, cfg) if auth else None

    from playwright.sync_api import sync_playwright  # LAZY — the CLI never hits this
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    ctx = browser.new_context()
    page = ctx.new_page()
    h = Harness(pw, browser, ctx, page, base, cfg, ship_obj, code, auth)
    if log_console:
        page.on("console", lambda m: print("[console]", m.text))
    if auth:
        _login(ctx, base, code)
    # run_mark (the start marker scoping runlog()) is set lazily on first
    # runlog/grep call — see Dojo.runlog. Setup itself never hits the dojo when
    # the `code` override is set, so a browser-only test stays sub-second.
    return h


@contextmanager
def with_ship(*, crash_check: bool | list[Pattern] = False, **opts):
    # authed run — logs in with the driven ship's code, always tears down.
    # crash_check: True watches CRASH_PATTERNS, a list watches those instead.
    s = setup(auth=True, **opts)
    try:
        if crash_check:
            s.watch_crashes(CRASH_PATTERNS if crash_check is True else crash_check)
        yield s
        s.check_crashes()  # only on success: a test that raised keeps its own failure
    finally:
        s.close()


@contextmanager
def with_anon(**opts):
    # unauthed run — no cookie; for public / read-only endpoints
    s = setup(auth=False, **opts)
    try:
        yield s
    finally:
        s.close()


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE (host-local pty holder)
#
# These run ON the ship's host, invoked as `python3 <this file> __<sub> --sock S`.
# They know nothing about CONFIG, ships, or hosts — they operate on a single unix
# socket. The frontend (above) drives them through Host.exec. `__daemon` forkpty's
# the target command and serves the socket; the rest are thin clients to it.
#
# Wire protocol: a 5-byte header (type:u8, length:u32-be) then `length` payload
# bytes. One framing for both directions.
# ═══════════════════════════════════════════════════════════════════════════════
HDR = struct.Struct("!BI")
C_ATTACH, C_DATA, C_WINCH, C_CAPTURE, C_PING, C_KILL, C_UPGRADE = 1, 2, 3, 4, 5, 6, 7
C_CAPTURE_AT = 8  # capture only ring bytes past a caller-held watermark (vs whole ring)
S_DATA, S_EXIT, S_SNAPSHOT, S_PONG = 1, 2, 3, 4
S_SNAPSHOT_AT = 5  # reply to C_CAPTURE_AT: 8-byte total-bytes-written + the requested tail
WINSZ = struct.Struct("!HHHH")
RING_MAX = 64 * 1024 * 1024  # scrollback ring cap; oldest bytes drop past this.
# Sized to survive a CONCURRENT dojo writer (a second agent dumping output to the
# same console) AND a chatty app slogging through a full rebuild. A 256KB ring was
# smaller than one large burst (e.g. a tool-guide dump), so any burst evicted a sentinel
# the moment it landed. 4MB held many bursts but a `|commit` that rebuilds an app
# ~&-printing every event still outran it mid-build — the command echo rolled off before
# the done echo landed, so the start..done slice was unrecoverable. 64MB holds a full
# noisy rebuild, so a window bracketed by the sentinels survives until the next capture.
# (RING_MAX is baked into the running daemon — `engine-upgrade` a ship to apply a new value.)

ATTACH_TAIL = 64 * 1024  # ring bytes replayed to the terminal on attach.
# The ring is sized for MACHINE readers hunting a sentinel across a noisy rebuild. A
# human attaching wants the screen they left, and a whole ring is neither: replaying all
# of it made attach cost a terminal parse — and, on a remote host, an ssh transfer —
# proportional to how long the daemon had been up, so the dojo churned through megabytes
# of stale redraw before the prompt was usable (measured: 7.7MB on a prod ship). 64KB is
# ~6 screens. What it skips is not lost: `.pty` is the durable copy and `slog` renders
# it properly, which is the right way to read history back anyway.
#
# Unlike RING_MAX this is NOT baked into the daemon. The cap rides in the C_ATTACH frame
# AND the client trims whatever comes back, so both halves move with the pushed engine
# and an un-upgraded daemon still gives a short attach (it just ships the bytes first).

ATTACH_SLACK = 4 * 1024  # asked for on top of ATTACH_TAIL, and thrown away by the client.
# The daemon cuts at a byte offset, which lands mid-line (and mid-escape-sequence) far
# more often than not — the very thing +attach_snapshot exists to clean up. Asking for
# exactly ATTACH_TAIL would hand the client a snapshot already at the cap, so its trim
# would decline to touch it and the cut would reach the screen. Asking for a little more
# guarantees there is something to discard, so the replay always opens on a line.

PTY_MAX = 16 * 1024 * 1024  # `.pty` rotation cap; ONE older generation is kept.
# Sized against the COOK, not the disk. pyte renders ~65KB/s (measured), so `--rebuild`
# is ~4min at this cap and would be ~34min at 128MB — an escape hatch that cannot be
# reached is not one. 16MB x 2 generations is still far more console than the question
# this exists to answer ("what did it print 30s ago, mid-rebuild") ever needs.
IDX = struct.Struct("!Qd")   # `.idx` record: (byte offset into .pty, unix time)
IDX_GAP = 0.05  # seconds; don't index a read that lands within this of the last record.
# A record per READ was the obvious design and it is wildly wasteful: the dojo repaints
# its spinner in tiny escape-sequence writes, so reads average ~14 bytes and a 16-byte
# record each made `.idx` LARGER than the `.pty` it indexes (measured: 38352B of index
# for 32776B of pty). Records are a sparse map anyway — a reader bisects and floors to
# the nearest record at or before its offset — so one per 50ms costs a stamp no worse
# than 50ms stale and collapses a spinner burst to almost nothing. Correlating a slog to
# a command window is a ~second-scale question; 50ms is far below the noise.
# The ring above is memory: it dies with the daemon and evicts past RING_MAX, so a slog
# from a poke 30s into a noisy rebuild is gone with nothing on disk remembering it.
# `.pty` is the durable counterpart — every byte the console printed, raw, appended
# forever. It is the SOURCE OF TRUTH and is never transformed here: rendering it to text
# needs a terminal emulator (pyte), which the engine cannot import (stdlib-only, see the
# section header above), so the daemon only ever appends and the control side cooks.
# `.idx` carries the timestamps `.pty` deliberately does not, keeping the byte stream
# exactly what vere wrote. (Both are baked into the daemon — `engine-upgrade` a ship to apply.)


def die(msg: str) -> None:
    # engine-side fatal: report and exit nonzero. Engine subcommands run on the ship's
    # host, so this lands on the frontend's captured stderr for that exec.
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def send_frame(sock: socket.socket, t: int, data: bytes = b"") -> None:
    sock.sendall(HDR.pack(t, len(data)) + data)


def write_all(fd: int, data: bytes) -> None:
    # os.write may write short; a partial write would silently desync `.pty` from the
    # `.idx` offsets that index it, so loop until the whole chunk lands.
    while data:
        data = data[os.write(fd, data):]


class FrameReader:
    # accumulate bytes off a (possibly non-blocking) socket; yield whole frames.
    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        while len(self.buf) >= HDR.size:
            t, n = HDR.unpack_from(self.buf)
            if len(self.buf) < HDR.size + n:
                break
            payload = bytes(self.buf[HDR.size:HDR.size + n])
            del self.buf[:HDR.size + n]
            yield t, payload


def engine_ping(path: str) -> tuple[int, int, str | None] | None:
    # (daemon_pid, child_pid, engine_version) if a daemon answers, else None. The
    # version field is absent from a pre-versioning daemon's pong -> None there.
    if not os.path.exists(path):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(3.0)
        s.connect(path)
        send_frame(s, C_PING)
        fr = FrameReader()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                return None
            for t, payload in fr.feed(chunk):
                if t == S_PONG:
                    parts = payload.decode().split()
                    ver = parts[2] if len(parts) > 2 else None
                    return (int(parts[0]), int(parts[1]), ver)
    except (OSError, ValueError):
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def engine_winsize(fd: int) -> bytes:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        return WINSZ.pack(24, 80, 0, 0)


def engine_daemonize(logpath: str) -> None:
    # double-fork; setsid between so the final process has no controlling tty
    # (forkpty's child then acquires its own). std fds -> /dev/null / log.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDONLY)
    log = os.open(logpath, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(devnull)
    os.close(log)


def engine_run_daemon(path: str, cmd: list[str], winsz: bytes | None) -> None:
    # fresh boot: forkpty the target command, bind the control socket, then serve.
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    pid, master = os.forkpty()
    if pid == 0:
        try:
            os.execvp(cmd[0], cmd)
        except OSError as e:
            sys.stderr.write(f"engine: exec {cmd!r} failed: {e}\n")
        os._exit(127)

    if winsz:
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, winsz)
        except OSError:
            pass
    os.set_blocking(master, False)

    listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listen.bind(path)
    listen.listen(16)
    listen.setblocking(False)
    engine_serve(path, master, pid, listen, bytearray())


def engine_adopt(path: str, master: int, pid: int, listen_fd: int) -> None:
    # resume serving after an in-place upgrade exec (see the C_UPGRADE branch). The
    # pty master, urbit child, and listen socket were all inherited across execv — so
    # NO forkpty, NO bind, NO daemonize: this process is already the daemon, same PID
    # and same urbit child as before, only the python image changed. The previous
    # image handed the scrollback ring over via a sidecar file.
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    ring = bytearray()
    try:
        with open(path + ".ring", "rb") as f:
            ring = bytearray(f.read())
        os.unlink(path + ".ring")
    except OSError:
        pass
    os.set_blocking(master, False)
    listen = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=listen_fd)
    listen.setblocking(False)
    engine_serve(path, master, pid, listen, ring)


def engine_serve(path: str, master: int, pid: int, listen: socket.socket, ring: bytearray) -> None:
    # the daemon's event loop: pump the pty master <-> attached clients, answer
    # control frames. Entered fresh (engine_run_daemon) or after an in-place swap
    # (engine_adopt) — identical from here on.
    sel = selectors.DefaultSelector()
    sel.register(master, selectors.EVENT_READ, "master")
    sel.register(listen, selectors.EVENT_READ, "listen")

    clients: dict[int, dict] = {}
    # total bytes ever written to the pty since this daemon image started. The ring is
    # the capped TAIL of that stream, so ring[0] sits at absolute offset total-len(ring).
    # A C_CAPTURE_AT caller holds a `total` watermark from before its command and asks
    # for everything past it — so it searches only its own output, not the whole ring.
    total = len(ring)

    # the durable pty tee (see PTY_MAX). Opened BY PATH rather than inherited, so an
    # in-place upgrade needs no handover: C_UPGRADE re-execs this process and we simply
    # reopen and keep appending, unlike the in-memory ring and its `.ring` sidecar.
    tee: dict[str, Any] = {"pty": -1, "idx": -1, "off": 0, "stamp": 0.0, "degraded": ""}

    def tee_fail(what: str, e: Exception) -> None:
        # a full or unwritable disk must NEVER take the ship down: drop the tee, note
        # why, keep serving. The console log is a debugging aid; the pier is the job.
        tee["degraded"] = f"{what}: {e}"
        for k in ("pty", "idx"):
            try:
                if tee[k] >= 0:
                    os.close(tee[k])
            except OSError:
                pass
            tee[k] = -1
        try:
            with open(path + ".log", "a") as f:
                f.write(f"[pty tee degraded — console log stops here: {what}: {e}]\n")
        except OSError:
            pass

    def tee_open() -> None:
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            tee["pty"] = os.open(path + ".pty", flags, 0o600)
            tee["idx"] = os.open(path + ".idx", flags, 0o600)
            tee["off"] = os.fstat(tee["pty"]).st_size
            tee["stamp"] = 0.0  # forces the first write to this file to be indexed
        except OSError as e:
            tee_fail("open", e)

    def tee_rotate() -> None:
        # the DAEMON rotates because it is the only writer: it holds the O_APPEND fd, so
        # a rename by anyone else would leave it writing to a detached inode while the
        # new `.pty` stayed empty forever. `.idx` rotates in lockstep — its offsets are
        # into the current `.pty`, so the two must never straddle a rotation.
        for k in ("pty", "idx"):
            os.close(tee[k])
            tee[k] = -1
        for suffix in (".pty", ".idx"):
            os.replace(path + suffix, path + suffix + ".1")
        tee_open()

    def tee_write(data: bytes) -> None:
        if tee["pty"] < 0:
            return
        try:
            if tee["off"] >= PTY_MAX:
                tee_rotate()
                if tee["pty"] < 0:
                    return
            # `.pty` first: a crash between the two costs this chunk's TIMESTAMP, which a
            # reader can interpolate. The reverse would leave `.idx` pointing past EOF.
            off, now = tee["off"], time.time()
            write_all(tee["pty"], data)
            tee["off"] = off + len(data)
            if now - tee["stamp"] >= IDX_GAP:
                write_all(tee["idx"], IDX.pack(off, now))
                tee["stamp"] = now
        except OSError as e:
            tee_fail("write", e)

    tee_open()

    def safe(c: dict, t: int, data: bytes = b"") -> bool:
        # client socks are non-blocking for the select read loop, but a frame can be
        # large (a 256KB ring snapshot) and sendall on a non-blocking sock raises
        # EAGAIN once the send buffer fills — which would drop the client mid-frame
        # and hang it. Send blocking with a cap instead; a genuinely stuck client
        # trips the timeout and gets dropped.
        s = c["sock"]
        try:
            s.setblocking(True)
            s.settimeout(15)
            send_frame(s, t, data)
            return True
        except OSError:
            return False
        finally:
            try:
                s.settimeout(None)
                s.setblocking(False)
            except OSError:
                pass

    def drop(fd: int) -> None:
        c = clients.pop(fd, None)
        if not c:
            return
        try:
            sel.unregister(c["sock"])
        except KeyError:
            pass
        try:
            c["sock"].close()
        except OSError:
            pass

    def broadcast(data: bytes) -> None:
        for fd in list(clients):
            if clients[fd]["attached"] and not safe(clients[fd], S_DATA, data):
                drop(fd)

    def shutdown(text: str) -> None:
        # persist the tail of the pty ring to the log BEFORE unlinking the socket, so
        # a failed boot's reason survives (e.g. vere's own "pier is already running"
        # lock refusal). `run` reads this back to tell the user WHY it didn't come up
        # — relying on vere's message, not reinventing a lock check.
        try:
            with open(path + ".log", "a") as f:
                f.write(f"\n[daemon shutdown: {text}]\n---- last pty output ----\n")
                f.write(bytes(ring[-4000:]).decode("utf-8", "replace"))
                f.write("\n---- end ----\n")
        except OSError:
            pass
        for fd in list(clients):
            safe(clients[fd], S_EXIT, text.encode())
            drop(fd)
        try:
            os.unlink(path)
        except OSError:
            pass

    while True:
        for key, _ in sel.select(timeout=1.0):
            tag = key.data
            if tag == "master":
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""  # EIO when the child is gone
                if not data:
                    try:
                        _, st = os.waitpid(pid, 0)
                    except OSError:
                        st = 0
                    shutdown(f"child exited (status {st})")
                    return
                tee_write(data)  # durable, raw, before the ring gets to evict anything
                ring += data
                total += len(data)
                if len(ring) > RING_MAX:
                    del ring[:len(ring) - RING_MAX]
                broadcast(bytes(data))
            elif tag == "listen":
                conn, _ = listen.accept()
                conn.setblocking(False)
                clients[conn.fileno()] = {"sock": conn, "reader": FrameReader(), "attached": False}
                sel.register(conn, selectors.EVENT_READ, "client")
            else:
                fd = key.fileobj.fileno()
                c = clients.get(fd)
                if not c:
                    continue
                try:
                    chunk = key.fileobj.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    drop(fd)
                    continue
                for t, payload in c["reader"].feed(chunk):
                    if t == C_PING:
                        safe(c, S_PONG, f"{os.getpid()} {pid} {VERSION}".encode())
                    elif t == C_ATTACH:
                        # payload (optional): 8-byte big-endian cap on how much
                        # scrollback to replay. A terminal only wants the screen it is
                        # about to sit in front of, and shipping a multi-MB ring to it
                        # costs an ssh transfer plus a full terminal parse before the
                        # prompt is usable. No payload => an older client that wants the
                        # whole ring, so keep giving it that.
                        cap = (struct.unpack("!Q", payload[:8])[0]
                               if len(payload) >= 8 else len(ring))
                        safe(c, S_SNAPSHOT, bytes(ring[len(ring) - min(cap, len(ring)):]))
                        c["attached"] = True
                    elif t == C_CAPTURE:
                        safe(c, S_SNAPSHOT, bytes(ring))
                    elif t == C_CAPTURE_AT:
                        # payload: 8-byte big-endian watermark (a prior `total`). Return
                        # the current total + only ring bytes at/after that offset. If the
                        # watermark predates the ring (output outran RING_MAX), clamp to
                        # the ring start — same recoverable case the full capture hits.
                        frm = struct.unpack("!Q", payload[:8])[0] if len(payload) >= 8 else 0
                        base = total - len(ring)
                        start = min(max(0, frm - base), len(ring))
                        safe(c, S_SNAPSHOT_AT, struct.pack("!Q", total) + bytes(ring[start:]))
                    elif t == C_DATA:
                        try:
                            os.write(master, payload)
                        except OSError:
                            pass
                    elif t == C_WINCH and len(payload) == WINSZ.size:
                        try:
                            fcntl.ioctl(master, termios.TIOCSWINSZ, payload)
                        except OSError:
                            pass
                    elif t == C_KILL:
                        # urbit forkpty child is a session leader (forkpty setsid'd
                        # it), so signal the whole PROCESS GROUP — king + serf — not
                        # just the king. urbit may ignore/slow-walk SIGTERM, so wait a
                        # bounded grace then escalate to SIGKILL; never block forever on
                        # a waitpid (a non-exiting urbit would wedge the daemon, leaving
                        # the socket up but unanswered).
                        try:
                            os.killpg(pid, signal.SIGTERM)
                        except OSError:
                            pass
                        gone = False
                        for _ in range(80):  # ~8s grace for a clean snapshot
                            try:
                                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                                    gone = True
                                    break
                            except OSError:
                                gone = True
                                break
                            time.sleep(0.1)
                        if not gone:
                            try:
                                os.killpg(pid, signal.SIGKILL)
                                os.waitpid(pid, 0)
                            except OSError:
                                pass
                        shutdown("killed by client")
                        return
                    elif t == C_UPGRADE:
                        # in-place engine swap: re-exec THIS process (same PID) into the
                        # freshly-pushed tool, handing over the pty master, the listen
                        # socket, and the urbit child. urbit NEVER restarts — only the
                        # python image changes, so there is no boot, no lock dance, no
                        # downtime. Ring scrollback crosses via a sidecar file; attached
                        # clients are dropped (the listen socket survives the exec, so
                        # they reconnect against the new image). sys.argv[0] is this
                        # tool's path, whose bytes ensure_self just refreshed — so the
                        # exec runs the NEW code.
                        try:
                            with open(path + ".ring", "wb") as rf:
                                rf.write(bytes(ring))
                        except OSError:
                            pass
                        for cfd in list(clients):
                            drop(cfd)
                        try:
                            os.set_inheritable(master, True)
                            os.set_inheritable(listen.fileno(), True)
                            os.execv(sys.executable,
                                     [sys.executable, sys.argv[0], "__daemon", "--sock", path,
                                      "--adopt", f"{master},{pid},{listen.fileno()}"])
                        except OSError as e:
                            # exec failed: stay alive on the OLD code (the ship keeps
                            # running). The client's version poll sees no change and
                            # falls back to a full restart.
                            try:
                                os.unlink(path + ".ring")
                            except OSError:
                                pass
                            sys.stderr.write(f"engine: in-place upgrade exec failed: {e}\n")


def engine_daemon(sock: str, cmd: list[str], foreground: bool) -> None:
    if engine_ping(sock):
        die(f"daemon already running at {sock}")
    if os.path.exists(sock):
        os.unlink(sock)  # stale socket from a crashed daemon
    if not cmd:
        die("__daemon needs a command after --")
    # ALWAYS give the pty a non-zero window size. urbit's terminal renderer blits
    # the dojo to the screen at boot and BAILS on a 0x0 pty (crud %coup / "bailing
    # out") — which is exactly the headless case (booted by `up` with no tty). Use
    # the booting client's size if it has one, else a sane 24x80 default; attaching
    # clients later send their real size via C_WINCH.
    winsz = engine_winsize(1) if sys.stdout.isatty() else WINSZ.pack(24, 80, 0, 0)
    if not foreground:
        engine_daemonize(sock + ".log")
    engine_run_daemon(sock, cmd, winsz)


def attach_snapshot(ring: bytes) -> bytes:
    # trim a C_ATTACH snapshot to the last ATTACH_TAIL bytes, for a terminal to replay.
    # Runs on BOTH paths, and does real work on both: a daemon that understands the cap
    # has already cut the transfer down to size but cut it at a raw byte offset, and an
    # older daemon sends the whole ring. Either way this is where the replay is made
    # safe to blit. See ATTACH_SLACK for why the capped case still lands here.
    if len(ring) <= ATTACH_TAIL:
        return ring
    tail = ring[-ATTACH_TAIL:]
    # start on a line boundary: an arbitrary cut lands inside an escape sequence about
    # as often as not, and half a `\x1b[6144;1H` renders as visible garbage.
    cut = tail.find(b"\n")
    if cut >= 0:
        tail = tail[cut + 1:]
    # soft reset + attributes off: the skipped bytes may have set a scroll region, an
    # origin mode or an SGR run that this tail never closes, and inheriting half of one
    # paints the rest of the session wrong. The C_WINCH sent right after attach makes
    # dill repaint the prompt itself, so nothing here has to reconstruct it.
    return b"\x1b[!p\x1b[0m\r\n" + tail


EOT = b"\x04"


def engine_attach(sock: str, detach_byte: int) -> None:
    if not sys.stdin.isatty():
        die("__attach needs a tty on stdin")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
    except OSError:
        die(f"no daemon at {sock}")
    detach = bytes([detach_byte])
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    fr = FrameReader()

    def push_winch(*_):
        try:
            send_frame(s, C_WINCH, engine_winsize(1))
        except OSError:
            pass

    try:
        tty.setraw(fd)
        send_frame(s, C_ATTACH, struct.pack("!Q", ATTACH_TAIL + ATTACH_SLACK))
        push_winch()
        signal.signal(signal.SIGWINCH, push_winch)
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ, "in")
        sel.register(s, selectors.EVENT_READ, "sock")
        while True:
            for key, _ in sel.select():
                if key.data == "in":
                    data = os.read(fd, 4096)
                    if not data or detach in data:
                        os.write(1, b"\r\n[detached]\r\n")
                        return
                    if EOT in data:
                        # Ctrl-D is EOF to vere, and vere exits on it: the reflex key for
                        # leaving a terminal would shut the ship down.
                        data = data.replace(EOT, b"")
                        os.write(1, b"\r\n[Ctrl-D would shut the ship down: not sent. "
                                    b"Ctrl-\\ detaches; `exit` stops the ship]\r\n")
                        if not data:
                            continue
                    send_frame(s, C_DATA, data)
                else:
                    chunk = s.recv(65536)
                    if not chunk:
                        os.write(1, b"\r\n[connection closed]\r\n")
                        return
                    for t, payload in fr.feed(chunk):
                        if t == S_SNAPSHOT:
                            os.write(1, attach_snapshot(payload))
                        elif t == S_DATA:
                            os.write(1, payload)
                        elif t == S_EXIT:
                            os.write(1, b"\r\n[" + payload + b"]\r\n")
                            return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        s.close()


def engine_send(sock: str) -> None:
    if not sock_send(sock, sys.stdin.buffer.read()):
        die(f"no daemon at {sock}")


def sock_capture(sock: str) -> bytes:
    # the whole ring, or b"" if no daemon answers (a probe reads that as "down").
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
    except OSError:
        return b""
    try:
        send_frame(s, C_CAPTURE)
        fr = FrameReader()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return b""
            for t, payload in fr.feed(chunk):
                if t == S_SNAPSHOT:
                    return payload
    except OSError:
        return b""
    finally:
        s.close()


def sock_capture_at(sock: str, offset: int) -> tuple[int, bytes] | None:
    # (current total, ring bytes past `offset`), or None when the daemon does not
    # answer S_SNAPSHOT_AT — no daemon at all, or one predating C_CAPTURE_AT. Both
    # leave the caller to fall back to a whole-ring capture.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
    except OSError:
        return None
    s.settimeout(5.0)
    try:
        send_frame(s, C_CAPTURE_AT, struct.pack("!Q", offset & 0xFFFFFFFFFFFFFFFF))
        fr = FrameReader()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return None
            for t, payload in fr.feed(chunk):
                if t == S_SNAPSHOT_AT:
                    return (struct.unpack("!Q", payload[:8])[0], payload[8:])
    except OSError:  # socket.timeout is an OSError subclass
        return None
    finally:
        s.close()


def sock_send(sock: str, data: bytes) -> bool:
    # inject bytes into the pty. False when no daemon took them — a SILENT failure
    # here reads downstream as "the dojo refused the line", which sends people
    # hunting the line editor for what is really a dead socket.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
        send_frame(s, C_DATA, data)
        return True
    except OSError:
        return False
    finally:
        s.close()


def engine_capture(sock: str) -> None:
    out = sock_capture(sock)
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()


def engine_capture_at(sock: str, offset: int) -> None:
    # like engine_capture, but ask only for ring bytes past `offset` (a prior `total`
    # watermark). stdout is a 16-hex-digit `total` + "\n" header, then the raw tail.
    # A daemon predating C_CAPTURE_AT never replies S_SNAPSHOT_AT; on the recv timeout
    # fall back to a full capture under the "ffff…" sentinel header so the frontend
    # transparently degrades to whole-ring search (no behavior change pre-upgrade).
    got = sock_capture_at(sock, offset)
    if got is None:
        if not os.path.exists(sock):
            return  # no daemon => empty capture (probe reads this as "down")
        sys.stdout.write("f" * 16 + "\n")
        sys.stdout.flush()
        return engine_capture(sock)
    sys.stdout.write("%016x\n" % got[0])
    sys.stdout.flush()
    sys.stdout.buffer.write(got[1])
    sys.stdout.buffer.flush()


def engine_status(sock: str) -> None:
    pong = engine_ping(sock)
    sys.stdout.write(f"up {pong[0]} {pong[1]} {pong[2] or '?'}\n" if pong else "down\n")


def engine_kill(sock: str) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
        send_frame(s, C_KILL)
    except OSError:
        pass
    finally:
        s.close()


def engine_upgrade(sock: str) -> None:
    # ask the daemon to swap its code in place (re-exec the freshly-pushed tool while
    # keeping urbit + the pty alive). A pre-versioning daemon has no C_UPGRADE handler
    # and silently ignores the frame — the frontend detects that by polling the
    # reported version and falls back to a restart.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock)
        send_frame(s, C_UPGRADE)
    except OSError:
        pass
    finally:
        s.close()


def engine_pty_seek(sock: str, when: float) -> None:
    # print the byte offset of the last `.idx` record at or before `when`.
    #
    # This is what the index is FOR. A caller who wants "what was printed in the last two
    # seconds" should not have to cook megabytes to find out: seeking by time turns
    # dojo's slog window into a read of a few KB, independent of how big `.pty` has got.
    # Floors rather than rounds — the record at/before `when` covers it, the one after
    # starts too late and would clip the first line.
    try:
        with open(sock + ".idx", "rb") as f:
            recs = f.read()
    except OSError:
        sys.stdout.write("0\n")
        return
    best = 0
    for i in range(0, len(recs) - IDX.size + 1, IDX.size):
        off, t = IDX.unpack_from(recs, i)
        if t <= when:
            best = off
        else:
            break
    sys.stdout.write(f"{best}\n")


def engine_pty_read(sock: str, offset: int) -> None:
    # ship `.pty` bytes past `offset`, plus the `.idx` records covering them.
    #
    # This talks to the FILES, not the daemon: the tee's whole point is that the console
    # survives the process, so a slog from a ship that crashed on the way up is exactly
    # the one worth reading. No socket, no frames, no liveness requirement.
    #
    # stdout is a text header line — `<inode> <size> <nrecs>` — then `nrecs` decimal
    # `<offset> <time>` lines, then the raw pty tail. The index goes as TEXT on purpose:
    # the frontend decodes this pipe with errors="replace" (see local_exec), which would
    # quietly corrupt the arbitrary bytes of a packed f64.
    #
    # inode lets the frontend notice rotation: same inode + larger size = plain growth;
    # a changed inode means `.pty` rotated under it and its watermark is meaningless.
    try:
        st = os.stat(sock + ".pty")
    except OSError:
        sys.stdout.write("0 0 0\n")  # no tee (pre-0.0.6 daemon, or never booted)
        return
    recs = b""
    try:
        with open(sock + ".idx", "rb") as f:
            recs = f.read()
    except OSError:
        pass
    # Records at/after `offset`, PLUS the last one before it: the chunk's leading bytes
    # were written at that earlier record's time, and without it they get attributed to
    # the next record and stamped LATER than they happened. That drift is invisible in a
    # rebuild (which starts at 0 and has every record) and shows up only as an
    # incremental cook disagreeing with a rebuilt one.
    prev: str | None = None
    lines: list[str] = []
    for i in range(0, len(recs) - IDX.size + 1, IDX.size):
        off, when = IDX.unpack_from(recs, i)
        if off < offset:
            prev = f"{off} {when:.6f}"
        else:
            lines.append(f"{off} {when:.6f}")
    if prev is not None:
        lines.insert(0, prev)
    data = b""
    if offset < st.st_size:
        try:
            with open(sock + ".pty", "rb") as f:
                f.seek(offset)
                data = f.read()
        except OSError:
            pass
    out = sys.stdout.buffer
    out.write(f"{st.st_ino} {st.st_size} {len(lines)}\n".encode())
    for l in lines:
        out.write(l.encode() + b"\n")
    out.write(data)
    out.flush()


ENGINE_SUBS = {"__daemon", "__attach", "__send", "__capture", "__capture-at",
               "__status", "__kill", "__upgrade", "__pty-read", "__pty-seek"}


def engine_main(argv: list[str]) -> None:
    # argv[0] = "__<sub>"; the rest carry "--sock <path>" plus per-sub extras.
    sub, rest = argv[0], argv[1:]
    if "--sock" not in rest:
        die(f"{sub}: --sock <path> required")
    i = rest.index("--sock")
    sock = rest[i + 1]
    rest = rest[:i] + rest[i + 2:]
    if sub == "__daemon":
        # adopt mode (post in-place-upgrade exec): inherit the pty master, urbit child,
        # and listen socket from the previous image instead of booting fresh.
        if "--adopt" in rest:
            spec = rest[rest.index("--adopt") + 1]
            master_s, pid_s, listen_s = spec.split(",")
            return engine_adopt(sock, int(master_s), int(pid_s), int(listen_s))
        fg = "--foreground" in rest
        rest = [a for a in rest if a != "--foreground"]
        cmd = rest[1:] if (rest and rest[0] == "--") else rest
        return engine_daemon(sock, cmd, fg)
    if sub == "__attach":
        detach = 0x1c
        if "--detach" in rest:
            detach = int(rest[rest.index("--detach") + 1], 0)
        return engine_attach(sock, detach)
    if sub == "__send":
        return engine_send(sock)
    if sub == "__pty-read":
        return engine_pty_read(sock, int(rest[0]) if rest else 0)
    if sub == "__pty-seek":
        return engine_pty_seek(sock, float(rest[0]) if rest else 0.0)
    if sub == "__capture":
        return engine_capture(sock)
    if sub == "__capture-at":
        return engine_capture_at(sock, int(rest[0]) if rest else 0)
    if sub == "__status":
        return engine_status(sock)
    if sub == "__kill":
        return engine_kill(sock)
    if sub == "__upgrade":
        return engine_upgrade(sock)
    die(f"unknown engine subcommand {sub!r}")


# ── dispatch (CLI mode only) ──────────────────────────────────────────────────
def main() -> None:
    argv = sys.argv[1:]
    first = argv[0] if argv else None
    # bare `admiral` → status over every ship in the config.
    if first is None:
        status = next(c for c in COMMANDS if c.name == "status")
        return status.run(Parsed(cmd=status, selector=None, cfg=load_config(), flags={}, pos=[]))
    # engine mode: hidden host-local pty subcommands own argv[0]. No config load.
    if first in ENGINE_SUBS:
        return engine_main(argv)
    if first == "help":
        return cmd_help(argv[1] if len(argv) > 1 else None)
    if first in ("-h", "--help"):
        return help_menu()
    # hidden completion subcommands (not in the menu): they own argv[0].
    if first == "__complete":
        return cmd_complete(argv[1:])
    if first == "enrich":
        sys.stdout.write(cmd_enrich(argv[1:]))
        return
    if first == "unenrich":
        sys.stdout.write(cmd_unenrich(argv[1:]))
        return
    # config wizards (top-level verbs, no ship selector).
    if first == "init":
        return cmd_init(argv[1:])

    selector = argv[0]
    if any(c.name == selector for c in COMMANDS):
        raise RuntimeError(f"`{selector}` needs a ship first: {INVOKE} <ship> {' '.join(argv)}")
    # no command after the selector → status (the default command).
    subcmd = argv[1] if len(argv) > 1 else "status"
    args = argv[2:]
    if subcmd == "help":
        return cmd_help(args[0] if args else None)
    cmd = next((c for c in COMMANDS if c.name == subcmd), None)
    if not cmd:
        subs = "|".join(c.name for c in COMMANDS)
        raise RuntimeError(f'Unknown subcommand "{subcmd}" ({subs})  —  try: {INVOKE} help')
    cfg = load_config()
    flags, pos = parse_args(cmd, args)
    return cmd.run(Parsed(cmd=cmd, selector=selector, cfg=cfg, flags=flags, pos=pos))


def _interrupt(*_: Any) -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    # SIGTERM unwinds like Ctrl-C, so a killed dojo drive aborts its command on the
    # way out instead of leaving it running for the next caller.
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        sys.exit(130)
    except Exception as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(1)
