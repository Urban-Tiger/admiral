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
import struct
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admiral as A  # noqa: E402
import harness as H  # noqa: E402


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
    desk = A.Desk("admiral-test")

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


class DojoText(unittest.TestCase):
    def test_prompt_state(self):
        self.assertEqual(A.prompt_state("~zod:dojo> "), ("idle", ""))
        self.assertEqual(A.prompt_state("~zod:dojo< "), ("open", ""))
        self.assertEqual(A.prompt_state("~zod:dojo: nuke? (y/N)"), ("ask", "nuke? (y/N)"))
        self.assertEqual(A.prompt_state("~zod:dojo> (add 2 2)\r\n4\r\n~zod:dojo> x"), ("idle", " x"))
        self.assertIsNone(A.prompt_state("no prompt here"))

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
        with self.assertRaises(SystemExit):
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

    def test_report_routes_streams(self):
        import contextlib
        results = [A.FanResult("a", True, "note", value="out\n"), A.FanResult("b", False, "", err="boom"),
                   A.FanResult("c", True, "", value="")]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = A.report(results, header=sys.stdout)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "=== ship: a ===\nout\n=== ship: b ===\n=== ship: c ===\n")
        self.assertEqual(err.getvalue(), "note\nboom\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(A.report(results[:1]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("out\n", "note\n"))

    def test_vere_argv(self):
        ship = A.Ship("zod", A.PierConfig(path="/p"))
        self.assertEqual(A.vere_argv(ship, FakeHost({"test": OSError()}), "roll", "--loom", "31"),
                         ["urbit", "roll", "--loom", "31", "/p"])
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
                A.offline(ship, host, calls.append, "chop", False, op)
            self.assertIn("chop failed, restarted (daemon 3 child 4): disk full", str(cm.exception))
            self.assertEqual([c for c in calls if c in ("stop", "op", "start")], ["stop", "op", "start"])
            calls.clear()
            self.assertEqual(A.offline(ship, host, calls.append, "next", True, lambda: "ok"), "ok")
            self.assertNotIn("start", calls)
        finally:
            A.pier_exists, A.daemon_status, A.daemon_stop, A.daemon_start, A.assert_port_match = saved

    def test_running_http_port(self):
        host = FakeHost({"ps": "/p/.run --loom 31 --http-port 8080 /p\n"})
        self.assertEqual(A.running_http_port(host, 1), (True, 8080))
        self.assertEqual(A.running_http_port(FakeHost({"ps": "urbit /p"}), 1), (True, None))
        self.assertEqual(A.running_http_port(FakeHost(), 1), (False, None))

    def test_run_argv(self):
        ship = A.Ship("zod", A.PierConfig(path="/p/zod", port=8080, loom=31, ames=31337, local=True))
        host = FakeHost({"test": OSError()})  # no <pier>/.run on the host
        self.assertEqual(A.run_argv(ship, host),
                         ["urbit", "-L", "--loom", "31", "--http-port", "8080", "-p", "31337", "/p/zod"])

    def test_sock_path_limit(self):
        host = A.Host("local", "/tmp")
        self.assertEqual(A.sock_path(A.Ship("zod", A.PierConfig(path="p")), host), "/tmp/p/.admiral.sock")
        with self.assertRaises(RuntimeError):
            A.sock_path(A.Ship("zod", A.PierConfig(path="p" * 120)), host)


class ConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text):
        p = self.dir / "admiral.toml"
        p.write_text(text)
        return A.load_config(p)

    def test_minimal(self):
        cfg = self.load("[ships.zod]\n")
        self.assertEqual(cfg.ships[0].pier, A.PierConfig())
        self.assertEqual(cfg.desks, [])

    def test_rejections(self):
        cases = {
            "": '"ships" must be',
            "[ships.zod]\npier = { prot = 1 }": "unknown key",
            "[ships.zod]\npier = { port = 70000 }": "1-65535",
            "[ships.zod]\npier = { local = 1 }": "boolean",
            "[ships.zod]\ncode = 'nope'": "+code",
            "[ships.zod]\nhost = 'ghost'": 'not in "hosts"',
            "[hosts.local]\nssh='a'\nroot='/r'\n[ships.zod]": "reserved",
            "[hosts.b]\nssh='a'\nroot='rel'\n[ships.zod]": "absolute",
            "[ships.zod]\nmnemonic = 'one'": "word list",
            "[ships.zod]\n[[desks]]\nname = 'Bad'": "must match",
        }
        for text, msg in cases.items():
            with self.assertRaises(RuntimeError, msg=text) as cm:
                self.load(text)
            self.assertIn(msg, str(cm.exception), text)

    def test_login_page_posts_to_eyre(self):
        page = A.login_page("https://x.urbit.house", "lidlut-tabwed-pillex-ridrup", "/apps/x")
        self.assertIn('action="https://x.urbit.house/~/login"', page)
        self.assertIn('name=password value="lidlut-tabwed-pillex-ridrup"', page)
        self.assertIn('name=redirect value="/apps/x"', page)
        self.assertIn('"', A.login_page("h", 'a"b', "/"))

    def test_persist_is_a_pier_bool(self):
        cfg = self.load('[ships.zod]\npier = { port = 1, persist = true }\n')
        self.assertTrue(cfg.ships[0].pier.persist)
        cfg = self.load('[ships.zod]\npier = { port = 1 }\n')
        self.assertFalse(cfg.ships[0].pier.persist)
        with self.assertRaises(RuntimeError):
            self.load('[ships.zod]\npier = { persist = "yes" }\n')

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
    cfg = A.Config(ships=[A.Ship("zod", A.PierConfig()), A.Ship("nec", A.PierConfig(), host="drain")],
                   desks=[A.Desk("base"), A.Desk("other")], hosts={"drain": A.Host("drain", "/r", "u@b")})

    def test_ships(self):
        self.assertIs(A.resolve_ship("~zod", self.cfg), self.cfg.ships[0])
        with self.assertRaises(RuntimeError) as cm:
            A.resolve_ship("bus", self.cfg)
        self.assertIn("zod, nec", str(cm.exception))
        self.assertEqual(A.select_ships(None, self.cfg), self.cfg.ships)

    def test_desks(self):
        self.assertIs(A.resolve_desk("", self.cfg), self.cfg.desks[0])
        with self.assertRaises(RuntimeError):
            A.resolve_desk("nope", self.cfg)
        with self.assertRaises(RuntimeError):
            A.resolve_desk("x", A.Config([], [], {}))

    def test_hosts(self):
        self.assertIs(A.host_for(self.cfg.ships[0], self.cfg), A.LOCAL)
        h = A.host_for(self.cfg.ships[1], self.cfg)
        self.assertEqual((h.name, h.ssh, h.root), ("drain", "u@b", "/r"))
        self.assertEqual(A.pier_dir(self.cfg.ships[1], h), "/r/.piers/nec")
        self.assertEqual(A.pier_dir(A.Ship("x", A.PierConfig(path="/abs")), h), "/abs")

    def test_ship_base(self):
        self.assertEqual(A.ship_base(A.Ship("z", A.PierConfig(), http="example.com/")), "https://example.com")
        self.assertEqual(A.ship_base(A.Ship("z", A.PierConfig(), http="http://l:80")), "http://l:80")
        with self.assertRaises(RuntimeError):
            A.ship_base(A.Ship("z", A.PierConfig()))

    def test_sync_flags(self):
        flags = A.sync_flags()
        self.assertIn("--exclude=.*", flags)
        self.assertIn("--exclude=admiral.toml", flags)
        self.assertIn("--delete", flags)

    def test_usage_strings(self):
        c = cmd([A.Flag("-t", "int", "", ph="<secs>"), A.Flag("--yes", "bool", "")],
                [A.Pos("CMD", True, ""), A.Pos("more", False, "", variadic=True)])
        self.assertEqual(A.usage_line(c), "Usage: ./admiral.py <ship> x [-t <secs>] [--yes] <CMD> [more...]")


if __name__ == "__main__":
    unittest.main(verbosity=1)
