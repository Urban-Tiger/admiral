#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
#
# admiral's test suite, run against a live ship from the nearest admiral.toml
# (found by walking up from the current directory, like admiral itself).
#
#   uv run tests/run.py <ship> [--group cli|dojo|commit|attach|harness] [--case <name>]
#
# A fake ship is the right target: the suite nukes nothing, but it leaves dojo
# variables, desks %admiral-test and %admiral-yes, and a stream of aborted evals.
# The commit group needs the fixture desk in admiral.toml, installed on the ship:
#
#   [[desks]]
#   name = "admiral-test"
#   src = "tests/desk"
#
#   ./admiral.py <ship> dojo '|new-desk %admiral-test, =hard &, =gall &'
#   ./admiral.py <ship> dojo '|mount %admiral-test'
#   ./admiral.py <ship> commit admiral-test
#   ./admiral.py <ship> dojo '|install our %admiral-test'
#
# Cases that play "another driver" type raw keys at the ship's socket, so they need a
# local ship. One PASS/FAIL line per case; exits nonzero if any failed.

import argparse
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIRAL = str(REPO / "admiral.py")
HARNESS = str(REPO / "harness.py")
sys.path.insert(0, str(REPO))
import admiral as adm  # noqa: E402  (the config walk-up, socket paths)
DESK = REPO / "tests" / "desk"
PROBE_AGENT = DESK / "app" / "admiral-probe.hoon"

CASES = []


def case(group, name):
    def deco(fn):
        CASES.append((group, name, fn))
        return fn
    return deco


class Check:
    def __init__(self):
        self.errs = []

    def that(self, ok, msg):
        if not ok:
            self.errs.append(msg)


def admiral(*args, timeout=120, cwd=REPO):
    t0 = time.monotonic()
    r = subprocess.run([ADMIRAL, *args], capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip(), time.monotonic() - t0


def dojo(ship, command, *flags, timeout=120):
    return admiral(ship, "dojo", *flags, "--", command, timeout=timeout)


def sock(ship):
    # the ship's socket, from whichever admiral.toml the walk-up from here finds
    cfg = adm.load_config()
    entry = adm.resolve_ship(ship, cfg)
    host = adm.host_for(entry, cfg)
    if host.ssh:
        raise RuntimeError(f"{ship} is remote; this case types at a local socket")
    return adm.sock_path(entry, host)


def type_raw(ship, keys):
    # another driver's keystrokes, straight into the pty
    subprocess.run([sys.executable, ADMIRAL, "__send", "--sock", sock(ship)],
                   input=keys.encode(), check=True)


def lines(out):
    return out.split("\n")


# ── cli: no ship needed ───────────────────────────────────────────────────────
@case("cli", "no-first-or-all")
def _(ship, c):
    for word in ("first", "all"):
        rc, out, err, _ = admiral(word, "status")
        c.that(rc != 0, f"`{word} status` succeeded")
        c.that(f'named "{word}"' in err, f"`{word}`: {err!r}")


@case("cli", "command-needs-ship")
def _(ship, c):
    rc, out, err, _ = admiral("dojo", "(add 2 2)")
    c.that(rc != 0, "a command without a ship succeeded")
    c.that("needs a ship first" in err, f"{err!r}")


@case("cli", "bare-is-help-status-is-all")
def _(ship, c):
    rc, out, err, _ = admiral()
    c.that(rc == 0 and "Commands" in err and ship in err, f"bare admiral is not the help menu: {rc} {err[:200]!r}")
    rc, out, err, _ = admiral("status")
    c.that(ship in out + err, f"`status` without a ship did not list {ship}: {(out + err)[:200]!r}")


@case("cli", "renamed-commands")
def _(ship, c):
    for old in ("upgrade", "next"):
        rc, out, err, _ = admiral(ship, old)
        c.that(rc != 0, f"`{old}` still runs")
    for new in ("engine-upgrade", "vere-upgrade"):
        rc, out, err, _ = admiral("help", new)
        c.that(rc == 0 and new in err, f"help {new}: {rc} {err[:200]!r}")


@case("cli", "help-every-topic")
def _(ship, c):
    rc, out, err, _ = admiral("help")
    c.that(rc == 0, f"help: exit {rc}")
    menu = lines(err)
    table = menu[menu.index("Commands") + 1:next(i for i, l in enumerate(menu) if l.startswith("Config:"))]
    names = [n for l in table if l.strip() and not l.lstrip().startswith("./")
             for n in l.split()[0].split("/")]
    c.that(len(names) > 15, f"read {len(names)} commands off the menu: {names}")
    for name in names:
        rc, out, err, _ = admiral("help", name)
        c.that(rc == 0, f"help {name}: exit {rc}: {err[:200]!r}")
        c.that(f"admiral.py {name}" in err, f"help {name} has no header: {err[:200]!r}")
        # a command line can't wrap; everything else fits a terminal
        wide = [l for l in lines(err) if len(l) > 80 and not l.startswith(("Usage:", "  ./admiral.py"))]
        c.that(not wide, f"help {name}: lines over 80 columns: {wide[:2]}")
    for word in ("first", "all"):
        c.that(f"| {word}" not in err and f"{word} status" not in err, f"help still mentions {word}")


@case("cli", "help-main-fits")
def _(ship, c):
    rc, out, err, _ = admiral("help")
    wide = [l for l in lines(err) if len(l) > 80]
    c.that(not wide, f"main help lines over 80 columns: {wide[:3]}")
    c.that("Config:" in err and "Ships" in err and ship in err, "no config block naming the ship")


@case("cli", "help-unknown-is-one-line")
def _(ship, c):
    rc, out, err, _ = admiral("help", "frobnicate")
    c.that(rc != 0, "unknown help topic exited 0")
    c.that(len(lines(err)) == 1, f"unknown topic printed {len(lines(err))} lines")


@case("cli", "socket-path-too-long")
def _(ship, c):
    with tempfile.TemporaryDirectory() as d:
        pier = "p" * 120
        Path(d, "admiral.toml").write_text(
            f'[ships.zod]\nhttp = "http://localhost:1"\npier = {{ path = "{pier}", port = 1 }}\n')
        rc, out, err, _ = admiral("zod", "status", cwd=d)
        c.that(rc != 0, "a 120-byte pier path passed")
        c.that("socket path is" in err, f"no socket-length error: {err[-300:]!r}")


# ── dojo ──────────────────────────────────────────────────────────────────────
@case("dojo", "expr")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "(add 2 2)")
    c.that(rc == 0, f"exit {rc}: {err[-200:]}")
    c.that(out == "4", f"want '4', got {out!r}")
    c.that(dt < 3.0, f"took {dt:.1f}s")


@case("dojo", "expr-noun")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "[1 2 3]")
    c.that(out == "[1 2 3]", f"want '[1 2 3]', got {out!r}")


@case("dojo", "slog-and-result")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "~&  %hello-slog  (add 2 3)")
    c.that(rc == 0, f"exit {rc}: {err[-200:]}")
    c.that("%hello-slog" in out, f"slog missing from {out!r}")
    c.that("5" in lines(out), f"result missing from {out!r}")


@case("dojo", "thread-slow")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "-time ~s3")
    c.that(rc == 0, f"exit {rc}: {err[-200:]}")
    c.that(dt >= 3.0, f"returned in {dt:.1f}s, before the thread finished")
    c.that(any(l.startswith("~s3..") for l in lines(out)), f"thread result missing from {out!r}")
    c.that(dt < 12.0, f"took {dt:.1f}s")


@case("dojo", "poke-ack")
def _(ship, c):
    rc, out, err, dt = dojo(ship, f"|hi ~{ship}")
    c.that(rc == 0, f"exit {rc}: {err[-200:]}")
    c.that(">=" in lines(out), f"poke ack missing from {out!r}")


@case("dojo", "refused-line")
def _(ship, c):
    rc, out, err, dt = dojo(ship, ".^(@ %gy /=ook=)")
    c.that(rc != 0, "refused line reported success")
    c.that("did NOT run" in err, f"no refusal report in {err[-300:]!r}")
    c.that(dt < 6.0, f"took {dt:.1f}s to notice")


@case("dojo", "dropped-keystrokes")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "))))")
    c.that(rc != 0, "fully-dropped input reported success")
    c.that(dt < 12.0, f"took {dt:.1f}s to notice")


@case("dojo", "long-line")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "(crip (reap 300 'x'))")
    c.that(rc == 0, f"exit {rc}: {err[-200:]}")
    c.that("x" * 300 in out.replace("\n", ""), "300-char run not intact")


@case("dojo", "long-input-at-the-limit")
def _(ship, c):
    command = '(lent "' + "a" * 991 + '")'  # 1000 chars
    rc, out, err, dt = dojo(ship, command)
    c.that(rc == 0, f"a {len(command)}-char command failed: {err[-300:]}")
    c.that(out.replace(".", "") == "991", f"want 991, got {out!r}")


@case("dojo", "long-input-refused")
def _(ship, c):
    rc, out, err, dt = dojo(ship, '(lent "' + "a" * 992 + '")')
    c.that(rc != 0, "a 1001-char command was typed")
    c.that("1001 chars" in err, f"no length refusal: {err[-300:]!r}")
    c.that(dt < 1.5, f"refusal took {dt:.1f}s; it should not touch the ship")


@case("dojo", "newline-refused")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "(add 1\n1)")
    c.that(rc != 0, "a command with a newline was typed")
    c.that("newline" in err, f"no newline refusal: {err[-300:]!r}")
    c.that(dt < 1.5, f"refusal took {dt:.1f}s; it should not touch the ship")


@case("dojo", "underscore-path-hint")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "=x /lib/a_b/hoon")
    c.that(rc != 0, "an underscore path literal succeeded")
    c.that("(stab" in err, f"no stab hint: {err[-300:]!r}")
    rc, out, err, dt = dojo(ship, "(add 4 4)")
    c.that(out == "8", f"next command: want 8, got {out!r} {err[-200:]}")


@case("dojo", "back-to-back")
def _(ship, c):
    for i in range(3):
        rc, out, err, dt = dojo(ship, f"(add {i} {i})")
        c.that(out == str(i + i), f"run {i}: want {i + i}, got {out!r}")


@case("dojo", "concurrent-busy")
def _(ship, c):
    # a second drive arriving mid-thread waits for it, then runs
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("first", dojo(ship, "-time ~s2")))
    t.start()
    time.sleep(0.6)
    rc, out, err, dt = dojo(ship, "(add 20 22)")
    t.join()
    c.that(rc == 0, f"second drive: exit {rc}: {err[-200:]}")
    c.that("42" in lines(out), f"second drive: want 42, got {out!r}")
    c.that(res["first"][0] == 0, f"first drive: {res['first'][2][-200:]}")


@case("dojo", "timeout-aborts-own-thread")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "-time ~s45", "-t", "4")
    c.that(rc != 0, "timed-out thread reported success")
    c.that("TIMEOUT" in err and "aborted" in err, f"no abort in {err[-300:]!r}")
    rc, out, err, dt = dojo(ship, "(add 1 1)")
    c.that(out == "2", f"next command: want 2, got {out!r} {err[-200:]}")
    c.that("aborted" not in err, f"the next command found the thread still running: {err!r}")
    c.that(dt < 3.0, f"next command took {dt:.1f}s")


@case("dojo", "timeout-interrupts-own-computation")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "=|(i=@ |-(?:(=(i 2.000.000.000) i $(i +(i)))))", "-t", "4")
    c.that(rc != 0, "timed-out computation reported success")
    c.that("TIMEOUT" in err and "aborted" in err, f"no abort in {err[:300]!r}")
    rc, out, err, dt = dojo(ship, "(add 1 2)")
    c.that(out == "3", f"next command: want 3, got {out!r} {err[-300:]}")


@case("dojo", "open-entry-fails-fast")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "=x " + "'" * 3)
    c.that(rc != 0, "an unclosed entry reported success")
    c.that("open entry" in err, f"{err[-300:]!r}")
    c.that(dt < 4.0, f"took {dt:.1f}s to notice")
    rc, out, err, dt = dojo(ship, "(add 6 6)")
    c.that(out == "12", f"next command: want 12, got {out!r}")


@case("dojo", "question-cancelled")
def _(ship, c):
    dojo(ship, "|new-desk %admiral-yes, =hard &")
    rc, out, err, dt = dojo(ship, "|new-desk %admiral-yes")
    c.that(rc != 0, "an unanswered question reported success")
    c.that("--yes" in err and "=hard &" in err, f"no hint: {err[-300:]!r}")
    rc, out, err, dt = dojo(ship, "(add 3 3)")
    c.that(out == "6", f"next command: want 6, got {out!r} {err[-200:]}")
    c.that("cancelled" not in err, "the question was still pending for the next command")


@case("dojo", "question-yes")
def _(ship, c):
    dojo(ship, "|new-desk %admiral-yes, =hard &")
    rc, out, err, dt = dojo(ship, "|new-desk %admiral-yes", "--yes")
    c.that(rc == 0, f"exit {rc}: {err[-300:]}")
    c.that("answered y" in err, f"no answer logged: {err[-300:]!r}")
    c.that(">=" in lines(out), f"the answered generator did not run: {out!r}")
    rc, out, err, dt = dojo(ship, "(add 5 5)")
    c.that(out == "10", f"next command: want 10, got {out!r}")


@case("dojo", "left-open-entry")
def _(ship, c):
    type_raw(ship, "=x '''\r")
    time.sleep(0.5)
    rc, out, err, dt = dojo(ship, "(add 7 7)")
    c.that(out == "14", f"want 14, got {out!r} {err[-300:]}")
    c.that("backed out" in err, f"not reported: {err!r}")


@case("dojo", "left-half-typed-line")
def _(ship, c):
    type_raw(ship, "(add 9")
    time.sleep(0.3)
    rc, out, err, dt = dojo(ship, "(add 8 8)")
    c.that(out == "16", f"want 16, got {out!r} {err[-300:]}")


@case("dojo", "left-question")
def _(ship, c):
    dojo(ship, "|new-desk %admiral-yes, =hard &")
    type_raw(ship, "|new-desk %admiral-yes\r")
    time.sleep(2)
    rc, out, err, dt = dojo(ship, "(add 9 9)")
    c.that(out == "18", f"want 18, got {out!r} {err[-300:]}")
    c.that("cancelled a dojo question" in err, f"not reported: {err!r}")


@case("dojo", "left-running-thread")
def _(ship, c):
    type_raw(ship, "-time ~s60\r")
    time.sleep(0.5)
    rc, out, err, dt = dojo(ship, "(add 10 10)", "-t", "3")
    c.that(out == "20", f"want 20, got {out!r} {err[-300:]}")
    c.that("aborted a running thread" in err, f"not reported: {err!r}")
    c.that(dt < 12.0, f"took {dt:.1f}s")


@case("dojo", "left-computation")
def _(ship, c):
    type_raw(ship, "=|(i=@ |-(?:(=(i 2.000.000.000) i $(i +(i)))))\r")
    time.sleep(1.0)
    rc, out, err, dt = dojo(ship, "(add 11 11)", "-t", "3")
    c.that(out == "22", f"want 22, got {out!r} {err[-300:]}")
    c.that("aborted an event still computing" in err, f"not reported: {err!r}")


@case("dojo", "ctrl-c-aborts")
def _(ship, c):
    for sig in (signal.SIGINT, signal.SIGTERM):
        p = subprocess.Popen([ADMIRAL, ship, "dojo", "--", "-time ~s60"], cwd=REPO,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(3)
        p.send_signal(sig)
        p.wait(timeout=30)
        c.that(p.returncode == 130, f"{sig.name}: exit {p.returncode}")
        rc, out, err, dt = dojo(ship, "(add 12 12)")
        c.that(out == "24", f"after {sig.name}: want 24, got {out!r}")
        c.that("aborted" not in err, f"after {sig.name}: the thread was still running: {err!r}")


@case("dojo", "output-truncated")
def _(ship, c):
    rc, out, err, dt = dojo(ship, "(lent (turn (gulf 1 600) |=(a=@ ~&(a a))))", "-t", "60")
    c.that(rc == 0, f"exit {rc}: {err[-200:]}")
    c.that(len(lines(out)) == 401, f"want 400 lines + a note, got {len(lines(out))}")
    c.that("more lines" in lines(out)[-1], f"no truncation note: {lines(out)[-1]!r}")


# ── commit ────────────────────────────────────────────────────────────────────
def commit(ship):
    return admiral(ship, "commit", "admiral-test", timeout=400)


def verdict(err):
    return lines(err)[-1]


def exists(ship, path):
    rc, out, err, _ = dojo(ship, f".^(? %cu /=admiral-test=/{path})")
    return out.split("\n")[-1] == "%.y"


class Restore:
    # put a fixture file back the way it was, and the desk with it
    def __init__(self, ship, path):
        self.ship, self.path = ship, path
        self.text = path.read_text() if path.exists() else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.text is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(self.text)
        commit(self.ship)


@case("commit", "no-changes")
def _(ship, c):
    commit(ship)
    rc, out, err, dt = commit(ship)
    c.that(rc == 0, f"exit {rc}: {err[-300:]}")
    c.that(verdict(err) == "commit %admiral-test: no changes", f"verdict: {verdict(err)!r}")


@case("commit", "delete-sticks-without-remount")
def _(ship, c):
    probe = DESK / "lib" / "admiral-delete-probe.hoon"
    with Restore(ship, probe):
        probe.write_text(f"{int(time.time())}\n")
        rc, out, err, dt = commit(ship)
        c.that("(+1)" in verdict(err), f"add verdict: {verdict(err)!r}")
        c.that(exists(ship, "lib/admiral-delete-probe/hoon"), "the file never reached clay")
        probe.unlink()
        rc, out, err, dt = commit(ship)
        c.that(rc == 0, f"exit {rc}: {err[-300:]}")
        c.that("remounting" in err, f"no remount: {err[-400:]!r}")
        c.that("(-1)" in verdict(err), f"delete verdict: {verdict(err)!r}")
        c.that(not exists(ship, "lib/admiral-delete-probe/hoon"), "the deleted file is still in clay")


@case("commit", "reload-verdict")
def _(ship, c):
    with Restore(ship, PROBE_AGENT):
        PROBE_AGENT.write_text(PROBE_AGENT.read_text().replace("!>(~)", "!>(0)"))
        rc, out, err, dt = commit(ship)
        c.that(rc == 0, f"exit {rc}: {err[-300:]}")
        c.that("(:1), reloaded %admiral-probe" in verdict(err), f"verdict: {verdict(err)!r}")


@case("commit", "comment-only-verdict")
def _(ship, c):
    with Restore(ship, PROBE_AGENT):
        PROBE_AGENT.write_text(PROBE_AGENT.read_text() + f"::  {time.time()}\n")
        rc, out, err, dt = commit(ship)
        c.that("(:1), nothing reloaded" in verdict(err), f"verdict: {verdict(err)!r}")


@case("commit", "build-error-verdict")
def _(ship, c):
    with Restore(ship, PROBE_AGENT):
        PROBE_AGENT.write_text(PROBE_AGENT.read_text().replace("!>(~)", "!>(~"))
        rc, out, err, dt = commit(ship)
        c.that(rc != 0, "a syntax error committed with exit 0")
        c.that("FAILED" in err and "syntax error" in err, f"verdict: {err[-300:]!r}")


@case("commit", "suspended-warning")
def _(ship, c):
    probe = DESK / "lib" / "admiral-suspend-probe.hoon"
    dojo(ship, "|suspend %admiral-test", "-t", "180")
    try:
        with Restore(ship, probe):
            probe.write_text(f"{int(time.time())}\n")
            rc, out, err, dt = commit(ship)
            c.that("suspended" in err and "|revive %admiral-test" in err, f"no warning: {err[-300:]!r}")
    finally:
        rc, out, err, dt = dojo(ship, "|revive %admiral-test", "-t", "180")
        c.that(rc == 0, f"|revive: {err[-300:]}")


# ── attach ────────────────────────────────────────────────────────────────────
@case("attach", "ctrl-d-not-sent")
def _(ship, c):
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(REPO)
        os.execv(ADMIRAL, [ADMIRAL, ship, "attach"])
    seen = b""

    def read_for(secs):
        nonlocal seen
        end = time.monotonic() + secs
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    return

    read_for(3)
    os.write(fd, b"\x04")
    read_for(2)
    os.write(fd, b"\x1c")
    read_for(2)
    os.waitpid(pid, 0)
    c.that(b"would shut the ship down" in seen, "no Ctrl-D notice")
    c.that(b"[detached]" in seen, "Ctrl-\\ did not detach")
    rc, out, err, dt = dojo(ship, "(add 13 13)")
    c.that(out == "26", f"the ship did not survive Ctrl-D: {out!r} {err[-200:]}")


# ── harness: with_ship's helpers, in a real browser ───────────────────────────
HARNESS_DRIVE = REPO / "tests" / "harness_drive.py"
_harness = {}


def harness(ship, name):
    # one browser run serves every case in the group
    if not _harness:
        t0 = time.monotonic()
        r = subprocess.run([HARNESS, ship, str(HARNESS_DRIVE)], capture_output=True, text=True,
                           timeout=300, cwd=REPO)
        rc, out, err = r.returncode, r.stdout.strip(), r.stderr.strip()
        for line in lines(out):
            verdict, _, rest = line.partition(" ")
            check, _, why = rest.partition(": ")
            _harness[check] = why if verdict == "FAIL" else None
        _harness.setdefault("(run)", None if rc == 0 else err[-400:])
    if name not in _harness:
        return f"never reported: {_harness.get('(run)')}"
    return _harness[name]


for _check in ("roles", "axe", "console", "window", "crash_caught", "crash_absolved", "crash_unwatched"):
    @case("harness", _check.replace("_", "-"))
    def _(ship, c, _check=_check):
        why = harness(ship, _check)
        c.that(why is None, why)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ship")
    ap.add_argument("--group", default=None)
    ap.add_argument("--case", default=None)
    args = ap.parse_args()

    failed = 0
    for group, name, fn in CASES:
        if (args.group and args.group != group) or (args.case and args.case != name):
            continue
        c = Check()
        t0 = time.monotonic()
        try:
            fn(args.ship, c)
        except Exception as e:
            c.errs.append(f"harness error: {e!r}")
        dt = time.monotonic() - t0
        if c.errs:
            failed += 1
            print(f"FAIL  {group:7s} {name:36s} {dt:5.1f}s", flush=True)
            for e in c.errs:
                print(f"        {e}", flush=True)
        else:
            print(f"pass  {group:7s} {name:36s} {dt:5.1f}s", flush=True)
    print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
