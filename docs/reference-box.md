# A reference box

The machine mcu grew up on, as a sizing example. Nothing in mcu depends
on it: the figures in the other docs that say "on the reference box" come
from here.

| | |
|---|---|
| Machine | a small form factor PC (Intel NUC class), dedicated to mcu |
| CPU | 6 cores / 12 threads (Intel, intel_pstate, RAPL power limits) |
| Threads for jobs | 10 of 12: one core (2 threads) kept for the system |
| Build cores | 4 (`build_cores`: what an image build gets) |
| Memory | 16 GB; about 14.3 GB shared by jobs, 2 GB kept for the system |
| Disks | an NVMe SSD (system and the default pool, XFS with project quotas), a spinning disk as an `hdd` pool |
| GPUs | the CPU's iGPU (`render`, shared) and an NVIDIA eGPU over Thunderbolt 3 (`gpu`, one job at a time, can be ejected) |
| OS | Fedora 44, SELinux enforcing, cgroup v2, rootless podman |

What it taught:

- **Cooling.** Under a long all-core load it reaches 100 °C and throttles.
  Timing jobs get a frequency cap (the timing profile) and, if the admin turns
  it on, a cool-down before they start. The "balanced" power profile is about
  25 % slower on a long build than "fast", and keeps the box quieter.
- **Memory.** 14 GB for jobs is the real limit for parallel Rust builds:
  two 6 GB jobs fit side by side, and a third waits.
- **The eGPU.** It can drop off the bus. The box handles its unplug and
  replug at once (GPU jobs wait or are refused), and its shutdown helper keeps
  a power-off from hanging.
