# Architecture

mcu is a handful of programs on one machine, the Python standard library and
podman, with no daemon of its own beyond the dashboard and the task worker.

```text
 project (ssh key) ──► sshd ──► mcu-job (forced command) ──► mcu.py entry      as mcu-<project>
                                                             └─ mcu.py run <id>  (detached runner, one per job)
                                                                 ├─ podman (rootless) ─► the job's containers
                                                                 └─ sudo mcu-helper ─► root: quotas, placement,
                                                                                       frequency cap, sftp view
 admin (browser) ──► api.py (HTTPS, port 8080)                                     as mcu-web
                       └─ sudo mcu-admin <fixed list> ─► root
 admin (shell)   ──► mcu-admin ──► root
 timers          ──► collect.py (each minute)                                       as mcu-web
                     mcu-helper usage (every 5 min), mcu.py prune (daily)           as root
 mcu-task.service ──► long admin operations (moves, reclaim, purge, shutdown)       as root
```

## Components

| | runs as | what it does |
|---|---|---|
| `mcu-job` → `mcu.py entry` | the project's user, `mcu-<project>` | the forced command of each project's SSH key: parses the command, refuses what the envelope forbids, queues jobs, answers status, fetch, queue, docs. An sftp request goes to the helper's read-only view |
| `mcu.py run <id>` | the project's user | one detached runner per job: waits for its dependencies, its turn in the queue (the scheduler is a function the runners call under a lock on `state.json`), builds the image, prepares the checkouts and storage, runs the containers, records the results and the cost |
| `mcu-helper` | root, through one sudo rule for the group `mcu` | the only root entry point for projects: quotas, unit placement on pools, the timing profile's frequency cap, the per-session sftp view. It takes the project from `SUDO_USER`, never from an argument, and checks every request against the envelope |
| `mcu-admin` | root (the admin, or the dashboard for a fixed list of subcommands) | install, invite and revoke, the envelope, pools, queue modes, maintenance, doctor, config show, uninstall |
| `api.py` | `mcu-web` | the dashboard's HTTPS API and its static files; reads the state, changes it only through `sudo mcu-admin` |
| `collect.py` | `mcu-web`, each minute | samples the machine into rrdtool databases, writes `resources.json` (the GPU, what the box can do now) and `hardware.json` |
| `mcu-task` | root, a service | runs long admin operations queued by the dashboard, one at a time, with progress and logs |
| `mcuconf.py`, `hw/` | (libraries) | the configuration's one merge and its defaults; the optional hardware's detection and actions |

## A job's life

1. `run build branch=main` arrives over SSH. `mcu.py entry` checks the
   Benchfile and the envelope, writes `jobs/<id>/request`, and starts a
   detached runner.
2. The runner waits for its dependencies (`after=`), then for its turn: the
   scheduler gives it threads and memory when they fit, and backfill lets it
   pass the jobs ahead only if it delays none of them.
3. It builds or reuses the image, prepares `/work` (checkouts from a per-project
   git cache) and mounts its storage, then runs the container with exactly
   the threads, memory, devices and network it was granted.
4. At the end it writes `status`, the summary of declared outputs, the cost
   (CPU, memory peak, I/O), and learns the job's duration and memory for the
   next estimates. `fetch` and the read-only sftp view read the results.

## State on disk

| path | owner | written by | what |
|---|---|---|---|
| `/etc/mcu/box.toml` | root | the admin, `envelope set`, the dashboard | the machine's settings, the only place each lives |
| `/var/lib/mcu/state.json` | group `mcu` | the runners (under `sched.lock`) | running, queued and dependency-waiting jobs |
| `/var/lib/mcu/events.jsonl` | group `mcu` | every component | the event log the dashboard and the estimates read |
| `/var/lib/mcu/projects/<p>.json` | the project | the project (`config`), the admin | the project's settings, Benchfile versions |
| `/var/lib/mcu/pools.json`, `timing.json`, `power.json` | root | the admin | the pools, the timing and power profiles |
| `/var/lib/mcu/idle.json` | root | the usage timer (`mcu-helper`) | the measured idle temperature, the auto cool-down target's base |
| `/var/lib/mcu/queue.json` | root | `mcu-admin queue` only (the dashboard, deploys and the shutdown task call it) | the queue's mode: open, paused or closed, why, until when, by whom |
| `/var/lib/mcu/memory.json`, `runs/` | group `mcu` | the runners | learned memory peaks, per-run cost sheets |
| `/home/mcu-<p>/jobs/<id>/` | the project | its runner | request, log, status, `out/`, summary |
| `/home/mcu-<p>/cache/<name>` | the project | its jobs | its storage (links to a pool) |
| `<pool>/<p>/` | the project | its jobs | its data on a pool, under an XFS project quota |

## Security boundaries

- **Between projects:** one Unix user each, homes mode 700, rootless podman
  (a container's root is the project's user), disk quotas per project. A
  project's key reaches only `mcu-job`, never a shell.
- **Project to root:** only `mcu-helper`, through one sudo rule; the project
  comes from the caller, arguments are validated, requests are checked against
  the envelope.
- **Dashboard to root:** only `sudo mcu-admin` with a fixed list of
  subcommands; the dashboard runs as `mcu-web`, behind the admin's password,
  over HTTPS.
- **Read-only view:** per sftp session, a private mount namespace with the
  project's own storage and results bound read-only, chrooted, `sftp-server -R`
  as the project.

`SECURITY.md` has the reporting process and the threat model.
