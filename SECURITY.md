# Security

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
("Report a vulnerability" in the Security tab). Don't open a public issue.

## The model, in brief

An mcu box is a dedicated machine shared by several projects.

- **Each project is a Unix user** (`mcu-<project>`) reached over SSH with its
  own key. The key gives no shell: a forced command runs the box's entry point,
  which only accepts the box's commands. sftp on the same key gets a read-only,
  chrooted view of that project's own storage and results, nothing else.
- **Jobs run in rootless podman containers** as the project's user, with the
  devices, network and memory the box's envelope allows, and nothing more.
- **Root is reached only through two programs**: `mcu-helper` (by the
  projects, through one sudo rule) and `mcu-admin` (by the admin, and by the
  dashboard for a fixed list of commands). Both take the project from the
  calling user, never from an argument, and validate everything else.
- **The dashboard** runs as an unprivileged user. Changes need the admin's
  password, and each one goes through `mcu-admin`'s fixed list of
  subcommands; it cannot call the helper itself.

The threat model, with what mcu does not try to do, is in
[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md).
