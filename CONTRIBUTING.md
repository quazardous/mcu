# Contributing to mcu

Thanks for looking. mcu runs on a dedicated Linux machine (Fedora, tested on
44), so most changes are tested in two places: the unit tests here, then a box.

## Tests

```sh
make check     # the unit tests (scheduler, queue, dependencies, config, hardware on fake /sys trees,
               # docs topics: no root, no box) and the tracker-reference check
make lint      # ruff and shellcheck (through uvx when they are not installed)
```

CI (`.github/workflows/ci.yml`) runs both on every push and pull request,
builds the dashboard and runs `svelte-check` on it, and builds the release
tarball on a `v*` tag.

The box itself (`box/`) uses the Python standard library only. The dashboard
(`dashboard/`, Svelte) builds with `npm ci && npm run build`; `npm run demo`
serves it against made-up data, with no box behind it; the screenshots in
`docs/img/` come from it (1280×860, light theme), for example
`google-chrome --headless=new --window-size=1280,860 --screenshot=docs/img/dashboard-home.png http://localhost:5173/#/`
for the home page, `#/p/alpha/runs`, `#/machine/overview` and `#/settings/config` for the others.
To try a change on a
real box, install it from a checkout (`sudo box/mcu-admin init`) on a
dedicated machine or a VM.

## Style

- **Comments say why.** What the code does is in the code. A comment explains
  the reason, the constraint, or the case it handles, in full sentences.
- **English** in code, comments, docs and commit messages.
- **No references to an issue tracker** in code or commits. Say the reason in
  full instead. `scripts/check-refs` enforces it. Enable the hooks once with
  `git config core.hooksPath .githooks`.
- Match the code around you: its naming, its comment density, its idioms.
- No machine-specific values in the repository: they go in `*.local.toml` or
  `.env.local`, which git ignores.

## Commits

One change per commit, with a subject like `area: what changed, and why`.
The area is `box`, `dashboard`, `client`, `docs`, or similar.

## Proposing a change

Open an issue first for anything beyond a small fix, so we can agree on the
approach. Then open a pull request with tests where they apply, and say how you
tried it on a box.

## Security issues

Not in public issues: see [SECURITY.md](SECURITY.md).
