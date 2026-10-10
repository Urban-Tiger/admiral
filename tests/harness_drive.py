#!/usr/bin/env python3
# The browser side of run.py's harness group: run under `admiral.py <ship> test`,
# which brings playwright. Prints one `pass <name>` or `FAIL <name>: why` per check.

import traceback

from admiral import with_ship

PAGE = """
<main>
  <h1>probe</h1>
  <button>save draft</button>
  <button>save</button>
  <input aria-label="title">
</main>
"""

# a line on the ship's console that CRASH_PATTERNS reads as a crash
CRASH_LINE = "~&  'bail: harness probe'  ~"


def raises(fn, needle):
    try:
        fn()
    except (AssertionError, RuntimeError) as e:
        assert needle in str(e), f"raised, but without {needle!r}: {e}"
        return
    raise AssertionError(f"did not raise ({needle!r} expected)")


def roles():
    with with_ship() as s:
        s.page.set_content(PAGE)
        s.expect_name("button", "save draft")
        raises(lambda: s.click_role("button", "save"), "2 controls match")
        s.click_role("button", "save", exact=True)
        s.fill_role("title", "hello")
        assert s.page.input_value("input") == "hello", "fill_role did not fill"
        raises(lambda: s.expect_name("link", "nowhere"), 'button "save draft"')
        assert 'heading "probe"' in s.a11y_tree(), s.a11y_tree()
        assert "no element" in s.a11y_tree("#absent")


def axe():
    with with_ship() as s:
        s.page.set_content('<main><img src="x.png"></main>')
        raises(lambda: s.assert_a11y(), "image-alt")
        s.page.set_content(f'<html lang="en"><title>probe</title><body>{PAGE}</body></html>')
        s.assert_a11y()


def console():
    with with_ship() as s:
        s.page.set_content("<p>probe</p>")
        s.console_tag("first")
        s.page.evaluate("console.error('widget exploded')")
        s.console_tag("second")
        s.page.evaluate("console.warn('Failed to load resource: 404')")
        s.page.evaluate("console.log('all fine')")
        s.page.evaluate("setTimeout(() => { throw new Error('boom') }, 0)")
        s.page.wait_for_timeout(300)
        seen = [(r["surface"], r["severity"], r["kind"]) for r in s.console_records()]
        assert seen == [("first", "error", "other"), ("second", "pageerror", "other")], seen
        raises(s.assert_console_clean, "other: 2")
        s.console_blame = {"widget": ("widget",)}
        s.console_fatal = {"widget"}
        assert [r["kind"] for r in s.console_records()] == ["widget", "other"]
        raises(s.assert_console_clean, "widget: 1")
        s.console_fatal = {"nothing"}
        s.assert_console_clean()


def window():
    # marks bracket exactly what the console printed between them
    with with_ship() as s:
        a = s.dojo.mark()
        out = s.dojo.run("~&  'alpha-line'  ~")
        assert "alpha-line" in out, out
        b = s.dojo.mark()
        s.dojo.run("~&  'beta-line'  ~")
        between = s.dojo.between(a, b)
        assert "alpha-line" in between and "beta-line" not in between, between
        assert "beta-line" in s.dojo.since(b), s.dojo.since(b)
        assert s.dojo.grep_between(a, b, [r"alpha-line"]) and not s.dojo.grep(b, [r"alpha-line"])
        s.dojo.assert_clean(b, [r"alpha-line"])
        raises(lambda: s.dojo.assert_clean_between(a, b, [r"alpha-line"]), "not clean")
        assert s.dojo.run("(add 2 2)").strip() == "4"


def crash_caught():
    def run():
        with with_ship(crash_check=True) as s:
            s.dojo.run(CRASH_LINE)
    raises(run, "harness probe")


def crash_absolved():
    with with_ship(crash_check=True) as s:
        s.dojo.run(CRASH_LINE)
        s.absolve()


def crash_unwatched():
    with with_ship() as s:
        s.dojo.run(CRASH_LINE)
        raises(s.absolve, "no crash check")


for check in (roles, axe, console, window, crash_caught, crash_absolved, crash_unwatched):
    try:
        check()
        print(f"pass {check.__name__}", flush=True)
    except Exception as e:
        why = f"{e!r}" if isinstance(e, AssertionError) else traceback.format_exc().strip().split("\n")[-1]
        print(f"FAIL {check.__name__}: {why}", flush=True)
