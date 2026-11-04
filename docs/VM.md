# Running mcu in a virtual machine

mcu is meant for a machine you dedicate to it (`docs/INSTALL.md`). A
virtual machine is the way to try it first, or to run it without a spare
machine, with the costs below. Jobs are rootless podman containers inside the
guest, so no nested virtualization is needed.

*This page has not been followed step by step on a test VM yet; if a step
differs from what you see, please open an issue.*

## What a VM costs

- **Timings are not comparable.** The guest shares the host's CPU; the host's
  load, turbo and throttling are invisible from inside. `doctor` and the
  dashboard say "virtual machine", and timing jobs still run, but their
  numbers only compare within one quiet host.
- **No GPU** unless you pass a device through (VFIO), which is out of scope
  here. GUI jobs render on the CPU (pixman), GPU jobs are refused.
- **No power or thermal control:** no RAPL power profiles, usually no
  frequency cap (timing jobs get the cool-down only).

The box works without all of these, minus those features.

## The guest

- **Fedora** (42 or later), Server or Cloud edition, SELinux enforcing.
- **Size:** 4 vCPUs and 8 GB of memory run small jobs; give it what your jobs
  need, plus one vCPU and about 10 % of the memory for the system.
- **Disks:** a system disk of about 20 GB, and **a second disk for the
  projects' data** (as large as their builds and caches), formatted XFS and
  mounted with project quotas, so each project's disk quota holds:

  ```sh
  sudo mkfs.xfs /dev/vdb
  sudo mkdir -p /srv/mcu/projects
  echo '/dev/vdb /srv/mcu/projects xfs defaults,prjquota 0 0' | sudo tee -a /etc/fstab
  sudo mount /srv/mcu/projects
  ```

## The host

**libvirt** (virt-manager, or GNOME Boxes for a quick try), with the Fedora
Cloud image:

```sh
sudo dnf install qemu-kvm libvirt virt-install
sudo systemctl enable --now libvirtd
# the Fedora Cloud qcow2 from https://fedoraproject.org/cloud/download
sudo virt-install --name mcu --memory 8192 --vcpus 4 --osinfo fedora-unknown \
  --disk path=/var/lib/libvirt/images/mcu.qcow2,backing_store=$PWD/Fedora-Cloud.qcow2,size=20 \
  --disk size=60,format=qcow2 \
  --cloud-init root-ssh-key=$HOME/.ssh/id_ed25519.pub \
  --network network=default --import --noautoconsole
```

**Network:** on libvirt's default NAT network the guest is reachable from
the host only. For projects on other machines, use a bridged network, or
forward ports 22 (the projects' SSH) and 8080 (the dashboard) to the guest.

## Then

Inside the guest, follow `docs/INSTALL.md` from "Install": `doctor`, the
packages, `make install`, `init`, a pool on `/srv/mcu/projects`, the first
project, its first job. `doctor` reports the VM and the absent hardware as
optional, not as errors.
