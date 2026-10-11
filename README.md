# admiral

An Urbit ship manager in a single Python file. Admiral runs your piers under a
small host-local daemon, drives the dojo over a socket, and syncs desk source
into clay, on this machine or on remote hosts over SSH.

```
./admiral.py status                      # one card per ship; `<ship>` alone is everything about one
./admiral.py <ship> run                  # start a ship (the daemon survives disconnects)
./admiral.py <ship> open                 # its web UI in your browser, signed in
./admiral.py <ship> dojo '(add 2 2)'     # run a dojo command and print its output
./admiral.py <ship> commit <desk>        # rsync a desk's source in and |commit it
./admiral.py <ship> attach               # a live dojo (Ctrl-\ detaches)
./admiral.py <ship> exit                 # stop it cleanly
```

## Requirements

- [uv](https://docs.astral.sh/uv/), which runs the script and installs its one
  dependency (`pyte`)
- `python3` (3.11+) and `rsync` on each host that runs a ship. The host-side
  engine uses only the standard library.
- the [`urbit`](https://urbit.org) runtime

## Getting started

```
curl -O https://raw.githubusercontent.com/Urban-Tiger/admiral/main/admiral.py
chmod +x admiral.py
curl -O https://raw.githubusercontent.com/Urban-Tiger/admiral/main/admiral.example.toml
cp admiral.example.toml admiral.toml   # then edit it: one [ships.<name>] per ship
./admiral.py help          # every command
./admiral.py help <cmd>    # everything about one command
```

Admiral finds its config by climbing from the current directory to the first
`admiral.toml` (or `.admiral.toml`). See `admiral.example.toml` for the schema.

## Playwright harness

`harness.py`, beside `admiral.py`, drives a browser against a ship admiral runs
and reads the ship's console back. It needs `playwright` as well as `pyte`
(declared in its own uv header).

```
./harness.py <ship> [--headed] file.py...   # run test files against a ship
```

A test is a python script:

```python
from harness import with_ship
with with_ship() as s:       # logged in with the ship's +code
    mark = s.dojo.mark()
    s.goto("/")
    s.shot("home")
    s.dojo.assert_clean(mark, [r"crud", r"%bail"])
```

`with_anon()` is the same without logging in. `s.page` is the Playwright page,
`s.dojo.run(cmd)` drives the dojo, and the file's header lists the screen-reader,
console, and crash-check helpers.

## Using it in another repo

To vendor admiral into a project, add it as a git submodule and symlink the
script to the project root:

```
git submodule add https://github.com/Urban-Tiger/admiral.git tools/admiral
ln -s tools/admiral/admiral.py admiral.py
ln -s tools/admiral/harness.py harness.py   # only if you use the harness
```

## Tests

Two suites:

- `uv run tests/unit.py` — the pure functions (argument parsing, commit
  verdicts, prompt parsing, terminal rendering, config load/write, the engine's
  framing), no ship needed. CI runs these on every push.
- `tests/run.py <ship>` — the live suite against a running ship: the dojo
  driver, commits, attach, and the Playwright harness. The file's header
  explains how to set up the fixture desk in `tests/desk`.

## License

MIT
