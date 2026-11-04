# Releasing mcu

Versions follow semver; 0.x means the formats may still change. The
Benchfile has its own format number (`format = 1`), unchanged within a major
version.

## Each release

1. Move the `[Unreleased]` notes in `CHANGELOG.md` under `## [x.y.z] - YYYY-MM-DD`.
2. `make release VERSION=x.y.z`: it checks the tree is clean, the version is
   new and the CHANGELOG has its section, runs `make check`, sets `VERSION`,
   commits and tags `vx.y.z`.
3. `git push origin main vx.y.z`. CI builds the tarball (the dashboard built
   in), its `SHA256SUMS`, and a GitHub release whose notes are that
   CHANGELOG section.

## Before the first public release

- [ ] the public-release tickets done: content cleanup, licence, the name,
      install, configuration, optional hardware, tests and CI, docs, the
      security review, the dashboard
- [ ] the docs reread start to end, every command run
- [ ] `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md` present
- [ ] the scan: `scripts/check-refs`, no hostname, LAN address or private
      project name, no secret, over the tree and over the initial commit
- [ ] screenshots from the dashboard's demo mode, current
- [ ] the maintainer's own box upgraded from the release tarball itself (not a
      checkout) and running fine for a few days
