# Installing an mcu box

mcu takes a machine over. It creates users and groups, a sudo rule, systemd
units and udev rules, opens the dashboard's port, and gives each project a
disk quota. Install it on a machine **dedicated to it**, with nothing else
running there. To try it first, a virtual machine works with known limits
(`docs/VM.md`).

## The machine

- **CPU and memory:** what your jobs need, plus one core and about 10 % of
  the memory for the system. A 6-core/12-thread machine with 16 GB runs two
  mid-size Rust builds side by side (`docs/reference-box.md`).
- **Disks:** a fast SSD for the system and the projects' data. A spinning
  disk can be added as a second pool for large, cold data.
- **Optional:** a GPU (an integrated GPU's render node for GUI jobs; an
  NVIDIA GPU, internal or an eGPU, for CUDA/Vulkan jobs), RAPL power limits
  (Intel, recent AMD), a cpufreq driver for timing profiles. The box works
  without any of them, minus that feature.
- **Network:** a LAN. Projects reach the box over SSH, and the admin reaches
  the dashboard over HTTPS.

## The system

**Fedora** (42 or later), the only distribution tested so far, with SELinux
enforcing.

**Disk quotas** are how each project gets its share:

- **A data filesystem with XFS project quotas** (recommended): a partition or
  logical volume formatted XFS and mounted with `prjquota`, for example in
  `/etc/fstab`:

  ```text
  /dev/mapper/vg-mcu  /srv/mcu/projects  xfs  defaults,prjquota  0 0
  ```

- or **user quotas on `/`** (XFS root mounted with `uquota`, which needs
  `rootflags=uquota` on the kernel command line).

## Install

From a release tarball (the dashboard comes built) or a checkout (build it
first: `cd dashboard && npm ci && npm run build`, on any machine with node):

```sh
sudo box/mcu-admin doctor            # what is missing, each line with its fix
sudo dnf install podman python3 rrdtool smartmontools xfsprogs openssl policycoreutils-python-utils
cp box/box.example.toml box/box.local.toml   # optional: every line is a commented default
sudo make install                    # /usr/local/lib/mcu, /usr/local/bin/mcu-job, /usr/local/sbin/mcu-admin
sudo box/mcu-admin init              # users, sudoers, units, the dashboard's certificate, the firewall port
sudo mcu-admin passwd --generate     # the dashboard's admin password, printed once
sudo mcu-admin pool add data /srv/mcu/projects   # where projects' data goes (XFS prjquota)
sudo mcu-admin doctor                # everything ok now
sudo mcu-admin config show           # the settings in force, and where each comes from
```

The dashboard is at `https://<box>:8080` (a self-signed certificate; the port
is `dashboard_port` in `/etc/mcu/box.toml`).

## The first project

```sh
sudo mcu-admin invite myapp > myapp.key    # the project's private key, shown once
```

Give `myapp.key` to the project. From its machine:

```sh
chmod 600 myapp.key
ssh -i myapp.key -o IdentitiesOnly=yes mcu-myapp@box docs agents
```

`docs agents` is the project's starting point: a minimal Benchfile, the five
commands, getting results back. The project uploads its Benchfile and runs
its first job:

```sh
ssh -i myapp.key mcu-myapp@box config benchfile < Benchfile
ssh -i myapp.key mcu-myapp@box run build branch=main
```

## Upgrade

The same two commands, from the new release or checkout. They change files,
units and the sudo rule, never data:

```sh
sudo make install && sudo box/mcu-admin init
```

Running jobs keep the code they started with. To install `mcu.py` (the
scheduler) without disturbing them, pause the queue first (`mcu-admin queue
pause upgrade`), wait for the running jobs, install, then `queue open`.
`mcuctl admin deploy` does this for you from a checkout.

## Uninstall

```sh
sudo mcu-admin uninstall             # services, code, units, sudoers, udev rules, firewall port; data kept
sudo mcu-admin uninstall --purge     # also every project's account, results, storage and volumes
```

`--purge` is refused while jobs run, and asks you to type `purge`.

## A box installed under the first name

Boxes installed before the rename use `bench` names (`/var/lib/bench`,
`bench-<project>`). `mcu-admin migrate-name` converts one: it closes the queue,
waits for running jobs, backs everything up to `/root/mcu-migrate-name`,
renames accounts and paths (the uids don't change), installs the new names,
and leaves a `bench-<project>` alias for old clients until
`migrate-name --drop-aliases`. `migrate-name --reverse` puts it back.
