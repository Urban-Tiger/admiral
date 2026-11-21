#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte"]
# ///
#
# admiral's unit tests: the pure functions, no ship. Run with `uv run tests/unit.py`
# (or `tests/unit.py`); CI runs them on every push. The live suite is tests/run.py.

import io
import os
import re
import struct
import sys
import tempfile
import time
import tomllib
import socket
import threading
import http.client
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admiral as A  # noqa: E402
import harness as H  # noqa: E402


RAW = Path(__file__).resolve().parent / "raw"  # console bytes saved from real ships


def cmd(flags=(), positionals=()):
    return A.Cmd(name="x", summary="", flags=list(flags), positionals=list(positionals), run=lambda a: None)


class FakeHost(A.Host):
    # a Host whose exec answers from a canned table instead of running anything
    def __init__(self, answers=None, ssh="fake@box"):
        super().__init__("fake", "/root", ssh)
        self.answers = answers or {}
        self.calls = []

    def exec(self, file, args, **kw):
        self.calls.append((file, args))
        out = self.answers.get(file)
        if isinstance(out, Exception):
            raise out
        return out or ""


class Persist(unittest.TestCase):
    def test_unit_text_runs_the_same_daemon(self):
        h = FakeHost()
        ship = A.Ship("zod", A.PierConfig(path="piers/zod", port=8080, ames=31337, loom=31))
        text = A.unit_text(ship, h)
        sock = A.sock_path(ship, h)
        self.assertIn("WantedBy=default.target", text)
        self.assertIn(f"RequiresMountsFor={A.pier_dir(ship, h)}", text)
        self.assertIn(f"__daemon --foreground --sock {sock} -- " + " ".join(A.run_argv(ship, h)), text)
        self.assertIn(f"ExecStop=-/usr/bin/env python3 {A.remote_self(h)} __kill --sock {sock}", text)
        self.assertEqual(A.unit_name(ship), "admiral-zod.service")

    def test_unit_enabled_needs_systemctl(self):
        # host_has caches per host name, and every FakeHost is "fake"
        A._has_tool.clear()
        h = FakeHost({"which": ""})
        self.assertFalse(A.unit_enabled(h, A.Ship("zod", A.PierConfig())))
        A._has_tool.clear()
        h = FakeHost({"which": "/usr/bin/systemctl", "systemctl": "enabled\n"})
        self.assertTrue(A.unit_enabled(h, A.Ship("zod", A.PierConfig())))


class ParseArgs(unittest.TestCase):
    def setUp(self):
        self.c = cmd([A.Flag("-t", "int", "", ph="<secs>"), A.Flag("--yes", "bool", ""),
                      A.Flag("--since", "str", "")],
                     [A.Pos("CMD", True, "")])

    def test_kinds(self):
        flags, pos = A.parse_args(self.c, ["-t", "5", "--yes", "--since", "2h", "(add 2 2)"])
        self.assertEqual(flags, {"-t": 5, "--yes": True, "--since": "2h"})
        self.assertEqual(pos, ["(add 2 2)"])

    def test_flag_equals_value(self):
        flags, pos = A.parse_args(self.c, ["-t=5", "--since=2h", "x"])
        self.assertEqual(flags, {"-t": 5, "--since": "2h"})
        with self.assertRaises(A.UsageError):
            A.parse_args(self.c, ["--yes=1", "x"])

    def test_double_dash_ends_flags(self):
        flags, pos = A.parse_args(self.c, ["--", "-time ~s1"])
        self.assertEqual((flags, pos), ({}, ["-time ~s1"]))

    def test_errors(self):
        for argv, msg in ([["--nope", "x"], "Unknown flag"], [[], "missing required"],
                          [["a", "b"], "unexpected argument"], [["-t", "0", "x"], "positive"],
                          [["-t"], "needs a value"]):
            with self.assertRaises(A.UsageError, msg=argv) as cm:
                A.parse_args(self.c, argv)
            self.assertIn(msg, str(cm.exception))

    def test_variadic(self):
        c = cmd([], [A.Pos("file", True, "", variadic=True)])
        self.assertEqual(A.parse_args(c, ["a", "b", "c"])[1], ["a", "b", "c"])

    def test_check_dojo_input(self):
        with self.assertRaises(RuntimeError):
            A.check_dojo_input("a\nb")
        with self.assertRaises(RuntimeError):
            A.check_dojo_input("x" * (A.DOJO_INPUT_MAX + 1))
        A.check_dojo_input("x" * A.DOJO_INPUT_MAX)


class CommitVerdict(unittest.TestCase):
    desk = A.Desk("admiral-test", ".")

    def test_no_changes(self):
        self.assertEqual(A.commit_verdict(self.desk, ">=\n"), (True, "commit %admiral-test: no changes", []))

    def test_counts_and_reload(self):
        out = "+ /lib/x/hoon\n: /app/y/hoon\n- /mar/z/hoon\ngall: reloading %y\ngall: reloading %y\n>="
        ok, line, reloaded = A.commit_verdict(self.desk, out)
        self.assertTrue(ok)
        self.assertEqual(line, "commit %admiral-test: 3 files (+1 -1 :1), reloaded %y")
        self.assertEqual(reloaded, ["y"])

    def test_nothing_reloaded(self):
        self.assertEqual(A.commit_verdict(self.desk, ": /lib/x/hoon")[1],
                         "commit %admiral-test: 1 file (:1), nothing reloaded")

    def test_compiler_line_beats_crud(self):
        out = "crud: %into event failed\nsyntax error at [7 19] in /app/x/hoon\nbail: meme"
        ok, line, _ = A.commit_verdict(self.desk, out)
        self.assertFalse(ok)
        self.assertTrue(line.startswith("commit %admiral-test FAILED: syntax error"), line)


class DojoSettle(unittest.TestCase):
    # dojo_settle and the drive's keys, against a dojo whose verdicts are canned: no
    # ship, no sleeps. Records every key sent.
    def setUp(self):
        self.sent, self.verdicts = [], []
        self.clock = [0.0]
        patches = {
            "dojo_probe": lambda h, s, t: (self.verdicts.pop(0) if len(self.verdicts) > 1
                                           else self.verdicts[0], 0, ""),
            "send_data": lambda h, s, d: self.sent.append(d),
            "ring_mark": lambda h, s: 0,
            "clear_input": lambda h, s: None,
        }
        self.saved = {k: getattr(A, k) for k in patches}
        for k, v in patches.items():
            setattr(A, k, v)
        self.saved_time = A.time
        clock = self.clock

        class FakeTime:
            @staticmethod
            def monotonic():
                return clock[0]

            @staticmethod
            def sleep(s):
                clock[0] += s
        A.time = FakeTime
        A._interrupted.clear()
        self.ship = A.Ship("zod", A.PierConfig())

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(A, k, v)
        A.time = self.saved_time
        A._interrupted.clear()

    def settle(self, interrupt=False):
        logs = []
        A.dojo_settle(None, self.ship, 10.0, 10, logs.append, interrupt)
        return logs

    def test_idle_returns(self):
        self.verdicts = ["idle"]
        self.settle()
        self.assertEqual(self.sent, [])

    def test_mute_is_waited_out(self):
        self.verdicts = ["mute"] * 5 + ["idle"]
        self.settle()
        self.assertEqual(self.sent, [])

    def test_mute_past_budget_fails_without_interrupt(self):
        self.verdicts = ["mute"]
        with self.assertRaises(RuntimeError) as e:
            self.settle()
        self.assertNotIn(A.INTERRUPT, self.sent)
        self.assertEqual(self.sent, [])
        self.assertIn("still computing an event after 10s", str(e.exception))
        self.assertIn("not interrupted", str(e.exception))
        self.assertIn("zod slog", str(e.exception))

    def test_busy_is_aborted_with_backspace(self):
        self.verdicts = ["busy"] * 30 + ["idle"]
        logs = self.settle()
        self.assertEqual(self.sent, [A.BACKSPACE])
        self.assertIn("aborted a running thread left in the dojo", logs)

    def test_busy_gives_up_after_two_aborts(self):
        self.verdicts = ["busy"]
        with self.assertRaises(RuntimeError):
            self.settle()
        self.assertEqual(self.sent, [A.BACKSPACE, A.BACKSPACE])

    def test_interrupt_flag_sends_one_ctrl_c_per_run(self):
        self.verdicts = ["mute"]
        with self.assertRaises(RuntimeError) as e:
            self.settle(interrupt=True)
        self.assertEqual(self.sent, [A.INTERRUPT])
        self.assertIn("will not again", str(e.exception))
        # a second wait in the same run, on the same ship: no second Ctrl-C
        self.clock[0] = 0.0
        with self.assertRaises(RuntimeError):
            self.settle(interrupt=True)
        self.assertEqual(self.sent, [A.INTERRUPT])

    def test_interrupt_flag_then_idle(self):
        self.verdicts = ["mute"] * 22 + ["idle"]
        logs = self.settle(interrupt=True)
        self.assertEqual(self.sent, [A.INTERRUPT])
        self.assertTrue(any("--interrupt" in l for l in logs))


class DojoWait(unittest.TestCase):
    # dojo_one's completion loop against a scripted console: `script` is (time, bytes)
    # the fake ring has printed by then, and the probe's verdicts are canned. A fake
    # clock, so a 900s wait takes no time. Each probe takes 1s, as a mute one does.
    PROMPT = "\r\n\x1b[K\x1b[u~zod:dojo> \x1b[u"

    def setUp(self):
        self.clock, self.script, self.verdicts, self.probes = [0.0], [], [], []
        ring = lambda: "".join(b for t, b in self.script if t <= self.clock[0])

        def probe(h, s, total):
            self.probes.append(self.clock[0])
            self.clock[0] += 1.0
            v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
            return (v, total, A.BELL if v == "idle" else "")
        patches = {
            "host_for": lambda s, c: None,
            "daemon_status": lambda h, s: "up",
            "guard_port": lambda h, s: None,
            "dojo_ready": lambda *a: None,
            "ring_mark": lambda h, s: 0,
            "capture_at": lambda h, s, off: (len(ring()), ring()[off:]),
            "send_data": lambda h, s, d: None,
            "dojo_probe": probe,
        }
        self.saved = {k: getattr(A, k) for k in patches}
        for k, v in patches.items():
            setattr(A, k, v)
        self.saved_time = A.time
        clock = self.clock

        class FakeTime:
            @staticmethod
            def monotonic():
                return clock[0]

            @staticmethod
            def sleep(s):
                clock[0] += s
        A.time = FakeTime
        self.ship = A.Ship("zod", A.PierConfig())

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(A, k, v)
        A.time = self.saved_time

    ECHO = "~zod:dojo> |commit %x\r\n> |commit %x"
    STALE = PROMPT + "~zod:dojo> stale"  # a row that reads as a prompt holding text

    def spin(self, start, secs):
        # vere's spinner: a frame every 250 ms from `start` for `secs`
        return [(start + i / 4, "|«behn»" + "\x08" * 7) for i in range(int(secs * 4))]

    def run_one(self, timeout):
        self.logs = []
        return A.dojo_one(self.ship, "|commit %x", None, timeout=timeout, log=self.logs.append)

    def test_misread_prompt_on_a_silent_ring_is_probed(self):
        # the incident's shape: the event computes with the row misread, then the ring
        # goes silent. One probe on the silence finds the dojo idle, long before -t.
        self.script = [(0.0, self.ECHO + self.PROMPT + ">=" + self.STALE)] + self.spin(0.5, 14)
        self.verdicts = ["idle"]
        self.run_one(900)
        self.assertEqual(self.logs, ["the prompt row read idle, holding '~zod:dojo> stale', "
                                     "but a probe found the dojo idle", "OK"])
        self.assertEqual(len(self.probes), 1)
        last_frame = self.script[-1][0]
        self.assertGreaterEqual(self.probes[0], last_frame + A.REPROBE_QUIET)
        self.assertLess(self.probes[0], last_frame + A.REPROBE_QUIET + 1)

    def test_silent_probes_repeat(self):
        # a silent ring that probes mute (a key queued behind something that paints
        # nothing) is probed again every REPROBE_QUIET, not once
        self.script = [(0.0, self.ECHO + self.STALE)]
        self.verdicts = ["mute", "mute", "idle"]
        self.run_one(900)
        self.assertEqual(len(self.probes), 3)
        self.assertLess(self.clock[0], 4 * (A.REPROBE_QUIET + 1))

    def test_misread_prompt_under_a_spinner_is_probed(self):
        # a misread row on a ring that never goes silent (an agent's spinner): the
        # REPROBE_EVERY probe finds the dojo idle, at most that late
        self.script = [(0.0, self.ECHO + self.STALE)] + self.spin(0.5, 900)
        self.verdicts = ["idle"]
        self.run_one(900)
        self.assertEqual(self.logs[-1], "OK")
        self.assertEqual(len(self.probes), 1)
        self.assertGreaterEqual(self.probes[0], A.REPROBE_EVERY)
        self.assertLess(self.probes[0], A.REPROBE_EVERY + 1)

    def test_waiting_line_dates_its_verdict(self):
        # one mute probe on a bare prompt, then a misread row under a spinner that
        # never stops, probed mute every REPROBE_EVERY: the minute line dates the
        # verdict and says the ship is busy, and the deadline's probe reads mute
        self.script = ([(0.0, self.ECHO + self.PROMPT), (1.5, self.STALE)]
                       + self.spin(2.0, 200))
        self.verdicts = ["mute"]
        with self.assertRaises(RuntimeError) as e:
            self.run_one(130)
        self.assertIn("still computing an event after 130s", str(e.exception))
        waits = [l for l in self.logs if l.startswith("still waiting")]
        self.assertEqual(len(waits), 2)
        for w in waits:
            m = re.search(r"after \d+s: a probe (\d+)s ago read mute; "
                          r"the ship is printing or computing$", w)
            self.assertIsNotNone(m, w)
            self.assertLessEqual(int(m.group(1)), A.REPROBE_EVERY)

    def test_idle_at_the_deadline_is_still_a_timeout(self):
        # a misread row under a spinner, probed mute in the loop, then idle at the
        # deadline: still a TIMEOUT, saying the drive missed the end, never an OK
        self.script = [(0.0, self.ECHO + self.STALE)] + self.spin(0.5, 200)
        self.verdicts = ["mute", "idle"]  # one probe at REPROBE_EVERY, one at 60s
        with self.assertRaises(RuntimeError) as e:
            self.run_one(60)
        self.assertTrue(str(e.exception).startswith("TIMEOUT after 60s\n"), str(e.exception))
        self.assertIn("missed the command's end", str(e.exception))
        self.assertIn("holding '~zod:dojo> stale'", str(e.exception))
        self.assertNotIn("OK", self.logs)
        self.assertEqual(len(self.probes), 2)


class DojoText(unittest.TestCase):
    def test_prompt_state(self):
        self.assertEqual(A.prompt_state("~zod:dojo> "), ("idle", ""))
        self.assertEqual(A.prompt_state("~zod:dojo< "), ("open", ""))
        self.assertEqual(A.prompt_state("~zod:dojo: nuke? (y/N)"), ("ask", "nuke? (y/N)"))
        self.assertEqual(A.prompt_state("~zod:dojo> (add 2 2)\r\n4\r\n~zod:dojo> x"), ("idle", " x"))
        self.assertIsNone(A.prompt_state("no prompt here"))

    def test_prompt_under_spinner_is_bare(self):
        # vere's spinner paints after the prompt on its row, a frame every 250 ms
        frames = "".join(f"{c}«ames»" + "\x08" * 7 for c in "|/-\\" * 3)
        raw = "\x1b[24;1H\x1b[K~zod:dojo> \x1b[u" + frames
        self.assertEqual(A.prompt_state(raw), ("idle", ""))
        self.assertEqual(A.unspin("a|\x08b/«behn»\x08\x08\x08\x08\x08\x08\x08c"), "abc")
        self.assertEqual(A.unspin("[1 2]\n|/-"), "[1 2]\n|/-")

    def test_prompt_repainted_in_place_is_bare(self):
        # tests/raw/groups-wait.raw is a ship's console, byte for byte, from `|commit
        # %gnat-dms` to `|commit %gnat-board`. After the groups commit, vere painted a
        # spinner frame and repainted the prompt over it with no line feed, and the row
        # read as a prompt holding `~ship:dojo>`. The drive never probed again and timed
        # out after 900s on a dojo idle the whole time.
        raw = RAW.joinpath("groups-wait.raw").read_bytes().decode("utf-8")
        start = raw.index("> |commit %gnat-groups")
        row = "\x1b[K~migrev-migrev-dolseg:dojo> \x1b[u"
        end = raw.rindex(row, start, raw.index("|commit %gnat-board")) + len(row)
        self.assertEqual(A.prompt_state(raw[start:end]), ("idle", ""))
        # every cut through the window: a repaint never reads as text on the prompt
        for cut in range(start, raw.index("|commit %gnat-board")):
            state = A.prompt_state(raw[start:cut])
            if state and state[0] == "idle":
                self.assertNotIn("dojo>", state[1], raw[cut - 80:cut])
        # the measured shape, and a move to mid-row that is NOT a repaint
        frame = "|«behn»" + "\x08" * 7
        self.assertEqual(A.prompt_state(f"~zod:dojo> \x1b[u\x1b[39;29H\x1b[s{frame}"
                                        f"\x1b[39;1H\x1b[K~zod:dojo> \x1b[u"), ("idle", ""))
        self.assertEqual(A.prompt_state("~zod:dojo> \x1b[H\x1b[2K~zod:dojo> "), ("idle", ""))
        self.assertEqual(A.prompt_state("~zod:dojo> ab\x1b[39;13Hc"), ("idle", " abc"))

    def test_reject_report_points_at_drop(self):
        rep = A.dojo_reject_report("(add 2 2)", "(add 2", "pane")
        self.assertIn("char 7", rep)
        self.assertIn("did NOT run", rep)
        self.assertNotIn("(stab", rep)
        self.assertIn("(stab", A.dojo_reject_report("=x /lib/a_b/hoon", "", ""))

    def test_truncate_output(self):
        ship = A.Ship("zod", A.PierConfig())
        text = "\n".join(str(i) for i in range(A.DOJO_OUTPUT_MAX + 5))
        out = A.truncate_output(text, ship).split("\n")
        self.assertEqual(len(out), A.DOJO_OUTPUT_MAX + 1)
        self.assertIn("5 more lines", out[-1])
        self.assertEqual(A.truncate_output("a\nb", ship), "a\nb")

    def test_text_helpers(self):
        self.assertEqual(A.trim_blank(["", " ", "a", "", "b", ""]), ["a", "", "b"])
        self.assertEqual(A.tail_n("a\nb\nc", 2), "b\nc")
        self.assertEqual(A.shq("it's"), "'it'\\''s'")
        self.assertEqual(A._to_pids("12\nabc\n0\n 7 \n"), [12, 7])


class Render(unittest.TestCase):
    def test_regex_clean(self):
        self.assertEqual(A._regex_clean("\x1b[31mred\x1b[0m\r\n\x1b]0;title\x07x"), "red\nx")

    def test_clean_pane_erases_spinner(self):
        raw = "~zod:dojo> |\x08/\x08-\x08\\\x08 \x08done\r\n"
        self.assertEqual(A.clean_pane(raw).strip(), "~zod:dojo> done")
        # a regex strip keeps what the backspaces erased
        self.assertIn("|", A._regex_clean(raw))

    def test_clean_pane_keeps_long_line_whole(self):
        run = "x" * 4000
        lines = [l for l in A.clean_pane(f"a\r\n{run}\r\nb\r\n").split("\n") if l]
        self.assertEqual(lines, ["a", run, "b"])

    def test_cook_pty_stamps_by_segment(self):
        # a row reaches history when the NEXT line feed scrolls it off the 2-row screen,
        # so a line is stamped with the segment that pushed it, not the one that wrote it
        raw = "line one\r\nline two\r\nline three\r\n~zod:dojo> \r\n"
        t0, t1 = 1_700_000_000.0, 1_700_000_005.0
        out = A.cook_pty(raw, [(0, t0), (20, t1)], 0)
        self.assertEqual([l.split("\t")[1] for l in out], ["line one", "line two", "line three"])
        self.assertEqual([l.split("\t")[0] for l in out],
                         [A.fmt_stamp(t0), A.fmt_stamp(t1), A.fmt_stamp(t1)])

    def test_cook_pty_cuts_segments_by_byte(self):
        # `.idx` offsets count bytes, and a spinner frame's `«»` is two bytes a char.
        # Cut by char, each frame moved the cut two chars on, so after a quiet stretch
        # a line written at 12:50 was stamped 12:35, the last read before the silence.
        frames = ("|«behn»" + "\x08" * 7) * 40  # 80 bytes more than chars
        early = f"line one\r\n~zod:dojo> {frames}\r\n"
        late = "|commit %x\r\n~zod:dojo> \r\n"
        t0, t1 = 1_700_000_000.0, 1_700_000_900.0
        out = A.cook_pty(early + late, [(0, t0), (len(early.encode()), t1)], 0)
        self.assertEqual(out[-1], f"{A.fmt_stamp(t1)}\t|commit %x")
        # a cut through the middle of a two-byte char loses nothing
        out = A.cook_pty("«a»\r\nb\r\n", [(0, t0), (1, t1)], 0)
        self.assertEqual([l.split("\t")[1] for l in out], ["«a»", "b"])

    def test_fmt_stamp(self):
        self.assertRegex(A.fmt_stamp(time.time()), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}$")

    def test_attach_snapshot(self):
        self.assertEqual(A.attach_snapshot(b"short"), b"short")
        ring = b"junk\x1b[6144;1H" + b"a" * A.ATTACH_TAIL + b"\nrest\n"
        out = A.attach_snapshot(ring)
        self.assertTrue(out.startswith(b"\x1b[!p\x1b[0m\r\n"))
        self.assertTrue(out.endswith(b"rest\n"))
        self.assertNotIn(b"\x1b[6144", out)

    def test_wrap(self):
        self.assertEqual(A.wrap("aa bb cc dd", 5), ["aa bb", "cc dd"])
        self.assertEqual(A.wrap("  pre formatted line", 5), ["  pre formatted line"])
        self.assertEqual(A.wrap("a\n\nb", 80), ["a", "", "b"])

    def test_sizes(self):
        self.assertEqual(A.print_size(512 * 1024), "512 MB")
        self.assertEqual(A.print_size(3 * 1024 * 1024), "3.0 GB")

    def test_parse_since(self):
        self.assertEqual(A.parse_since("5m"), 300)
        self.assertEqual(A.parse_since(" 2h "), 7200)
        with self.assertRaises(A.UsageError):
            A.parse_since("soon")


def fake_daemon(path, replies, hold=False):
    # a daemon that answers one connection: reads a frame, sends replies[ctype] (a list of
    # (stype, payload)) back, or holds the connection open forever when `hold`
    import socket
    import threading
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    seen = []

    def serve():
        conn, _ = srv.accept()
        fr = A.FrameReader()
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            for t, payload in fr.feed(chunk):
                seen.append((t, payload))
                for st, sp in replies.get(t, []):
                    A.send_frame(conn, st, sp)
                if not hold:
                    conn.close()
                    srv.close()
                    return
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return seen


class Engine(unittest.TestCase):
    def test_sock_request_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s")
            seen = fake_daemon(path, {A.C_PING: [(A.S_DATA, b"noise"), (A.S_PONG, b"11 22 9.9.9")]})
            self.assertEqual(A.engine_ping(path), (11, 22, "9.9.9"))
            self.assertEqual(seen, [(A.C_PING, b"")])
            # no daemon: every client reads it as down, none raises
            os.unlink(path)
            self.assertIsNone(A.engine_ping(path))
            self.assertIsNone(A.sock_capture_at(path, 0))
            self.assertFalse(A.sock_send(path, b"x"))
            # fire-and-forget sends the frame and returns once connected
            seen = fake_daemon(path, {})
            self.assertTrue(A.sock_send(path, b"keys"))
            time.sleep(0.1)
            self.assertEqual(seen, [(A.C_DATA, b"keys")])

    def test_sock_capture_at_decodes_total(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s")
            seen = fake_daemon(path, {A.C_CAPTURE_AT: [(A.S_SNAPSHOT_AT, struct.pack("!Q", 500) + b"tail")]})
            self.assertEqual(A.sock_capture_at(path, 123), (500, b"tail"))
            self.assertEqual(seen, [(A.C_CAPTURE_AT, struct.pack("!Q", 123))])

    def test_sock_request_times_out_on_a_mute_daemon(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s")
            fake_daemon(path, {}, hold=True)
            t0 = time.monotonic()
            self.assertIsNone(A.sock_request(path, A.C_PING, want=A.S_PONG, timeout=0.2))
            self.assertLess(time.monotonic() - t0, 2)

    def test_frame_reader_reassembles(self):
        frames = A.HDR.pack(A.C_PING, 0) + A.HDR.pack(A.S_DATA, 3) + b"abc" + A.HDR.pack(A.S_EXIT, 2) + b"xy"
        fr = A.FrameReader()
        got = []
        for i in range(0, len(frames), 2):  # feed in 2-byte dribbles
            got += list(fr.feed(frames[i:i + 2]))
        self.assertEqual(got, [(A.C_PING, b""), (A.S_DATA, b"abc"), (A.S_EXIT, b"xy")])
        self.assertEqual(fr.buf, bytearray())

    def test_idx_record_layout(self):
        self.assertEqual(A.IDX.size, 16)
        off, t = A.IDX.unpack(A.IDX.pack(42, 1.5))
        self.assertEqual((off, t), (42, 1.5))

    def test_pty_read_parses_header_and_index(self):
        ship = A.Ship("zod", A.PierConfig(path="p"))
        host = FakeHost({"python3": "77 900 2\n0 1.5\n400 2.5\nraw\nbytes"}, ssh=None)
        self.assertEqual(A.pty_read(host, ship, 0), (77, 900, [(0, 1.5), (400, 2.5)], "raw\nbytes"))
        self.assertEqual(A.pty_read(FakeHost({"python3": "0 0 0\n"}, ssh=None), ship, 0), (0, 0, [], ""))
        self.assertEqual(A.pty_read(FakeHost(ssh=None), ship, 0), (0, 0, [], ""))

    def test_on_ship_routes_streams(self):
        import contextlib
        cfg = A.Config(ships=[A.Ship("zod", A.PierConfig())], desks=[], hosts={})
        a = A.Parsed(cmd=None, selector="~zod", cfg=cfg, flags={}, pos=[])

        def work(ship, host, log):
            log(f"note {ship.name} {host.name}")
            return "out\n"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            A.on_ship(a, work)
            A.on_ship(a, lambda ship, host, log: "")
        self.assertEqual((out.getvalue(), err.getvalue()), ("out\n", "note zod local\n"))
        with self.assertRaises(RuntimeError):
            A.on_ship(a, lambda ship, host, log: (_ for _ in ()).throw(RuntimeError("boom")))

    def test_vere_argv(self):
        ship = A.Ship("zod", A.PierConfig(path="/p"))
        with self.assertRaises(RuntimeError):
            A.vere_argv(ship, FakeHost({"test": OSError()}), "roll")
        self.assertEqual(A.vere_argv(ship, FakeHost({"test": ""}), "next"), ["/p/.run", "next"])

    def test_offline_restarts_after_failure(self):
        # a failing op still restarts the ship, and the error says so
        calls = []
        ship = A.Ship("zod", A.PierConfig(path="/p"))
        host = FakeHost(ssh=None)
        saved = (A.pier_exists, A.daemon_status, A.daemon_stop, A.daemon_start, A.assert_port_match)
        try:
            A.pier_exists = lambda h, s: True
            A.daemon_status = lambda h, s: (1, 2, A.VERSION)
            A.assert_port_match = lambda h, s, st: None
            A.daemon_stop = lambda s, h: calls.append("stop") or True
            A.daemon_start = lambda s, h: calls.append("start") or (3, 4, A.VERSION)

            def op():
                calls.append("op")
                raise RuntimeError("disk full")
            with self.assertRaises(RuntimeError) as cm:
                A.offline(ship, host, calls.append, "chop", op)
            self.assertIn("chop failed, restarted (daemon 3 child 4): disk full", str(cm.exception))
            self.assertEqual([c for c in calls if c in ("stop", "op", "start")], ["stop", "op", "start"])
            # a ship already down is left down
            calls.clear()
            A.daemon_status = lambda h, s: None
            self.assertEqual(A.offline(ship, host, calls.append, "next", lambda: "ok"), "ok")
            self.assertEqual([c for c in calls if c in ("stop", "op", "start")], [])
        finally:
            A.pier_exists, A.daemon_status, A.daemon_stop, A.daemon_start, A.assert_port_match = saved

    def test_running_http_port(self):
        host = FakeHost({"ps": "/p/.run --loom 31 --http-port 8080 /p\n"})
        self.assertEqual(A.running_http_port(host, 1), (True, 8080))
        self.assertEqual(A.running_http_port(FakeHost({"ps": "urbit /p"}), 1), (True, None))
        self.assertEqual(A.running_http_port(FakeHost(), 1), (False, None))

    def test_run_argv(self):
        ship = A.Ship("zod", A.PierConfig(path="/p/zod", port=8080, loom=31, ames=31337, local=True))
        self.assertEqual(A.run_argv(ship, FakeHost({"test": ""})),
                         ["/p/zod/.run", "-L", "--loom", "31", "--http-port", "8080", "-p", "31337", "/p/zod"])
        with self.assertRaises(RuntimeError) as cm:  # no <pier>/.run: never an `urbit` off PATH
            A.run_argv(ship, FakeHost({"test": OSError()}))
        self.assertIn("docked", str(cm.exception))

    def test_sock_path_limit(self):
        host = A.Host("local", "/tmp")
        self.assertEqual(A.sock_path(A.Ship("zod", A.PierConfig(path="p")), host), "/tmp/p/.admiral.sock")
        with self.assertRaises(RuntimeError):
            A.sock_path(A.Ship("zod", A.PierConfig(path="p" * 120)), host)


class ConfigFile(unittest.TestCase):
    # a scratch dir holding the config, a pier dir "p" and a desk dir "desk"
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "p").mkdir()
        (self.dir / "desk").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text):
        p = self.dir / "admiral.toml"
        p.write_text(text)
        return A.load_config(p)

    def ship(self, extra="", pier="", port=80):
        # one good ship, plus `extra` lines and `pier` keys
        return self.load(f'[ships.zod]\nhttp = "http://localhost:{port}"\n'
                         f'pier = {{ path = "p", port = {port}{pier} }}\n{extra}\n').ships[0]

    def refused(self, text, why):
        with self.assertRaises(A.ConfigError, msg=text) as cm:
            self.load(text)
        self.assertIn(why, str(cm.exception), text)
        return str(cm.exception)

    def test_minimal(self):
        ship = self.ship()
        self.assertEqual(ship.pier, A.PierConfig(path="p", port=80))
        self.assertEqual((ship.http, ship.host, ship.latency), ("http://localhost:80", None, None))
        self.assertEqual(self.load('[ships.zod]\nhttp = "http://l"\npier = { path = "p", port = 1 }\n').desks, [])

    def test_optional_keys(self):
        ship = self.ship(pier=", ames = 31337, loom = 31, memo = 1000, local = true, persist = true")
        self.assertEqual((ship.pier.ames, ship.pier.loom, ship.pier.memo, ship.pier.local, ship.pier.persist),
                         (31337, 31, 1000, True, True))
        self.assertFalse(self.ship().pier.persist)

    GOOD = '[ships.zod]\nhttp = "http://l"\npier = { path = "p", port = 1 }\n'

    def test_required_keys(self):
        self.refused("", '"ships" must be')
        self.refused('[ships.zod]\npier = { path = "p", port = 1 }\n', "is missing http")
        self.refused('[ships.zod]\nhttp = "http://l"\n', "is missing pier")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = { port = 1 }\n', "pier is missing path")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = { path = "p" }\n', "pier is missing port")
        self.refused(self.GOOD + "[hosts.b]\nroot = '/r'\n", "is missing ssh")
        self.refused(self.GOOD + "[hosts.b]\nssh = 'a@b'\n", "is missing root")
        self.refused(self.GOOD + "[[desks]]\nname = 'house'\n", "is missing src")
        self.refused(self.GOOD + "[[desks]]\nsrc = 'desk'\n", "is missing name")

    def test_unknown_keys(self):
        self.refused(self.GOOD + "colour = 1\n", "unknown key(s) colour")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = { path = "p", port = 1, prot = 1 }\n',
                     "unknown key(s) prot")
        self.refused("top = 1\n" + self.GOOD, "unknown top-level key(s) top")
        self.refused(self.GOOD + "[hosts.b]\nssh = 'a@b'\nroot = '/r'\nuser = 'x'\n", "unknown key(s) user")
        self.refused(self.GOOD + "[[desks]]\nname = 'house'\nsrc = 'desk'\nmount = 1\n", "unknown key(s) mount")

    def test_wrong_types_and_values(self):
        bad = lambda pier, why: self.refused(f'[ships.zod]\nhttp = "http://l"\npier = {{ path = "p", {pier} }}\n', why)
        bad("port = 70000", "1-65535")
        bad('port = "80"', "1-65535")
        bad("port = 1, local = 1", "true or false")
        bad('port = 1, persist = "yes"', "true or false")
        bad("port = 1, loom = 0", "positive integer")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = "p"\n', "pier must be a table")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = false\n', "pier must be a table")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = { path = "~/p", port = 1 }\n', "not ~")
        self.refused('[ships.zod]\nhttp = "http://l"\npier = { path = "", port = 1 }\n', "must not be empty")
        self.refused('[ships.zod]\nhttp = "example.com"\npier = { path = "p", port = 1 }\n', "with its scheme")
        self.refused(self.GOOD + "code = 'nope'\n", "+code")
        self.refused(self.GOOD + "mnemonic = 'one'\n", "word list")
        self.refused(self.GOOD + "[[desks]]\nname = 'Bad'\nsrc = 'desk'\n", "desk name")
        self.refused(self.GOOD + "[[desks]]\nname = 'house'\nsrc = 5\n", "must be a string")
        self.refused(self.GOOD + "[hosts.b]\nssh = 'a@b'\nroot = 'rel'\n", "absolute")
        self.refused(self.GOOD + "desks = 'x'\n", "unknown key(s) desks")  # inside [ships.zod]

    def test_hosts(self):
        self.refused(self.GOOD + 'host = "ghost"\n', "is not a [hosts.<name>]")
        self.refused(self.GOOD + 'host = ""\n', "is not a [hosts.<name>]")
        self.refused(self.GOOD + 'host = "local"\n', "is not a [hosts.<name>]")
        self.refused(self.GOOD + "host = false\n", "must be a string")
        self.refused(self.GOOD + "[hosts.local]\nssh='a'\nroot='/r'\n", "reserved")
        cfg = self.load('[hosts.b]\nssh = "a@b"\nroot = "/r"\n[ships.zod]\nhost = "b"\nhttp = "http://l"\n'
                        'pier = { path = "not/here/remote", port = 1 }\n')  # a remote pier isn't checked here
        self.assertEqual(cfg.ships[0].host, "b")

    def test_paths_must_exist(self):
        self.refused('[ships.zod]\nhttp = "http://l"\npier = { path = "nope", port = 1 }\n', "no directory at")
        self.refused(self.GOOD + "[[desks]]\nname = 'house'\nsrc = 'nope'\n", "no directory at")
        self.load(self.GOOD + f"[[desks]]\nname = 'house'\nsrc = '.'\n")
        self.load(f'[ships.zod]\nhttp = "http://l"\npier = {{ path = "{self.dir / "p"}", port = 1 }}\n')

    def test_desks_once_each(self):
        self.refused(self.GOOD + "[[desks]]\nname = 'house'\nsrc = 'desk'\n"
                     "[[desks]]\nname = 'house'\nsrc = 'desk'\n", "listed twice")

    def test_ship_names_are_patp(self):
        for good in ("zod", "marzod", "sampel-palnet", "moddel-migrev-dolseg", "dozzod-dozzod--dozzod-dozzod"):
            self.load(self.GOOD.replace("zod]", f"{good}]", 1))
        for bad in ("bus1", "Zod", "foo", "sampel-palzzz", "samp", "zod-", "a_b"):
            self.refused(self.GOOD.replace("zod]", f"{bad}]", 1), "must be the ship's @p")

    def test_secrets_are_never_echoed(self):
        for key, val in (("code", "sekret-value"), ("master-ticket", "sekret value"), ("mnemonic", "Sekret")):
            msg = self.refused(self.GOOD + f"{key} = '{val}'\n", f".{key} must be")
            self.assertNotIn("ekret", msg)
            self.assertIn("value not shown", msg)

    def test_every_refusal_names_the_file_and_table(self):
        msg = self.refused('[ships.zod]\nhttp = "http://l"\npier = { path = "p", port = 0 }\n', "1-65535")
        self.assertTrue(msg.startswith('admiral.toml: ships["zod"].pier.port '), msg)

    def test_login_page_posts_to_eyre(self):
        page = A.login_page("https://x.urbit.house", "lidlut-tabwed-pillex-ridrup", "/apps/x")
        self.assertIn('action="https://x.urbit.house/~/login"', page)
        self.assertIn('name=password value="lidlut-tabwed-pillex-ridrup"', page)
        self.assertIn('name=redirect value="/apps/x"', page)
        self.assertIn('"', A.login_page("h", 'a"b', "/"))

    def test_find_repo_walks_up(self):
        (self.dir / "admiral.toml").write_text("")
        sub = self.dir / "a" / "b"
        sub.mkdir(parents=True)
        cwd = os.getcwd()
        try:
            os.chdir(sub)
            self.assertEqual(A._find_repo(), (self.dir.resolve(), "admiral.toml"))
            (self.dir / ".admiral.toml").write_text("")
            with self.assertRaises(SystemExit):
                A._find_repo()
        finally:
            os.chdir(cwd)


class LatencyConfig(ConfigFile):
    LAT = "latency = {{ ms = {ms}, port = {port} }}"

    def test_latency_is_a_table_with_ms_and_port(self):
        ship = self.ship("latency = { ms = 1000, port = 8080 }")
        self.assertEqual((ship.latency, ship.latency_port), (1000, 8080))
        self.assertEqual(A.latency_want(ship), "8080 80 1000")
        self.assertEqual(A.latency_want(self.ship()), "none")
        ship = self.ship("latency = { ms = 0, port = 8080 }")
        self.assertIsNone(ship.latency)  # ms 0: no proxy
        self.assertEqual(A.latency_want(ship), "none")

    def test_the_old_string_form_is_refused_naming_the_table(self):
        for old in ('"1000"', "1000"):
            with self.assertRaises(A.ConfigError, msg=old) as cm:
                self.ship(f"latency = {old}")
            self.assertIn("latency = { ms = 1000, port = <port> }", str(cm.exception))
        with self.assertRaises(A.ConfigError) as cm:
            self.ship("latency = { ms = 1000, port = 9000 }\nlatency-port = 9000")
        self.assertIn("latency-port is gone", str(cm.exception))

    def test_the_port_is_required(self):
        with self.assertRaises(A.ConfigError) as cm:
            self.ship("latency = { ms = 1000 }")
        self.assertIn("needs port", str(cm.exception))
        self.assertIn(A.LATENCY_FORM, str(cm.exception))

    def test_bad_tables(self):
        for bad, why in (('{ port = 8080 }', "needs ms"),
                         ('{ ms = "1000", port = 8080 }', "ms must be"),
                         ('{ ms = -5, port = 8080 }', "ms must be"),
                         ('{ ms = 1000, port = 0 }', "port must be"),
                         ('{ ms = 1000, port = true }', "port must be"),
                         ('{ ms = 1000, port = 8080, jitter = 1 }', "unknown key")):
            with self.assertRaises(A.ConfigError, msg=bad) as cm:
                self.ship(f"latency = {bad}")
            self.assertIn(why, str(cm.exception), bad)
        with self.assertRaises(A.ConfigError) as cm:
            self.ship("latency = { ms = 1000, port = 80 }")
        self.assertIn("own http port", str(cm.exception))

    def test_engine_gate_and_brief(self):
        self.assertFalse(A.engine_has_latency("0.1.0"))
        self.assertTrue(A.engine_has_latency("0.2.0"))
        self.assertFalse(A.engine_has_latency("0.2.0", A.LATENCY_STOP_SINCE))
        self.assertTrue(A.engine_has_latency(A.VERSION, A.LATENCY_STOP_SINCE))
        self.assertFalse(A.engine_has_latency(None))
        self.assertIsNone(A.latency_brief("none"))
        self.assertEqual(A.latency_brief("8080 80 1000"), "1000ms :8080 -> :80")
        self.assertEqual(A.latency_brief("8080 80 0"), "off :8080 -> :80 (direct)")

    def test_detail_says_when_live_differs_from_config(self):
        ship = self.ship("latency = { ms = 1000, port = 8080 }")
        st = lambda latency, version=A.VERSION: A.ShipState(
            "zod", "local", True, False, False, True, [], "up", True, True, False, 80,
            version=version, latency=latency)
        self.assertEqual(A.latency_detail(ship, st("1000ms :8080 -> :80")), "1000ms :8080 -> :80")
        self.assertIn("latency reload", A.latency_detail(ship, st("300ms :8080 -> :80")))
        self.assertIn("latency reload", A.latency_detail(ship, st(None)))
        self.assertIn("engine-upgrade", A.latency_detail(ship, st(None, "0.1.0")))


class StubHandler(BaseHTTPRequestHandler):
    # the ship behind the proxy: GET / answers, POST / echoes, GET /sse streams events
    # SSE_GAP apart and records when it sent each one
    SSE_GAP = 0.3
    sent: list[float] = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(3):
                if i:
                    time.sleep(self.SSE_GAP)
                StubHandler.sent.append(time.monotonic())
                self.wfile.write(f"data: {i}\n\n".encode())
                self.wfile.flush()
            return
        body = b"hello"
        self.send_response(200)
        self.send_header("Set-Cookie", "urbauth=abc; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Latency(unittest.TestCase):
    MS = 400  # round trip; 200ms each way, ±20%

    @classmethod
    def setUpClass(cls):
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=cls.stub.serve_forever, daemon=True).start()
        cls.target = cls.stub.server_address[1]
        cls.proxy = A.DelayProxy()
        cls.port = free_port()
        got = cls.proxy.control(f"set {cls.port} {cls.target} {cls.MS}")
        assert got == f"{cls.port} {cls.target} {cls.MS}", got

    @classmethod
    def tearDownClass(cls):
        cls.stub.shutdown()
        cls.stub.server_close()

    def setUp(self):
        self.proxy.set(self.port, self.target, self.MS)

    def request(self, method="GET", path="/", body=None):
        t0 = time.monotonic()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers={"Host": "ship.example:8080"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return time.monotonic() - t0, r, data

    def test_get_and_post_each_take_the_round_trip(self):
        took, r, data = self.request()
        self.assertEqual((r.status, data), (200, b"hello"))
        self.assertEqual(r.getheader("Set-Cookie"), "urbauth=abc; Path=/")  # untouched
        self.assertGreater(took, 0.8 * self.MS / 1000)
        self.assertLess(took, 1.2 * self.MS / 1000 + 0.2)
        took, r, data = self.request("POST", "/", b"poke")
        self.assertEqual(data, b"poke")
        self.assertGreater(took, 0.8 * self.MS / 1000)
        self.assertLess(took, 1.2 * self.MS / 1000 + 0.2)

    def test_requests_in_flight_overlap(self):
        times: list[float] = []
        t0 = time.monotonic()
        ts = [threading.Thread(target=lambda: times.append(self.request()[0])) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(times), 4)
        # four in a row would take 4 round trips; overlapping, about one
        self.assertLess(time.monotonic() - t0, 1.2 * self.MS / 1000 + 0.3)

    def test_sse_events_arrive_in_order_each_delayed(self):
        StubHandler.sent = []
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"GET /sse HTTP/1.1\r\nHost: ship\r\n\r\n")
        buf, got = b"", []
        while len(got) < 3:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            for m in re.finditer(rb"data: (\d+)\n\n", buf):
                got.append((int(m.group(1)), time.monotonic()))
            buf = re.sub(rb"(?s).*data: \d+\n\n", b"", buf)
        s.close()
        self.assertEqual([i for i, _ in got], [0, 1, 2])
        one_way = self.MS / 2000
        for (i, arrived), sent in zip(got, StubHandler.sent):
            lag = arrived - sent
            self.assertGreater(lag, 0.8 * one_way, f"event {i}")
            self.assertLess(lag, 1.2 * one_way + 0.1, f"event {i}")
        # the stream stays open between events: they arrive their gap apart, not all at once
        self.assertGreater(got[2][1] - got[0][1], 1.5 * StubHandler.SSE_GAP)

    def test_off_is_direct(self):
        self.assertEqual(self.proxy.control(f"set {self.port} {self.target} 0"),
                         f"{self.port} {self.target} 0")
        took, r, data = self.request()
        self.assertEqual(data, b"hello")
        self.assertLess(took, 0.1)

    def test_stop_closes_the_port(self):
        p = A.DelayProxy()
        port = free_port()
        self.assertEqual(p.control(f"set {port} {self.target} 50"), f"{port} {self.target} 50")
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        self.assertEqual(p.control("stop"), "none")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        self.assertEqual(p.control("stop"), "none")  # stopping no proxy is fine
        # and it starts again, on another port
        port2 = free_port()
        self.assertEqual(p.control(f"set {port2} {self.target} 50"), f"{port2} {self.target} 50")
        p.control("stop")

    def test_control_errors(self):
        self.assertTrue(self.proxy.control("set x y z").startswith("error"))
        self.assertTrue(self.proxy.control(f"set {self.target} {self.target} 5").startswith("error"))
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        try:
            got = A.DelayProxy().control(f"set {busy.getsockname()[1]} {self.target} 5")
        finally:
            busy.close()
        self.assertTrue(got.startswith("error can't listen"), got)
        self.assertEqual(self.proxy.control(""), f"{self.port} {self.target} {self.MS}")


class LatencyDaemon(unittest.TestCase):
    # the proxy through a real daemon (holding `sleep`, not urbit): set over the socket,
    # kept across an in-place upgrade, gone with the daemon
    def test_set_survives_upgrade(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "s")
            proc = subprocess.Popen([sys.executable, str(Path(A.__file__)), "__daemon", "--foreground",
                                     "--sock", sock, "--", "sleep", "60"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    if A.engine_ping(sock):
                        break
                    time.sleep(0.1)
                pid = A.engine_ping(sock)[0]
                self.assertEqual(A.sock_latency(sock), "none")
                port, target = free_port(), free_port()
                want = f"{port} {target} 300"
                self.assertEqual(A.sock_latency(sock, f"set {port} {target} 300"), want)
                self.assertEqual(Path(sock + ".latency").read_text().strip(), want)
                A.sock_request(sock, A.C_UPGRADE)
                time.sleep(1.0)
                self.assertEqual(A.engine_ping(sock)[0], pid)  # same process, new image
                self.assertEqual(A.sock_latency(sock), want)
                socket.create_connection(("127.0.0.1", port), timeout=2).close()  # listening again
                self.assertEqual(A.sock_latency(sock, "stop"), "none")
                self.assertFalse(os.path.exists(sock + ".latency"))  # an upgrade won't bring it back
                self.assertEqual(A.sock_latency(sock, f"set {port} {target} 300"), want)
                A.sock_request(sock, A.C_KILL)
                proc.wait(15)
                self.assertFalse(os.path.exists(sock + ".latency"))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


class HarnessDojo(unittest.TestCase):
    # Dojo windows over a simulated daemon ring: capture_at and ring_mark are swapped
    # for fakes that serve a growing byte stream, so marks and slices can be checked
    # exactly without a ship
    def setUp(self):
        self.stream = ""
        self.saved = (A.ring_mark, A.capture_at)
        A.ring_mark = lambda h, s: len(self.stream)
        A.capture_at = lambda h, s, off: (len(self.stream), self.stream[max(off, len(self.stream) - A.RING_MAX):])
        cfg = A.Config([A.Ship("zod", A.PierConfig())], [], {})
        self.d = H.Dojo(cfg.ships[0], cfg)

    def tearDown(self):
        A.ring_mark, A.capture_at = self.saved

    def test_windows(self):
        # the dojo paints its prompt, and output arrives on fresh rows after it
        self.stream += "~zod:dojo> "
        a = self.d.mark()
        self.assertEqual(self.d.run_mark, a)
        self.stream += "\r\nalpha\r\n~zod:dojo> "
        b = self.d.mark()
        self.stream += "\r\nbeta\r\n~zod:dojo> "
        self.assertIn("alpha", self.d.since(a))
        self.assertIn("beta", self.d.since(a))
        self.assertNotIn("alpha", self.d.since(b))
        between = self.d.between(a, b)
        self.assertIn("alpha", between)
        self.assertNotIn("beta", between)
        self.assertEqual(self.d.grep_between(a, b, [r"alp"]), ["alpha"])
        self.assertEqual(self.d.grep(None, [r"bet"]), ["beta"])  # runlog: from the first mark
        self.d.assert_clean(b, [r"alpha"])
        with self.assertRaises(RuntimeError):
            self.d.assert_clean_between(a, b, [r"alpha"])
        with self.assertRaises(RuntimeError):
            self.d.between(b, a)

    def test_evicted_mark_is_an_error(self):
        a = self.d.mark()
        self.stream += "x" * (A.RING_MAX + 10)
        with self.assertRaises(RuntimeError) as cm:
            self.d.since(a)
        self.assertIn("no longer holds it", str(cm.exception))


class RcBlocks(unittest.TestCase):
    def test_write_strip_remove(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "rc")
            p.write_text("export A=1\n")
            A.rc_write_block(p, "installed", "body one")
            A.rc_write_block(p, "installed", "body two")  # refresh, not duplicate
            text = p.read_text()
            self.assertEqual(text.count("# >>> admiral installed >>>"), 1)
            self.assertIn("body two", text)
            self.assertNotIn("body one", text)
            self.assertTrue(A.rc_present(p, "installed"))
            A.rc_remove_block(p, "installed")
            self.assertEqual(p.read_text(), "export A=1\n")
            self.assertFalse(A.rc_present(p, "installed"))

    def test_enrich_snippets_parse(self):
        for shell in ("bash", "zsh"):
            body = A._installed_install(shell)
            self.assertIn("function admiral", body)
            self.assertIn("__complete", body)
            self.assertIn("unset -f admiral", A._installed_uninstall(shell))


class Resolution(unittest.TestCase):
    cfg = A.Config(ships=[A.Ship("zod", A.PierConfig(path="p")), A.Ship("nec", A.PierConfig(path="piers/nec"), host="drain")],
                   desks=[A.Desk("base", "."), A.Desk("other", ".")], hosts={"drain": A.Host("drain", "/r", "u@b")})

    def test_ships(self):
        self.assertIs(A.resolve_ship("~zod", self.cfg), self.cfg.ships[0])
        with self.assertRaises(RuntimeError) as cm:
            A.resolve_ship("bus", self.cfg)
        self.assertIn("zod, nec", str(cm.exception))

    def test_desks(self):
        self.assertIs(A.resolve_desk("other", self.cfg), self.cfg.desks[1])
        with self.assertRaises(RuntimeError):
            A.resolve_desk("nope", self.cfg)
        with self.assertRaises(RuntimeError):
            A.resolve_desk("x", A.Config([], [], {}))

    def test_hosts(self):
        self.assertIs(A.host_for(self.cfg.ships[0], self.cfg), A.LOCAL)
        h = A.host_for(self.cfg.ships[1], self.cfg)
        self.assertEqual((h.name, h.ssh, h.root), ("drain", "u@b", "/r"))
        self.assertEqual(A.pier_dir(self.cfg.ships[1], h), "/r/piers/nec")
        self.assertEqual(A.pier_dir(A.Ship("x", A.PierConfig(path="/abs")), h), "/abs")

    def test_ship_base(self):
        self.assertEqual(A.ship_base(A.Ship("z", A.PierConfig(), http="https://example.com/")), "https://example.com")
        self.assertEqual(A.ship_base(A.Ship("z", A.PierConfig(), http="http://l:80")), "http://l:80")

    def test_sync_flags(self):
        flags = A.sync_flags()
        self.assertIn("--exclude=.*", flags)
        self.assertIn("--exclude=admiral.toml", flags)
        self.assertIn("--delete", flags)
        self.assertIn("--checksum", flags)

    def test_usage_strings(self):
        c = cmd([A.Flag("-t", "int", "", ph="<secs>"), A.Flag("--yes", "bool", "")],
                [A.Pos("CMD", True, ""), A.Pos("more", False, "", variadic=True)])
        self.assertEqual(A.usage_line(c), "Usage: ./admiral.py <ship> x [-t <secs>] [--yes] <CMD> [more...]")


class ExitCodes(unittest.TestCase):
    # the CLI run for real, in a scratch dir holding its own config
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "p").mkdir()
        (self.dir / "admiral.toml").write_text(
            '[ships.zod]\nhttp = "http://localhost:18080"\npier = { path = "p", port = 18080 }\n')

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        import subprocess
        r = subprocess.run([sys.executable, str(Path(A.__file__)), *args], cwd=self.dir,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def test_help_and_configure_write_stdout(self):
        for args in (["help"], ["help", "run"], ["configure"]):
            code, out, err = self.cli(*args)
            self.assertEqual((code, err), (0, ""), args)
            self.assertGreater(len(out), 200, args)

    def test_usage(self):
        self.assertEqual(self.cli("zod", "nope")[0], A.EXIT_USAGE)
        self.assertEqual(self.cli("zod", "dojo")[0], A.EXIT_USAGE)  # missing argument
        self.assertEqual(self.cli("help", "nope")[0], A.EXIT_USAGE)
        self.assertEqual(self.cli("run")[0], A.EXIT_USAGE)  # needs a ship first

    def test_config(self):
        (self.dir / "admiral.toml").write_text("[ships.zod]\npier = { colour = 1 }\n")
        code, _, err = self.cli("zod", "exit")
        self.assertEqual(code, A.EXIT_CONFIG, err)
        (self.dir / "admiral.toml").write_text("not toml [")
        self.assertEqual(self.cli("zod", "exit")[0], A.EXIT_CONFIG)

    def test_down_and_refused(self):
        code, _, err = self.cli("zod", "dojo", "(add 2 2)")
        self.assertEqual(code, A.EXIT_DOWN, err)
        self.assertEqual(self.cli("zod", "dojo", "a\nb")[0], A.EXIT_REFUSED)
        self.assertEqual(self.cli("zod", "attach")[0], A.EXIT_DOWN)

    def test_classes_carry_their_codes(self):
        self.assertEqual(A.Timeout("x").exit_code, A.EXIT_TIMEOUT)
        self.assertEqual(A.Refused("x").exit_code, A.EXIT_REFUSED)
        self.assertEqual(len({n for n, _ in A.EXIT_CODES}), len(A.EXIT_CODES))

    def test_readme_lists_every_code(self):
        readme = (Path(A.__file__).parent / "README.md").read_text()
        for n, _ in A.EXIT_CODES:
            self.assertIn(f"| {n} |", readme)


if __name__ == "__main__":
    unittest.main(verbosity=1)
