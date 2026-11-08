# Using an mcu box

**mcu** stands for *multiple compute unit*: a shared machine that runs
several projects' jobs in containers, with declared resources, a fair queue
and comparable timings. A machine running it is an **mcu box**.

An mcu box runs your project's jobs (builds, tests, benches, GPU work) in
fresh rootless containers, away from your development machine. Your project
manages itself: once invited, nobody needs to approve your settings as long
as they fit in the box's envelope (`config show` prints it).

## What to send to the box, what to keep local
<!-- topic: when -->

The box pays off for **heavy work whose outcome you already expect**. Heavy
counts two ways: in time (CPU, a long run) and in disk (what the work leaves
behind). A run costs a queue wait, an image, a checkout and a copy back.
That's worth it when the job is long, or fills gigabytes of build trees and
caches, and you know it will most likely pass, or when you need the answer to
be comparable:

- full builds and release builds, on code that already compiles locally or
  passed a quick check;
- complete test suites, gates before a merge, timing runs and benches;
- GPU work, large corpora, anything your machine can't hold or shouldn't
  spend an hour on;
- work with a big footprint: release and debug `target/` trees of several
  GB, sccache, package registries, datasets, container images. They live in
  your storage on the box (`docs storage`), not on your disk. You read
  results in place (`docs view`) or fetch only the artefact you need.

It's the wrong tool for the **fast, failure-prone loop** of early work:
exploring, an alpha, a refactor in progress. There, most attempts fail within
seconds, and the box's round trip outweighs the work. Keep these local:

- syntax and type checking: `cargo check`, `cargo clippy` on a few crates,
  `tsc --noEmit`, a linter;
- compile-to-see-the-next-error cycles, and a single test you rerun again and
  again;
- anything you'd run on every save;
- light work: small, with no large local cache or build tree to keep.

Once that loop settles, and the code compiles and the narrow tests pass, send
the heavy part to the box. A rule of thumb: if you'd be surprised by a
failure, send it; if you expect one, fix it locally first. And if it would
fill your disk, it belongs on the box.

## What the box keeps between runs
<!-- topic: persistence -->

Heavy work only pays off if its build trees and caches survive from one call
to the next. That is part of the contract: **your storage persists between
runs, and its layout is yours.** You decide its names, its subs keyed on your
parameters (`target/${branch}`), what to lock, what to seed from main, and
which disk it goes on (`media`). See `docs storage`.

What the box guarantees for every `[storage]`, within your quota
(`config disk`, up to the envelope's `max_disk`):

- **Same path, same content.** Every job that mounts it sees it at the path it
  asks for, as the last job left it. An admin moving it to another disk copies
  it, and the path doesn't change. It is readable in place from your machine
  (`docs view`).
- **Removed or trimmed only by the rules you declared:** `drop`, `until`,
  `ttl`, `max` with your `trim` (`docs storage`). A `tmp = false` storage goes
  in no other way, except your own `storage rm` or an admin `purge`. A purge
  is refused while your jobs are queued or running, and your agent is told.
- **`tmp = true`** (the default) says the storage regenerates. On top of your
  rules, the box may evict its least recently used subs when your quota runs
  short before a job, never one a queued or running job mounts, and logs each
  eviction. An admin `reclaim` may empty it, and your agent is told. Declare
  `tmp = false` for what is costly to rebuild.
- Until 2026-11-03, a storage may still state no end. Its per-parameter subs
  unused for the retention (14 days) are removed daily, whatever `tmp` says.
  State an end to keep them by your own rule.

Not guaranteed: **there is no backup.** A failed disk loses the storage on it.
Keep sources of truth (code, datasets you can't fetch again) elsewhere. The
box is a workspace and a cache, not an archive.

## 0. For agents
<!-- topic: agents -->

Before a job, check it is worth a run (`docs when`): heavy in time or in disk
(big build trees, caches), and expected to pass. Light checks and the
edit-compile loop of early work stay on your machine. Your storage persists
between runs, laid out as you choose (`docs persistence`).

Everything goes through your project's key:
`ssh -i <key> -o IdentitiesOnly=yes mcu-<project>@<box> <command>`. There is
no shell. This guide is on the box: `docs --list` gives its topics, `docs
<topic>` one section, at the version the box runs. A refusal ends with the
topic that explains it (`...; see: docs storage`).

The five commands you need:

```sh
config show                     # your settings, the envelope, the Benchfile in force (in_force: its jobs)
run <job> [key=value...]        # queue a job and follow its log; exit = the job's
status <id> | wait <id>         # a job's state and summary; follow one already queued
fetch latest <job> [key=value...] <output> > out.tar   # an output of the last successful run
queue                           # what runs and what waits, with expected times
```

A minimal Benchfile (`config benchfile < Benchfile`; on a branch,
`config benchfile <branch> < Benchfile`, or every branch loses the jobs
yours lacks):

```toml
[image]
from = "registry.fedoraproject.org/fedora:44"
run = ["dnf -y install git-core gcc"]

[fetch]
run = ["git clone -q --depth 1 -b ${branch:-main} https://github.com/you/app /work/app"]

[jobs.build]
about = "the release build"
cwd = "/work/app"
command = "make && cp app /out/"
cores = 4
memory = "4g"
outputs = { app = { path = "app", kind = "file" } }
```

Getting the binary back: `fetch latest build branch=main app | tar -x`.
Or read results in place, no copy: `sshfs mcu-<project>@<box>:` with the same key (`docs view`).

## 1. Your key
<!-- topic: key -->

The box's admin invites your project and hands you a private key, your API
key, shown once. It can live **anywhere** you like, as long as only you can
read it (mode 600) and it never enters a repository: `~/.ssh/` is not
required. For example, in a git-ignored file of your project:

```sh
install -m 600 mcu-<project>.key .secrets/mcu.key     # with .secrets/ in .gitignore
```

Every command below is `ssh -i <your key> mcu-<project>@<box> <command>`;
add `-o IdentitiesOnly=yes` if your ssh agent holds other keys. With
`client/remote.sh`, set `MCU_KEY=<path>` in `client/.env.local`. The key
gives no shell: it reaches the box's job entry point and nothing else.

## 2. Configure your project
<!-- topic: config quotas volumes -->

```sh
config show                          # your settings, the envelope, and in_force: the default
                                     # Benchfile's version, upload time, source, jobs, storages
docs [--list | <topic>]              # this guide, on the box (help: every command)
config repo https://host/owner/repo  # where the box fetches your branches (https)
config manifest tools/bench/bench.toml   # where your manifest is in the repo (default)
config disk 40g                      # your disk quota, unless your Benchfile sets it (default: the box's default_disk)
config disk hdd 300g                 # room on the box's spinning disks, if it has some
config usage                         # what fills your quota (each pool type's, ssd and hdd): images, caches, src, jobs
config clean images                  # drop the images nothing needs now (works while jobs run; says what it kept)
config clean cache <name>[/<sub>]    # drop a cache, or one per-branch tree of it
config clean src                     # drop the repository-mode checkout, once on a Benchfile
volume create <name> [project...]    # a shared volume you fill; the projects listed read it
volume readers <name> [project...]   # change who reads it
volume list                          # your volumes and those you read
```

A request above the envelope is refused with the limit in the message.

Building your image is a job of the box too: when a job needs an image
that does not exist yet, an "image build" is queued for your project, given
its own threads (`build_cores` in the envelope, 4 on the reference box) and the memory
cap, and shown on the dashboard, in `health` and in other jobs'
`conditions.json`; your job starts once it is built. It never runs beside a
job holding `machine`.

From 80 % of your quota, `run`, `debug` and `box` warn you (`disk_warning`
in `box`'s JSON). Before an image build, the box drops the images no Benchfile of yours uses
any more and checks room as for a job (what your builds are known to take;
evicting unused tmp storage, or exit 75 with the figures). Each image build
drops your other images afterwards. Kept: the default Benchfile's image,
those of the Benchfiles your queued or running jobs use, and those of
branch Benchfiles used within the last day. An older branch keeps its
Benchfile; its image is rebuilt when a job uses it again. Images live in
your quota like your storage: one image per branch adds up fast. Also,
jobs that need the same new image wait for one build instead of each making
their own. A build that fails on the quota says so in the job's log.

## 3. Your Benchfile (the usual way)
<!-- topic: benchfile outputs fetch -->

One declarative file: the environment, where the code comes from, the jobs.
The box knows no repository; your Benchfile clones whatever it needs, so
several repos, submodules or package registries are all your business.

```sh
config benchfile < Benchfile     # stores a new version; each run records which one
config benchfile from=main@3f2a9c1 < Benchfile   # records where it came from (config show)
config file bwrap < bwrap        # a small file (1 MB max) [image] can copy: a script
                                 # (#!) or an ELF binary lands as 755, anything else 644
push mycode < code.tar           # local, uncommitted code at /work/mycode for the next
                                 # jobs; an empty tar removes it
```

```toml
[image]                          # built with network, cached until this changes
from = "registry.fedoraproject.org/fedora:44"
run = ["dnf -y install git-core rust cargo", "cargo install cargo-nextest --locked"]
copy = { "bwrap" = "/usr/local/bin/bwrap" }
env = { RUST_BACKTRACE = "1" }

[fetch]                          # with network, before each job, into a fresh /work
run = ["git clone -q --depth 1 -b ${branch:-main} https://github.com/you/app /work/app",
       "git clone -q --depth 1 -b ${lib:-main} https://github.com/you/lib /work/lib",
       "cd /work/app && cargo fetch --locked"]

[storage]
cargo = "the cargo registry, kept between jobs"

[jobs.test]
about = "the workspace tests"     # required: one line, shown beside the job's name
cwd = "/work/app"
command = "cargo nextest run --offline"
cores = 4
memory = "4g"
mounts = { cargo = "/root/.cargo/registry" }
```

Every job says what it does in `about`: one line of at most 120 characters.
The dashboard and `jobs` show it beside the job's name. A Benchfile with a
job that has none is refused at upload (the stored one keeps running).

**One Benchfile per branch.** Several agents on different branches would
otherwise overwrite each other's jobs: give each branch its own.

```sh
config benchfile feat-x < Benchfile   # feat-x's Benchfile, beside the default one
config benchfile rm feat-x            # refused while one of its jobs is queued or running
remote.sh --benchfile                 # the same from a checkout: the current branch's,
                                      # or the default one on main/master
```

A job uses the Benchfile named like its `branch=`, else the default one;
`benchfile=<name>` picks one explicitly. Its log and history say which
(`Benchfile feat-x v3`). Each Benchfile has its own image. What applies to the
whole project is taken from all the Benchfiles in use: the largest
`[project] disk`, every declared storage, every volume reader; so an upload
from main never takes away what a branch still needs. A branch's Benchfile
that no job has used within the retention (14 days) is removed; the default
one never is. `config show` lists them (`benchfiles`: version, stored, last
use).

Your project's own setup can live in the Benchfile too, applied at each
`config benchfile` within the envelope (above it, the whole upload is
refused and nothing is stored):

```toml
[project]
disk = "40g"                     # your quota; while a Benchfile in use sets it, `config disk` refuses
# disk = { ssd = "40g", hdd = "300g" }   # where the box has spinning disks: a quota per pool type

[volumes.raw-corpus]             # a volume you own: created on upload, readers kept in sync
about = "raw regression corpus"
readers = []                     # other projects that may mount it read-only
provision = "tools/bench/fill-corpus.sh /volume"   # run by `provision raw-corpus`, with network
```

The upload prints what changes: jobs added, removed or changed, the quota,
volumes created or their readers, whether the image will be rebuilt, and the
older spellings with their current form. A volume you stop declaring is kept:
an upload never deletes data.

**The format.** A Benchfile may say `format = 1` at the top; without it, it
is format 1. Within a format, a Benchfile that worked keeps working on newer
boxes. A box refuses a format newer than it reads, and says which version of
mcu it runs.

Run it with parameters, which fill the `${...}` (values limited to
`[A-Za-z0-9._/@+-]`):

```sh
run test branch=my-feature lib=fix-x
```

Each run records the parameters and the commit of every git repository found
under `/work` after the fetch.

### Large repositories: `[checkout]`
<!-- topic: checkout -->

Cloning a big repository in `[fetch]` at every job is slow. A `[checkout]`
keeps one cached clone per repository for your project, fetches only what
is new, and gives each job its own copy at `/work/<name>` (a local clone
whose objects are shared with the cache, so it costs little), submodules
included, from the cache too. Jobs on different branches run side by side,
each on its own commit, and `git describe` works inside the container.

```toml
[checkout.app]                   # -> /work/app
repo = "https://github.com/you/app.git"
ref = "${branch:-main}"          # a branch, a tag or a commit
submodules = true                # https submodules, recursively
at = "/build/app"                # optional: also seen there, always the same path

[storage]
ccache = "a plain storage: shared by every job"
build = { about = "build trees", lock = true }
corpus-index = { about = "slow to redo", tmp = false }

[jobs.test]
mounts = { "build/${branch:-main}" = "/target", ccache = "/ccache" }
checkouts = ["app"]              # optional: only these; the others are neither
                                 # fetched nor mounted for this job
```

**Seeded sub-directories.** A storage with per-branch sub-directories can
start each new one from an existing one, so a new branch's build begins warm
instead of from scratch:

```toml
[storage]
target = { about = "cargo targets, one per branch", lock = true, seed = "main" }
```

The first time a job mounts `target/wip-x`, it starts as a copy of
`target/main`: a reflink copy where the disk allows it (XFS, btrfs: instant,
and the blocks stay shared until rewritten), a plain copy elsewhere.

While a running job writes the seed, a new sub copies the seed's
**snapshot** instead: `target/.seed-main`, a reflink copy the box takes each
time a job that mounted the seed ends well (on a pool with reflink only).
`storage list` shows it, it goes with its seed, and nothing mounts it.
Without a snapshot yet, the new sub starts empty.

The quota counts a copy in full. When the room left on its pool is short,
the box first evicts your least recently used tmp subs there (never a seed,
its snapshot, or a sub a queued or running job mounts). If even that is not
enough, the new sub starts empty, and the job's log says so ("not seeded: …
doesn't fit in your hdd quota").
The box never evicts a seed to make room (only your `storage rm` drops it):
without it, every new branch would start cold.

**A compile cache shared by every branch** needs nothing from the box: a plain
storage (no `lock`, so jobs share it) and the tool's environment, e.g. for
Rust with sccache:

```toml
[image]
env = { RUSTC_WRAPPER = "sccache", SCCACHE_DIR = "/sccache", SCCACHE_CACHE_SIZE = "15G" }
[storage]
sccache = "compiled crates, shared by every branch"
[jobs.test]
mounts = { "target/${branch:-main}" = "/target", sccache = "/sccache" }
```

It saves time, not room: each branch's target still holds its own
artifacts. To keep targets small, `CARGO_INCREMENTAL=0` for jobs that don't
iterate, and lighter debug info (`[profile.dev] debug = "line-tables-only"`).

**Disk type.** Where the box has both SSD and spinning disks, a storage, a
volume or a job's `/work` can ask for one: a storage rewritten at every run
(rendered frames, big intermediate files) wears an SSD out; a spinning disk
takes it.

```toml
[storage]
frames = { about = "rendered frames, rewritten each run", media = "hdd" }
ccache = { about = "compiler cache", media = "ssd" }
[volumes.corpus]
media = "hdd"
[jobs.render]
work_media = "hdd"               # its disposable /work too
```

Each pool type has its own quota: `ssd` (the `disk` quota, as always) and
`hdd` (none until you or the admin set one: `config disk hdd <size>`, or
`[project] disk = { ssd = …, hdd = … }`). `box`, `config usage` and the
warnings give the use per type. A `media = "hdd"` unit needs an hdd quota;
without one it stays on ssd (and `hdd!` is refused).

`media` is `ssd` or `hdd`: a preference, so the same Benchfile works on a box
with a single kind of disk (the upload says so, and it stays on the usual
pool); `hdd!` or `ssd!` insists (refused with exit 78 where there is none).
A storage or volume already placed elsewhere gets a queued move at the upload
(jobs using it wait for it, as for any move). Machine → Overview shows each disk's
wear (an SSD's percentage used and TB written, a hard disk's reallocated
sectors), and Machine → Stats its curve.

Without `checkouts`, a job gets every `[checkout]`. A checkout whose fetch
fails on the network (connection refused, DNS, timeout) is tried once more
after 5 s; a missing branch or ref fails at once.

**Outputs and the summary.** A job may declare what it leaves in `/out`:

```toml
[jobs.test]
outputs = { junit = { path = "junit.xml", kind = "junit" },
            snapshots = { path = "snapshots/", kind = "images" } }
```

Kinds: `junit`, `images`, `json`, `log`, `file`. At the end of the job the
box writes a summary beside its status: a JUnit's passed, failed and skipped
counts and the first failing tests with their message; for a failed job
without a JUnit, the first error lines of its log. `status <id>` prints it
(`--json` for clients), `list` shows it in brief, and `fetch <id> <output>`
fetches that output alone (`fetch <id>` everything). `fetch latest <job>
[key=value...] <output>` fetches it from the latest successful run of that job
with those parameters (e.g. `fetch latest build-gui branch=main gui`), with no
job id to remember; the id goes to stderr. A job's results stay on
the box for the retention (14 days by default), reachable by id even when the
client was cut off.

**Sizing your jobs.** `box` gives the envelope in its JSON (`envelope`): the
threads and memory all jobs share (on the reference box: 10 of 12 threads,
one core being kept for the system, and about 14.3 GB; see
`docs/reference-box.md`), the most one job may ask for, and how
many jobs of one project run at once. A job asking for more than half of
either never runs beside another one; `config benchfile` warns about it. A
waiting job says what it lacks and who holds it ("needs 8 threads, 2 free
(held by myapp/lint 5, other/pixel 3)").

### Running a GUI app headless (wbox)
<!-- topic: gui -->

Every job has `XDG_RUNTIME_DIR` set to an empty private tmpfs (Wayland
compositors and clients refuse to start without one); set it in `env` to
use your own. A ready-made image and two jobs for
[wbox-mcp](https://github.com/quazardous/wbox-mcp) (labwc headless,
screenshots), tested on the reference box on both GPUs. wbox-mcp is not on PyPI: it comes
from a `[checkout]` and runs through `PYTHONPATH`.

```toml
[image]
from = "registry.fedoraproject.org/fedora:44"
run = ["dnf -y install --setopt=install_weak_deps=False labwc grim xdotool setxkbmap wlr-randr wlrctl xorg-x11-server-Xwayland xclip wl-clipboard python3 python3-pip python3-pyyaml mesa-dri-drivers mesa-vulkan-drivers mesa-libEGL mesa-libgbm egl-gbm egl-wayland && dnf clean all",
       "pip install --no-cache-dir 'mcp>=1.0'"]

[checkout.wbox]
repo = "https://github.com/quazardous/wbox-mcp.git"
ref = "${wbox:-main}"

[jobs.ui-intel]                  # the Intel iGPU, shared
resources = ["render"]
env = { PYTHONPATH = "/work/wbox/src", WLR_RENDERER = "gles2" }
command = "your-scenario"        # starts the app in wbox, writes screenshots to /out

[jobs.ui-nvidia]                 # the NVIDIA GPU, one job at a time
resources = ["gpu"]
display = "gpu"
env = { PYTHONPATH = "/work/wbox/src", WLR_RENDERER = "gles2" }
command = "your-scenario"
```

`WLR_RENDERER` in `env` works with any wbox; from wbox-mcp 0.11.0 you can
put `renderer: gles2` in wbox's config.yaml instead. Drop it for `pixman`
(software, the most reproducible pixels). Whether `egl-gbm` / `egl-wayland` are strictly needed beside the
NVIDIA CDI libraries is not measured; they are kept as a precaution.

A full screenshot job (an egui app's `cargo xtask screenshots`, built in the
job) taught these lessons:

- labwc aborts without Xwayland, and wbox calls `setxkbmap`: keep
  `xorg-x11-server-Xwayland` and `setxkbmap` in the image even for a
  Wayland-only app.
- Install fonts (`google-noto-sans-fonts dejavu-sans-fonts fontconfig`): a
  bare Fedora image renders text with whatever it finds, or nothing.
- `/out` is its own mount: a tool that moves its screenshot there with a
  rename (`os.replace`, `rename(2)`) fails with `EXDEV`; copy, or move
  with `shutil.move`.
- With `WLR_RENDERER=pixman`, two runs in a row gave byte-identical PNGs.
- Keep the cargo registry and `target/` in `[storage]`: the second run
  skips the build.

**GTK apps need `sandbox = true`.** On Fedora, gdk-pixbuf loads images
(even icons) through glycin, which runs each loader under `bwrap`. bwrap
mounts a fresh `/proc`, and a job's container refuses that by default, so a
GTK app aborts at startup with `Failed to load …: Loader process exited early
with status '1'`. Set `sandbox = true` on a job that runs bwrap, or any other
sandbox. Its container then gets the SELinux type made for containers that
run containers (still confined), with `/proc` unmasked. It stays rootless, in
your project's user. Its command runs as the container's PID 1, without an
init (podman's is not allowed under that type). Leave it off for every other job.

### Services: a database or a web server beside a job
<!-- topic: services -->

A job can list services: containers that start before it, share its
loopback (`127.0.0.1`, any port), and are removed when it ends.

```toml
[storage]
db = { about = "MariaDB datadir, kept between jobs", keep = true, max = "40g", lock = true }

[services.db]
about = "MariaDB 10.11"                               # required, one line
image = { from = "docker.io/library/mariadb:10.11" }  # like [image]: from, run, env, copy
mounts = { db = "/var/lib/mysql" }                    # storage (rw) or volumes (ro)
env = { MARIADB_ALLOW_EMPTY_ROOT_PASSWORD = "1" }
ready = "mariadb-admin ping -h 127.0.0.1 --silent"   # the job starts once it succeeds
ready_timeout = "5m"                                  # default 2m
memory = "4g"                                         # default 1g, a hard limit

[services.php]
about = "php-fpm on the job's image"                  # no image: the job's own
command = "exec php-fpm -F"                           # needed then; `exec` passes signals
tmpfs = ["/var/log/php-fpm"]                          # writable, in memory

[jobs.upgrade]
about = "the shop under PHP 7.1"
services = ["db", "php"]
command = "tools/scenario.sh"
```

- **Start:** the box creates the job's pod, then starts each service. It waits
  for each one's `ready` (retried every second) before the job runs. A service
  that exits, or is not ready in time, fails the job with exit 3, and the
  job's command never runs.
- **End:** the box removes the pod and every service whatever ended the job:
  success, failure, timeout or cancel. Each service's output is kept in
  `/out/services/<name>.log`, part of the results. A pod left by a runner
  that was killed is removed at the project's next job, and by the daily
  housekeeping. Containers never persist; only storage does.
- **Disk:** a service's rootfs is read-only. It writes only to its storage
  mounts, to `/work` and `/out` (the job's, which it sees too), and to its
  tmpfs (`/run`, `/tmp`, `/var/tmp`, its `tmpfs` list). Its storage counts in
  your quota like any other. The tmpfs count in its memory. A write past the
  quota fails with "No space left on device", and the job's log says where
  your quota went. A full quota still ends the job cleanly. The services are
  killed (through their cgroup, when podman itself has no room left), and
  the job gets its status. Their logs may be missing then, for lack of room.
- **Resources:** services run on the job's threads. Their `memory` adds to
  the job's reservation, and the total stays within the box's envelope. The
  network is the job's: none by default, so loopback only. Fetch packages in
  the service's `image` (built with network).
- **Images:** a service's `image` is built like `[image]`, at the first job
  that needs it, and kept while a job of yours uses it. `command` is optional
  with an image of its own (its default command runs). `cwd` sets its
  working directory.
- **`debug <job>`** starts the services too, and removes them when the shell
  exits.

Without services, one image running several daemons from a launcher script
also works. The job's container is removed with everything it started.

### Hardware: resources
<!-- topic: resources -->

The box declares its hardware under names; you ask for a name, never for a
device. `mcu-job box topology` lists the names, what each provides and the
hardware behind it now. On the reference box (`docs/reference-box.md`):

| resource | what | shared? |
|---|---|---|
| `render` | the Intel iGPU's render node: compositors, GL, Vulkan | shared |
| `gpu` | an NVIDIA eGPU through CDI (can be absent) | one job at a time |

```toml
[jobs.ui]
resources = ["render"]
needs = ["opengl"]        # optional: refused (exit 78) unless a resource provides it

[jobs.gpu-ui]
resources = ["gpu"]
display = "gpu"           # the compositor renders on the NVIDIA GPU...
env = { WLR_RENDERER = "gles2" }   # ...only with an explicit renderer (see below)
```

- Your container gets **only the devices of the resources you asked for**,
  resolved when the job starts (a render node number can change between two
  jobs). Do not rely on `/dev/dri/by-path`: a `render` job has none, and
  in a `gpu` job only the NVIDIA's own links (added by its CDI spec) exist.
  Use `WLR_RENDER_DRM_DEVICE` or `$MCU_RESOURCES` instead.
- **`display`** sets `WLR_RENDER_DRM_DEVICE` (read by wlroots: labwc, cage)
  to the chosen resource's render node. Without `display`, it goes on the
  first resource that renders, `render` before `gpu`. Setting
  `WLR_RENDER_DRM_DEVICE` in `env` yourself turns that off; doing both is
  refused (exit 78). The renderer itself (`WLR_RENDERER`: pixman, gles2,
  vulkan) stays yours, and **set it when the GPU matters**: with
  `WLR_RENDERER` unset (auto), wlroots that cannot use the node logs
  "is not a DRM render node" and silently falls back to pixman, so the job
  runs on the CPU and still passes; with `gles2` or `vulkan` the same case
  fails. In wbox's config.yaml that is `renderer: gles2|vulkan` (wbox-mcp
  0.11.0 and later); with an older wbox, put `WLR_RENDERER` in the job's
  `env` (wbox passes it on; config.yaml wins when both are set).
- **A precise device**, when it matters: `resources = [{ name = "gpu",
  select = { vendor_device = "10de:2484" } }]`, or `driver`, or `model` (a
  substring). Only stable attributes: never a PCI bus address, which moves
  when the eGPU is replugged. No match is refused, never a silent fallback.
- **`$MCU_RESOURCES`** (`/run/mcu/resources.json`; `$BENCH_RESOURCES` too, for one release) tells the job what it
  got: per resource its model, `vendor_device`, driver and version, render
  node as seen inside, `display: true` on the one carrying the display; the
  CPU model, threads, their SMT siblings, governor and turbo; the memory
  limit, kernel, image and whether `machine` was held. Copy it into `/out`
  with your results. `MCU_CORES` (and `BENCH_CORES`) is still set, and the file is also at
  `/run/bench/resources.json` (`BENCH_RESOURCES`), for one release.
- The older spellings still work: `devices = ["dri"]` is `resources =
  ["render"]`, `exclusive = ["gpu"]` is `resources = ["gpu"]`;
  `config benchfile` points them out.

### Storage: yours to organize
<!-- topic: storage -->

`[storage]` names your own persistent directories (`[cache]`
is the older spelling). A job mounts one by name, or one of its
sub-directories as `name/<sub>`, the sub written with the run's parameters:
`"build/${branch:-main}"`, `"lib-target/${lib:-main}"`. Key it on
whatever parameter fits the job; with a `:-default`, nothing requires
`branch=`. `lock = true`: two jobs never use the same resolved directory at
once (the second waits, "storage build/<sub> in use"); different subs run
side by side.

**Every storage says when it ends.** In `[storage]`, or on one mount
(`mounts = { "corpus" = { path = "/corpus", ttl = "2d" } }`), one or
more of:

```toml
[storage]
target = { about = "a tree per branch", lock = true, seed = "main",
           until = "branch", ttl = "14d", max = "40g" }
sccache = { about = "the compile cache", keep = true, max = "14g" }
scratch = { about = "per run", drop = true }
```

- `drop = true`: removed when the job ends (unless another of your jobs
  still mounts it).
- `until = "branch"`: kept while the branch the parameter `branch` names
  exists on the remote (the repositories your `[checkout]`s point at). The
  box's daily housekeeping removes it once the branch is deleted. Name the
  parameter the sub is keyed on: `until = "lib"` for
  `"lib-target/${lib:-main}"`, checked against the checkout whose
  `ref` uses `${lib}`. When the rule names another parameter and the
  sub is keyed on exactly one, the rule follows the sub's parameter, and the
  Benchfile check says so.
- `ttl = "14d"`: removed after that long without a job mounting it.
- `keep = true` with `max = "40g"`: kept, but capped. At the end of a job
  that mounts it, and daily, a storage over its cap loses its least recently
  modified files first, down to 90 % of the cap. It is emptied only if that
  is not enough. `trim = "cargo sweep --time 7"` runs first, in the job's
  image, when it is over. `max` also caps each sub of a storage with other
  rules (a branch tree that grows forever).

They combine, and whichever comes first wins. `storage list` shows each
storage's rule and what is left of it. A Benchfile with a storage that
states no end is warned about now; **from 2026-11-03 it is refused**.

`tmp = true` (the default) tells the box the storage regenerates: an
admin `reclaim` may empty it, and the box evicts its unused subs when your
quota runs short (below). `tmp = false` storage goes only with your
`storage rm` or an admin `purge`. Sub-directories unused for the retention
period are removed.

```sh
storage list                     # each storage and sub: size, last use, tmp, declared
storage rm <name>[/<sub>]        # yours to drop (refused only while a queued or running job of yours mounts it)
storage prune [--older-than 7d] [--keep <sub>]... [--dry-run]
                                 # drop your tmp subs unused for that long, in one call
```

**Room before a job.** The box learns how much each job grows your disk,
on the SSD and on the spinning disk separately (its peak during the run on
each). Before the next run starts, it checks each pool type against your
quota there. If one has less room than the job's need there, plus 10 %:
- it drops the images nothing needs now (see `config clean images`);
- it evicts your **least recently used tmp subs** on that pool type, never a sub a
  queued or running job of yours mounts, nor a `tmp = false` storage, and
  logs each one ("evicted storage lib-target/feat-x, 4.1 GB, unused
  since …");
- if even all of them would not make room, it evicts nothing and the job
  ends at once with **exit 75** and the figures, instead of failing midway
  with "No space left on device".

A run that still hits "No space left on device" (bigger than anything
learned) ends with a line giving your quota and your largest storages. A
**SIGBUS** (`ld terminated with signal 7 [Bus error]`) is the same thing
seen through a mapped file: the line then names a full quota, or the
container's memory limit filled by the write's page cache.
When a new Benchfile moves a job's storage to another pool type (`media`),
that job's learned needs are reset (`config benchfile` says so). Its next
run learns them again.

`at = "/some/path"` on a `[checkout.<name>]`, or under `[push.<name>]` for
pushed code, also mounts it at that path, so a build cache shared by branch
checkouts and pushed code always sees the same source path (cargo's
`CARGO_MANIFEST_DIR`, for one). The older `per = "branch"` still works.

## 4. Run jobs
<!-- topic: jobs cancel debug -->

```sh
run <job> [key=value...]  # Benchfile: queue, then follow the log until it ends
run <branch> <job>        # repository mode
provision <volume> [key=value...]   # or provision <branch> <volume>
wait <id> | status <id> | list
queue                     # what runs and what waits, with expected times
run <job> ... after_ok=<id>   # see "Dependencies and chains"; chain < steps
cancel <id>               # stop one of your jobs, queued or running
cancel --branch <branch>  # all your queued or running jobs with branch=<branch>
fetch <id> > result.tar   # log, status and /out of a job
ssh -t ... debug <job> [key=value...]   # Benchfile: the job's checkouts and [fetch],
                                        # then a shell in its container (its rights, its
                                        # caches, no network), queued like the job;
                                        # removed when you exit
ssh -t ... debug <branch> <job>   # repository mode
```

`cancel` takes a queued job out of the queue. On a running job it stops the
container: SIGTERM, then SIGKILL after 10 s. The log and whatever is in
`/out` stay. The job ends with **exit 130** (`exit 130 (cancelled)` in
`status` and `list`), and a `run` or `wait` still attached returns 130. A
job that reaches its `timeout` is stopped the same way and ends with exit 124.
Image builds are not cancelled (the job that queued one is).

### Reading in place: the project's view
<!-- topic: view -->

Instead of copying results back with `fetch`, read your storages and job
results where they are, over sshfs, with your usual key and user. It's
read-only and shows nothing but your project:

```sh
sshfs -o ro,IdentityFile=~/.ssh/mcu-<project> mcu-<project>@<box>: ~/mnt/mcu-<project>
```

```text
storage/<name>/...               each [storage], as your jobs see it (target/<branch>, ...)
jobs/<id>/log, status, out/...   each run kept for the retention
latest/<job>/                    the last successful run of <job>
latest/<job>,branch=main/        ... with those parameters (sorted, values URL-quoted)
```

Each sftp session gets its view when it connects. A storage created
meanwhile, or a newer `latest/`, shows on the next mount (`umount`, then
`sshfs` again). The paths stay put while a storage or job keeps its name.
Benchfile uploads, and the admin moving a storage to another disk, don't
change them. `config usage` and `box` print your mount line.

Look from the mount. To keep something, or to run a binary for long, copy
it with `fetch`: a process started from the mount dies if the box goes away
while it loads code from there. Writing still goes through `push`, `config`
and `run`. Symlinks inside a storage don't resolve in the view. Their
targets show at their own paths. `/usr`, `/lib64`, `/dev` and `/etc` at the
root hold only what the sftp server needs (your project's own user entry).

### Dependencies and chains
<!-- topic: deps chain -->

The queue knows no order between your jobs beyond its own; you state it:

```sh
run release branch=x after_ok=20261028-104512-3f2a   # only if that job succeeded
run report after=20261028-104512-3f2a,20261028-104530-9c1e   # once both ended, any outcome
run gate branch=x ext_job_id=gate-x                  # your own name for the job
run release branch=x after_ok=x:gate-x               # ... used as x:<ext_job_id>
status x:gate-x | wait x:gate-x | cancel x:gate-x | fetch x:gate-x
```

- A job waits for its dependencies **before** its checkout and its image, so
  it sees what they left; meanwhile it holds no threads and no memory, and
  `queue` lists it under "waiting for dependencies". Then it queues like
  any job.
- `after_ok`: when a dependency did not succeed (failed, timed out,
  cancelled), the job does not run and ends with **exit 79**.
- Dependencies are your own project's jobs. An unknown one is refused at
  submit; one already ended counts as met, unless it failed and you asked
  `after_ok` (refused). At most 50 jobs of a project wait for dependencies
  (`max_waiting_deps`).
- `ext_job_id=<name>` (letters, digits, `. _ -`, 64 at most) gives a job your
  own name; anywhere a job id goes, `x:<name>` means the job carrying it now.
  **Resubmitting with the same name takes its place**: after a failure, the
  rerun is what `x:<name>` means for `status`, `wait`, `fetch` and for the
  jobs that wait on it. A job still queued or running under that name is
  replaced, so cancelled. Jobs waiting with `after_ok=x:<name>` follow the
  rerun only if it arrives before they saw the failure (they check every 2 s).
- `cancel <id>` says what happens to the jobs that wait on it: `after_ok` ones
  end with exit 79, `after` ones go on.

**Prerequisites in the Benchfile: `first`.** A job can name what must pass
before it, so `run test-crate …` alone runs the check first:

```toml
[jobs.test-crate]
first = [{ job = "check", pass = ["branch", "crate"] }]
```

- `run test-crate branch=x crate=y` submits `check branch=x crate=y`, then
  `test-crate` waiting on it with `after_ok` (holding nothing meanwhile). If
  the identical check (same job, same passed parameters) is already queued or
  running, `test-crate` waits on that one instead of submitting another.
- `pass` lists the run's parameters given to the prerequisite; the others
  take its defaults. It uses the same Benchfile as the job.
- **Once per commit:** a prerequisite that finds, after its fetch, an
  earlier successful run with the same parameters, Benchfile and commits
  (and the same pushed code) ends at once with exit 0 ("already passed at …
  by <id>: not run again").
- If it fails, the job does not run and ends with **exit 79**; its log says
  `first: check <id> failed (exit 101)`. `status` shows each prerequisite and
  its state.
- One level: a prerequisite has no `first` of its own. `first=none` on a run
  skips them (`first` is a reserved parameter name).

A **chain** submits several jobs at once, each its own job (id, log, `out/`),
linked by step names; every line is checked before any job is created:

```sh
chain < steps        # prints name=id for each step, then follows them in turn
chain --detach < steps    # only the ids
```
```
# steps: one "name = job [key=value...] [after=...] [after_ok=...] [ext_job_id=...]" per line
lint    = lint branch=x
test    = test-affected branch=x ext_job_id=test-x
release = release branch=x after_ok=lint,test
```

`lint` and `test` run side by side when they fit; `release` waits for both.
A chain's exit code is its first failing step's. For a straight line,
`run lint branch=x -- test-affected branch=x -- release branch=x` is the same
chain, each step `after_ok` the one before.

The branch must be pushed first. `client/remote.sh` in the mcu repository
wraps push, run and fetch (`MCU_HOST` in `client/.env.local`):

```sh
remote.sh <job> [key=value...] [-o <results dir>]   # Benchfile: pushes branch= if given
remote.sh <branch> <job> [<results dir>]            # repository mode
remote.sh --check                                   # 0 when the box is ok, 75 otherwise
```

It asks the box which mode the project uses (`MCU_MODE=benchfile|repo`
skips that), turns an SSH failure into exit 75, and passes the job's own
exit through (75, 78 included). `MCU_NO_PUSH=1` when the branch lives in
another repository than the one you run it from.

### `mcuctl`: the same without ssh on the command line
<!-- topic: mcuctl -->

`client/mcuctl` (Python, standard library only) sends the same commands with
the key and the host taken from configuration, so a command line is only
`mcuctl <command>`: a permission layer can allow that one command, and the
box still checks each one.

```sh
# client/.env.local (git-ignored), or the file MCU_CONFIG names
MCU_HOST=mcu-myproject@box
MCU_KEY=/home/me/.ssh/mcu-myproject      # mode 600, outside the repo
```

```sh
mcuctl run test branch=x          # queue and follow; exit is the job's
mcuctl jobs                       # the queue now: memory reserved/used, your ids
mcuctl cancel <id>                # or: cancel --branch x
mcuctl fetch <id> junit --to out/ # results unpacked into out/
mcuctl benchfile wip/x            # upload tools/bench/Benchfile as branch wip/x's
mcuctl help                       # the rest: status, wait, list, box, health, config, storage…
```

`mcuctl help config` explains the settings. `BENCH_HOST`/`BENCH_KEY` (the
names `remote.sh` uses) are read too.

**An MCP server** gives the same as typed tools (`run`, `wait`, `status`,
`cancel`, `jobs`, `list`, `fetch`, `fetch_latest`, `benchfile`, `box`,
`health`, `storage_list`). It needs the standard library only, and uses the
same settings. In your project's `.mcp.json`:

```json
{ "mcpServers": { "mcu": {
    "command": "python3", "args": ["path/to/mcu/client/mcu/mcp_server.py"],
    "env": { "MCU_CONFIG": "client/.env.local" } } } }
```

`run` returns the job's id once it is queued (the job goes on, and `wait`
follows it with a timeout), so a tool call never hangs on a long job.
Results come back as JSON: exit code, summary, failures, the files fetched.
A permission layer can allow `mcp__mcu__status` and deny `mcp__mcu__cancel`,
tool by tool. With `MCU_ADMIN_HOST` set, admin tools appear too
(`admin_status`, `admin_queue`, `admin_power`, `admin_envelope_set`,
`admin_gpu`, `admin_cancel`, `admin_orphans`).

## 5. When the box is degraded
<!-- topic: degraded -->

The box is a shared machine with an external GPU: it can be off, in
maintenance, or without its GPU. You always get a clear answer, never a hang:

```sh
box          # first line: ok | degraded: gpu absent | maintenance: <reason>
             # then the details as JSON (threads, memory, gpu, gpu_state, queue)
```

**Exit 75** (EX_TEMPFAIL) from `run`, `provision`, `wait` or `debug` means
the box could not serve the job. Retry later or run it elsewhere; never
count it as a test failure. The reason is in the message and in the job's
log (times in the box's messages are its local time, with the zone:
`since 12:08 CEST`):

- `the box is in maintenance: <reason>`: the queue is closed; refused
  before queuing; queued and running jobs are kept. When the admin gave an
  expected end, the same line says `(expected back 18:30 CEST); retry-after:
  <seconds>`. `box wait-open [--timeout <s>]` (or `health --wait`) returns 0
  as soon as the queue is open again, 75 at the timeout: one blocking call
  rather than a retry loop. A job already queued says
  `queued (held: <reason>…)` in `status` (and `held` in `--json`), and
  `wait` prints it once when the hold starts and once when it lifts;
- `gpu unavailable on this box (...)`: a job holding `gpu` is refused at
  once, or dropped from the queue if the GPU goes while it waits; jobs
  without `gpu` run normally. The box knows the moment the eGPU is
  unplugged or plugged back (`gpu_state` in `box`: `present`, `absent`).
  The reason says when the admin is taking it away on purpose
  (`the admin is ejecting the eGPU`, then `ejected`, `gpu_state`
  `ejecting` and `ejected`): running GPU jobs finish first; retry once it
  is back. `lost` (`gpu_note` says why) means it was pulled without an eject: plugged back, it
  usually comes up by itself, otherwise the reason says a reboot is
  needed. Plugging the enclosure back brings the GPU up by itself
  (`present` again) in every case but that one;
- `gpu lost during the run`: the GPU went away under a running job;
- `resource <name>: no render node ...`: the hardware behind a resource is
  not there right now;
- `interrupted: box restarted`: the box rebooted under the job; its
  status is set at boot, so `wait` and `status` answer.

**Exit 78** (EX_CONFIG) is the opposite: the job asks for something this
box's configuration cannot give (a `needs` no resource provides, `display`
on a resource without a render node, `display` and `env` both choosing the
node, a `select` that matches nothing). Retrying cannot help: fix the
Benchfile.

The admin can also **pause** the queue: jobs are still accepted and queued,
but none starts until it reopens; `box` says `(queue paused: <reason>)`, and
`status` shows it as the reason a job waits. Nothing is refused, so there is
no exit 75: a `run` simply waits longer.

A **box upgrade** pauses the queue the same way (`held: box upgrade: …`, with
the expected end): the running jobs finish, never stopped, the new version
is installed, and the queue reopens on its own, usually within the time the
running jobs had left plus a few seconds.

On the client, an SSH failure (exit 255: box off, network) means the same:
`client/remote.sh` turns it into exit 75 ("mcu box unreachable"), and
`remote.sh --check` asks `box` with a 5 s timeout (0 when ok, 75 otherwise).
A job you lost the connection to may still run: `wait <id>` reattaches.

## 6. Watch
<!-- topic: watch -->

### Capacity and the queue
<!-- topic: queue priority backfill -->

The box shares its threads and memory between the jobs of every project:
`box` prints the total (`threads`, `memory`, in bytes) and your job
envelope. A job reserves its `cores` and its memory for its whole run, and
**jobs run side by side when their threads and memory fit in what is
free**. A job with `exclusive = ["machine"]` or `timing = true` runs alone;
a job naming a resource (`gpu`, …) waits for whoever holds it.

The queue keeps its order, with **backfill**: a job behind may start first
when it fits now and delays none of the jobs ahead of it. Either it ends
before their turn comes (estimated from the running jobs' learned
durations), or it leaves them room when it comes. How long it lasts is
counted on the safe side: the p90 of its last 20 runs, at most its
`timeout`, and its `timeout` when it has no history yet. A 4 GB job may
start beside a 10 GB one even with another 10 GB job waiting first, when
14 GB are for jobs: it takes nothing that one could use. Estimates can be
wrong, so a job that has waited longer than its **`max_wait`** is no longer
passed by anything that could delay it, whatever the estimates say:

```toml
[jobs.release]
max_wait = "15m"     # default: the box's backfill_max_wait (30m); between 5m and 24h
```

Nothing passes a job with `exclusive = ["machine"]` or `timing = true`. Two
jobs of a project on the same locked cache keep their order.

**Priority**, `high`, `normal` (the default) or `low`, to order a project's
own jobs: the release a merge waits for first, the full suites and timing
runs last.

```sh
run release branch=x priority=high   # given at submit
run suite priority=low
bump <id> [high|normal|low]          # a job already waiting (or x:<ext_job_id>); high by default
chain priority=high < steps          # every step without a priority= of its own
run a priority=high -- b -- c        # in run a -- b, a priority= in any step is the whole chain's
```

Any number of a project's jobs may have each level, and `bump` changes only
the job it names. Within a project, the levels deal its jobs again over the
places they already hold in the queue: high ones first, then normal, then
low, each in queue order. So the other projects' jobs never move because of
them. Across projects, only a project's first high-priority job goes ahead
of the rest (in queue order between projects); its other high jobs keep the
project's places. The rules above still apply (backfill, `max_wait`,
machine jobs, dependencies).

```sh
queue            # the capacity, what runs (since, expected end), what waits
                 # (position, why, expected start), in queue order
queue --json     # the same as `health`'s queue
status <id>      # a queued job: its position, the jobs ahead, its expected start
config usage     # ends with your jobs' run times: median and p90 of the last 20
```

While a job waits, `run` and `wait` print a line at each change and every
minute:

```
waiting: needs 6 threads, 2 free (held by myapp/lint 8); position 1; start ~10:42 CET (estimate)
```

The times are estimates: the scheduler's own rule played forward, each job
lasting the median of its last 5 runs (cancelled runs left out). A job with
no history yet makes the times after it unknown (`?`, `null`), and so does
a paused queue.

### `health`: the box as an agent sees it
<!-- topic: health -->

`mcu-job health` prints one JSON document:

- `status`: ok, degraded or maintenance;
- `queue`: its `mode` (open, paused, closed) and `reason`; the `capacity`
  (threads and memory for jobs); every project's `running` jobs (threads,
  exclusive, started, `eta_end`) and `waiting` ones (why, `position`,
  `ahead`: the jobs before them, `eta_start`), times in Unix seconds;
  `expected_wait.seconds`, an estimate of when the machine is free (all
  running jobs done), from each job's median recent duration; `null` when a
  running job has no history yet (see "Capacity and the queue");
- `machine`: load, CPU temperature, mean frequency, governor, turbo, the
  throttle counter, memory;
- `gpu`: present, name, temperature, VRAM used and total, utilization,
  power, and since when it is present or absent;
- `disk`: your quota and use, and what fills it (images, src, jobs, each
  cache with its per-branch trees, your volumes), in bytes.

### Timing jobs: `timing = true`
<!-- topic: timing -->

A job whose result is its duration says so: `timing = true`. It then holds
the whole machine (as `exclusive = ["machine"]`) and gets the box's timing
profile, set by the admin: a frequency cap while it runs (a small box
otherwise reaches 100 °C and throttles under load), and, if the admin turned it on, a
cool-down before it starts (the queue shows "cooling down: 78 °C -> 60 °C").
The admin may also lower the CPU's power limit for every other job (a
fast / balanced / quiet profile; balanced is about 25 % slower on a long
full-load build, quiet about 45 %); a timing job always runs at the fast
profile, so timings stay comparable whatever the box is set to.
Jobs that hold `machine` only to be alone (GUI races, say) leave `timing`
out and run at the box's current profile with no wait. `$MCU_RESOURCES` and
`conditions.json` record the profile a run had (`timing`: cap, start
temperature, cool-down); a timing run whose cap could not be applied, or
that started hot after the cool-down's longest wait, is not comparable.

### Timing jobs: `conditions.json`
<!-- topic: conditions -->

Every job leaves `conditions.json` next to its log (`fetch` returns it): the
CPU temperature max and mean over the run, the mean frequency of its
threads, the time it lost to throttling (`throttle_seconds`, and
`throttle_share` of the run) with the throttling events, governor and turbo at
both ends, whether it held `machine`, and the other jobs that ran at the same
time. `comparable` is false, with the reasons in `flags`, when the run should
not be compared with others: throttling took more than 1 % of its time,
governor or turbo changed, or another job ran meanwhile without `machine`
held. (A small box under load throttles thousands of times for a millisecond
each; that costs a run nothing measurable, so only the time counts.) A timing harness should keep only
comparable runs, or rerun. The dashboard's run page shows the same.

`https://<box>:8080/` (self-signed certificate, behind the admin's password):
the box's threads, GPU and machine as lanes, with the queue above the "now"
line (and why each job waits) and finished runs sinking below; each project's
recent runs, each job's trends (duration, CPU, memory peak), each run's cost,
and the machine's own curves. The page is a client of `/api` (JSON, plus an
SSE stream at `/api/events`); agents use `list`, `status` and `wait` instead.

## 7. Exit codes
<!-- topic: exit -->

`run`, `wait` and `status` end with the job's own exit code when it ran, or
one of the box's:

| code | meaning | what to do |
|---|---|---|
| 0 | the job succeeded | |
| 1–125 | the job's own command failed with that code (cargo nextest, for one, ends with 100 on test failures) | read the log (`status <id>` shows the first failures) |
| 2 | refused: the request breaks a rule (a job not in the Benchfile, above the envelope, a bad parameter). The message ends with the topic that explains it | fix the request; `docs <topic>` |
| 3 | the box could not prepare the job: a checkout, a fetch, a prefetch or a service failed | the log says which; often the network or a branch that doesn't exist |
| 75 | not now: maintenance, the queue closed, the GPU absent, not enough disk, or the box restarted under the job | retry later; never a test failure |
| 78 | this box can never run it as asked (a resource it doesn't have, a capability no resource provides) | change the Benchfile, or ask the admin |
| 79 | a dependency (`after_ok=`, or a Benchfile `first`) did not succeed; the job never ran | fix and rerun the dependency |
| 124 | the job reached its `timeout` and was stopped | raise `timeout` (up to the envelope's `timeout_max`), or make the job shorter |
| 130 | cancelled (`cancel`, or a newer job took its `ext_job_id`) | |
| 137 | the kernel killed it: almost always its memory limit | declare more `memory`; the log says when the limit was hit |

## 8. Troubleshooting
<!-- topic: troubleshooting -->

- **"No space left on device", or exit 75 "needs N GB":** your quota is full.
  `config usage` shows what fills it; `storage prune`, `config clean images`,
  or raise `[project] disk` within the envelope (`docs quotas`, `docs storage`).
- **A GPU job ends with 75:** the GPU is absent or ejected right now (`box`
  says so). It comes back by itself when the admin re-attaches it.
- **A job that needs the network fails at its checkout:** the envelope's
  `network` decides what jobs may reach. `[checkout]` and `[fetch]` run with
  network; the job itself runs with what it asked for, within the envelope
  (`docs config`).
- **"waiting: storage x/y in use":** another of your jobs holds that locked
  storage. Different subs run side by side (`docs storage`).
- **A job waits for a long time:** `queue` says why each job waits and when it
  should start. A job past its `max_wait` is no longer passed by smaller ones
  (`docs queue`).
- **A storage being moved:** the admin is moving it to another disk; jobs
  using it wait for the move, then start.
- **A service doesn't start:** its own log is in the job's results
  (`out/services/<name>.log`); the job ends with 3 (`docs services`).

## Appendix: repository mode (legacy)
<!-- topic: repo -->

The first format, kept for projects that still use it. New projects should
use a Benchfile. In repository mode the box checks out your repository and
reads a manifest from it; jobs share one checkout and one build tree, so they
run one at a time.

### Repository mode

`config repo <https url>` and a manifest at `config manifest` (default
`tools/bench/bench.toml`): the box fetches the branch you name
(`run <branch> <job>`) into one persistent checkout at `/src`, and builds the
image from the manifest's `containerfile`. Same job keys as below. All jobs
share that one checkout, so a project in this mode runs one job at a time;
a Benchfile with `[checkout]` does not have that limit.

### The manifest
<!-- topic: manifest -->

In your repository, at the path above. The box reads it from the branch you
run, and builds your image from your Containerfile; the image is rebuilt only
when its directory changes.

```toml
containerfile = "tools/bench/image/Containerfile"
prefetch = "cargo fetch --locked"     # optional: runs with network before each job

[cache]                               # directories kept between jobs, private to you
target = "the cargo target dir"

[jobs.test]
command = "cargo nextest run --profile ci --offline"
cores = 4                 # threads the job gets, pinned
memory = "4g"             # an estimate; the box learns the real peak
mounts = { target = "/target" }
timeout = "30m"

[jobs.bench]
command = "cargo xtask bench masks"
cores = 8
memory = "3g"
exclusive = ["machine"]   # nothing else runs meanwhile: for timings

[jobs.gpu-tests]
command = "cargo nextest run --features gpu"
cores = 2
resources = ["gpu"]       # the NVIDIA GPU, one job at a time (see "Hardware")
mounts = { tool-cli = "/opt/tool" }   # a shared volume, read-only

[volumes.my-volume]       # how to fill a volume you own (with network)
provision = "tools/bench/fill-volume.sh /volume"
```

**Memory.** `memory` is what the queue reserves for the job: it starts
only when that fits beside the running jobs' reservations. Once the job has
run, the box reserves the largest peak of its last 5 runs plus 10 % when
that is more (the job's anonymous memory, not the page cache of what it
writes). The container's hard limit is the reservation plus a quarter (2 GB
more at least, room for a large write's page cache), up to the per-job cap
plus that room. A job that goes
past its limit only hurts itself: it is slowed down (its memory goes to
swap), or past that a process of it is killed by the kernel. Its log says so
("memory limit … reached"), and its next run reserves more. To avoid that first failure, declare a realistic
`memory`.

The log's `memory peak` line, sampled from the job's cgroup every second,
says what each figure is: the peak first is the job's anonymous memory with
what of it went to swap, the figure the box reserves from; then the
cgroup's own peak with the page cache (it fills up to the limit on a large
write, at no cost), the most of mapped files (a linker's inputs: reclaimable,
but a link slows down without them) and the swap peak. The same figures are
in the job's `conditions.json` under `memory`.

A job runs in `/work` (Benchfile) or `/src` (repository mode), with `/out`
for its results, and no network. Exclusive pseudo-resources: `machine`
(alone on the box) and `disk` (heavy I/O); hardware comes as resources.
