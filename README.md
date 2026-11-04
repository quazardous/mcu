<p align="center"><img src="brand/logo/mcu.svg" width="120" alt="the mcu logo: a black cube with lava glowing along its edges"></p>

# mcu

**mcu** (*multiple compute unit*) turns one strong Linux machine into a shared
job box. Several projects, people or AI agents, each with its own SSH key,
send it builds, test suites, benchmarks and GPU work. It runs them in rootless
containers with the threads, memory and devices each job declares, in a fair
queue, with timings you can compare from one run to the next.

It's for a lab, a team or one person with **a machine to dedicate** and several
projects that would otherwise fight over it, or fill a laptop's disk. It is
not a cluster scheduler and not a hosted CI service: one box, run by its
owner, used over SSH.

- **Projects manage themselves.** Once invited, a project writes its own
  `Benchfile` (image, checkouts, jobs, storage) and sets its own limits inside
  the box's envelope, with no approval step.
- **Fair and predictable.** Declared resources, backfill that never delays the
  jobs ahead, priorities within a project, dependencies between jobs, and
  estimates of when each job starts.
- **Heavy work stays on the box.** Build trees and caches persist between runs
  in the project's storage. Results are fetched, or read in place over sshfs.
- **An admin dashboard**: the queue and the machine live, projects, disks and
  pools, timing and power profiles, maintenance.

![The dashboard's home: each thread, the GPU and the machine as a lane, the last 48 minutes of jobs, the queue on the left, CPU, memory and power under it](docs/img/dashboard-home.png)

<sub>The dashboard in demo mode (`npm run demo`), with made-up projects.</sub>

## Quick start

**The admin**, on a dedicated Fedora machine (see [docs/INSTALL.md](docs/INSTALL.md)):

```sh
sudo box/mcu-admin doctor          # what the machine has and lacks, with the fixes
sudo make install                  # the files (the dashboard comes built in a release)
sudo box/mcu-admin init            # services, accounts, sudoers, the dashboard
sudo mcu-admin invite myapp        # prints myapp's private key, once
```

**A project**, with the key it was given (see [docs/USING.md](docs/USING.md)):

```sh
ssh -i myapp.key mcu-myapp@box docs agents             # the guide, on the box
ssh -i myapp.key mcu-myapp@box config benchfile < Benchfile
ssh -i myapp.key mcu-myapp@box run test branch=main    # queue a job, follow its log
```

## Documentation

| | |
|---|---|
| [docs/USING.md](docs/USING.md) | for projects: the key, the Benchfile, running jobs, results, limits, exit codes. Also on the box: `docs` |
| [docs/INSTALL.md](docs/INSTALL.md) | a dedicated box from scratch, upgrade, uninstall |
| [docs/ADMIN.md](docs/ADMIN.md) | running a box: projects, the envelope, disks and pools, queue modes, timing, backups |
| [docs/CONFIG.md](docs/CONFIG.md) | the settings and where each value comes from |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | the components, who runs as whom, the state on disk, the security boundaries |
| [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) | what mcu protects, from whom, how, and what it does not try to do |
| [docs/VM.md](docs/VM.md) | running mcu in a virtual machine, and what that costs |
| [docs/reference-box.md](docs/reference-box.md) | one real machine's figures, as a sizing example |

## Status

Pre-release (`0.1.0-dev`), used daily on one machine. Fedora only for now.

## Naming

The file format keeps its first name, the **Benchfile** (`config benchfile`),
like a Dockerfile. So do the markers it leaves in a project's storage
(`.bench-*`) and the `bench:` prefix of a job's log lines. Everything else is
`mcu`: the commands (`mcu-admin`, `mcu-job`), the users (`mcu-<project>`), the
paths (`/var/lib/mcu`, `/etc/mcu`) and the `MCU_*` variables (`BENCH_*` are
still read for one release).

## License

MIT, see [LICENSE](LICENSE). Third-party notices: [THIRD_PARTY.md](THIRD_PARTY.md).
Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
