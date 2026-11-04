# Configuring a box

A box's settings come from two layers, merged in one place
(`box/mcuconf.py`), the second over the first:

1. **Defaults**, safe on any dedicated machine. Some are computed from it:
   one physical core kept for the system, 10 % of the memory (2 GB at least),
   half the jobs' threads and memory as one job's ceiling, and only the
   hardware actually found (a render node, an NVIDIA GPU).
2. **`/etc/mcu/box.toml`**, the machine's file, edited by hand.
   `box/box.example.toml` lists every key, commented out with its default.
   `init` installs `box/box.local.toml` there the first time. Afterwards the
   file in `/etc` is the machine's own.

`box.toml` is the only place a setting lives. The dashboard and
`mcu-admin envelope set <key> <value>` change the envelope's limits by
rewriting that key's line in it: the other lines and the comments are kept,
the previous file is kept as `box.toml.bak.<date>` (the five latest), and the
diff goes to the event log. `mcu-admin envelope reset <key>` brings the default back: a
line with its own comment is commented out (a later `set` comes back to it), a
line the dashboard added goes. A box upgraded from a version that kept the
dashboard's values in `/var/lib/mcu/envelope.json` gets them written into
`box.toml` by `init`, and the old file removed.

Other state the admin changes from the dashboard stays in `/var/lib/mcu`,
apart from the file: the pools (`pools.json`), the timing profile
(`timing.json`), the CPU power profile (`power.json`), the queue's mode.

To see what is in force and where each value comes from:

```sh
mcu-admin config show
```

```text
envelope.max_cores           10                           box.toml
envelope.jobs_memory         14g                          box.toml
reserved                     0,6                          box.toml
backfill_max_wait            30m                          default
```

`mcu-admin config show --origin` groups the settings (Machine, Per job, Disk,
Network and hardware, Housekeeping) and adds, for each, the line in
`box.toml`, the default when the file changes it, who may change it (by hand
in the file, or the dashboard and `envelope set`) and when a change takes
effect. `--json` gives the same rows; the dashboard's `GET /api/config` serves
them.

`mcu-admin doctor` checks the machine itself (packages, quotas, SELinux,
services). See `docs/reference-box.md` for one machine's values.
