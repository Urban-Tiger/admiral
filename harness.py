#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright", "pyte"]
# ///
#
# Playwright harness for ships admiral runs. admiral.py manages the ship; this file
# drives a browser against it and reads the ship's console back.
#
#   ./harness.py <ship> [--headed] file.py...   # run test files against a ship
#
# Each file runs as its own process with `--ship <name>` (and `--headed`) on its argv,
# and this directory on PYTHONPATH so it can `from harness import with_ship`. A bare
# name resolves under <repo>/.admiral/.
#
# Wrap a test in with_ship (authed) or with_anon (no login); both launch a browser,
# build the helper `s`, and always tear the browser down. The `s` object exposes raw
# Playwright handles (s.page/ctx/browser), s.goto/shot/post/assert_no_page_errors,
# and s.dojo to drive and read the ship's console (run/tail/mark/since/runlog/grep/
# assert_clean) — assert frontend with locators, backend with s.dojo.since(mark)
# against a mark taken before the action. A mark is the daemon ring's byte watermark
# (see ring_mark): free to take, nothing typed into the dojo, and the window it opens
# is exactly the bytes the console printed after it.
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
#   from harness import with_ship
#   with with_ship() as s:
#       mark = s.dojo.mark()
#       s.goto("/foo"); s.shot("foo")
#       s.dojo.assert_clean(mark, [r"crud", r"%bail"])

from __future__ import annotations

import os
import re
import sys
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from contextlib import contextmanager
from typing import Any

# admiral.py lives beside this file; resolve through any symlink a project points at us.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import admiral as adm  # noqa: E402

Pattern = Any  # str | compiled regex
SCRIPTS = f".{adm.NAME}"  # test/script dir under the project root


def argv_flag(name: str) -> str | None:
    # read `--flag value` from this process's argv (test options arrive as argv)
    try:
        i = sys.argv.index(name)
    except ValueError:
        return None
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else None


def harness_capture(ship: adm.Ship, cfg: adm.Config, lines: int = 200) -> str:
    host = adm.host_for(ship, cfg)
    return adm.tail_n(adm.capture_pane(host, ship, adm.LINE_BYTES * lines).rstrip("\n"), lines)


class Dojo:
    # the ship's console, for a test: run commands, and read what the console printed
    # between two marks. Marks are ring watermarks, so a window is a byte range of the
    # raw pty stream rendered through the same terminal emulator `slog` uses — a
    # concurrent writer's output lands in the window only if it printed inside it.
    def __init__(self, ship: adm.Ship, cfg: adm.Config):
        self.ship = ship
        self.cfg = cfg
        self.host = adm.host_for(ship, cfg)
        self.run_mark: int | None = None

    def run(self, cmd: str, timeout_secs: int | None = None) -> str:
        # type cmd into the dojo and return its transcript (the result, then every line
        # the ship printed while it ran). `-name` threads need no `--`: nothing parses
        # this as a flag.
        return adm.dojo_one(self.ship, cmd, self.cfg, timeout=timeout_secs or adm.T_DOJO, log=lambda m: None)

    def tail(self, n: int = 40) -> str:
        return "\n".join(harness_capture(self.ship, self.cfg, n).rstrip("\n").split("\n")[-n:])

    def mark(self) -> int:
        # the console's current byte watermark. Costs one socket ping; the first one a
        # test takes doubles as the run-start mark runlog()/grep() read from.
        total = adm.ring_mark(self.host, self.ship)
        if self.run_mark is None:
            self.run_mark = total
        return total

    def _raw(self, mark: int) -> str:
        # raw pty bytes past `mark`. The ring holds the last adm.RING_MAX bytes, so a mark
        # older than that is gone for good: a hard error, never a silently shorter window.
        total, raw = adm.capture_at(self.host, self.ship, mark)
        if total - mark > adm.RING_MAX:
            raise RuntimeError(f"dojo mark {mark} is {total - mark} bytes back; the daemon's "
                               f"{adm.RING_MAX // (1024 * 1024)}MB ring no longer holds it — read "
                               f"`{adm.INVOKE} {self.ship.name} slog` instead, or mark closer to the action")
        return raw

    def since(self, mark: int) -> str:
        # everything the console printed after `mark`, rendered
        return adm.render_transcript(self._raw(mark))

    def between(self, start: int, end: int) -> str:
        # what the console printed between two marks: the start window minus its tail
        # past `end`. Output a concurrent writer adds after `end` is excluded, so the
        # bracket a test draws around its own action stays its own.
        if end < start:
            raise RuntimeError(f"dojo between: end mark {end} precedes start mark {start}")
        head, tail = self._raw(start), self._raw(end)
        return adm.render_transcript(head[:len(head) - len(tail)] if tail else head)

    def runlog(self) -> str:
        # the console since the first mark this test took (taken now if it never did)
        if self.run_mark is None:
            self.mark()
        return self.since(self.run_mark)  # type: ignore[arg-type]

    @staticmethod
    def _hits(hay: str, patterns: list[Pattern]) -> list[str]:
        pats = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        return [l for l in hay.split("\n") if any(p.search(l) for p in pats)]

    def grep(self, mark: int | None, patterns: list[Pattern]) -> list[str]:
        return self._hits(self.since(mark) if mark is not None else self.runlog(), patterns)

    def grep_between(self, start: int, end: int, patterns: list[Pattern]) -> list[str]:
        return self._hits(self.between(start, end), patterns)

    def assert_clean(self, mark: int | None, patterns: list[Pattern]) -> None:
        hits = self.grep(mark, patterns)
        if hits:
            raise RuntimeError("dojo not clean:\n" + "\n".join(hits))

    def assert_clean_between(self, start: int, end: int, patterns: list[Pattern]) -> None:
        hits = self.grep_between(start, end, patterns)
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
        self._shots_dir = os.path.join(str(adm.REPO), SCRIPTS, "shots")
        self.console: list[dict] = []
        self.console_surface = "(untagged)"
        self.console_benign = CONSOLE_BENIGN
        self.console_blame: dict[str, tuple[str, ...]] = {}
        self.console_fatal: set[str] | None = None  # None: every kind fails
        self._crash_mark: int | None = None
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
        resp = opener.open(req, timeout=adm.T_RUN)
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
    cfg = adm.load_config()
    name = ship or argv_flag("--ship")
    if not name:
        raise RuntimeError(f"no ship to test against: pass --ship <name> (./harness.py <ship> file.py does)")
    ship_obj = adm.resolve_ship(name, cfg)
    if headless is None:
        headless = "--headed" not in sys.argv
    base = (base or adm.ship_base(ship_obj)).rstrip("/")
    code = adm.fetch_code(ship_obj, cfg) if auth else None

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
    # setup never types into the dojo: the code override skips the +code read, and a
    # mark is a socket ping, so a browser-only test stays sub-second.
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


# ── runner ────────────────────────────────────────────────────────────────────
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


USAGE = "Usage: ./harness.py <ship> [--headed] file.py..."


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(USAGE + "\n")
        return 0 if argv else 1
    headed = "--headed" in argv
    args = [x for x in argv if x != "--headed"]
    if len(args) < 2:
        sys.stderr.write(USAGE + "\n")
        return 1
    cfg = adm.load_config()
    ship = adm.resolve_ship(args[0], cfg)
    ensure_chromium()
    status = 0
    for t in args[1:]:
        file = t if os.path.exists(t) else os.path.join(str(adm.REPO), SCRIPTS, t)
        sys.stderr.write(f"=== running {file} on {ship.name} ===\n")
        pass_args = [file, "--ship", ship.name, *(["--headed"] if headed else [])]
        path = os.pathsep.join(x for x in [str(HERE), os.environ.get("PYTHONPATH")] if x)
        try:
            subprocess.run([sys.executable, *pass_args], check=True,
                           env={**os.environ, "PYTHONPATH": path})
        except subprocess.CalledProcessError:
            sys.stderr.write(f"=== FAILED: {file} ===\n")
            status = 1
    return status


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(1)
