# Threat model

What mcu protects, from whom, how, and what it does not try to do. It names
classes of problems and the defences in place, never steps to exploit them.

## What is worth protecting

- **Each project's data:** its storage, results, volumes it does not share,
  its settings and Benchfiles.
- **The host:** root, the other users, the admin's settings.
- **Fairness:** the queue, the disk, the memory and the threads shared by the
  projects.
- **The admin's dashboard:** its sessions and what it may change.

## Who could attack

### A malicious project

It holds a project key. It controls the commands it sends, its Benchfile, the
code its jobs run, and the files its jobs write.

- **Only the box's commands.** The key's forced command is `mcu-job`: no
  shell, no forwarding. sftp on the same key gets a read-only view of that
  project's own storage and results, built per session in a private mount
  namespace, chrooted, as the project's user.
- **Job code stays in containers.** Jobs run in rootless podman as the
  project's user, with the threads, memory, devices and network the envelope
  allows, and nothing more. A container's root is the project's user on the
  host. No step runs project code on the host. In the legacy repository mode,
  the job cannot write the checkout's git metadata, and the host's git
  ignores hooks.
- **Root is reached only through `mcu-helper`**, by one sudo rule. It takes
  the project from the caller, never from an argument, checks every request
  against the envelope, and acts on a project's paths only inside that
  project's own trees: a link or a setting pointing elsewhere is refused.
- **Between projects:** one Unix user each, homes in mode 700, disk quotas
  per project (XFS project quotas, or user quotas).
- **Fairness:** declared resources, a scheduler that never lets a job delay
  the ones ahead, memory learned from what jobs really use, timeouts.

### Someone on the LAN, against the dashboard

- HTTPS only (TLS 1.2 or later), the admin's own certificate if given.
- One admin password, hashed with scrypt. Server-side sessions with a lifetime
  and a maximum age, a new token at every login, cookies `Secure; HttpOnly;
  SameSite=Strict`, a CSRF token on every change.
- Failed logins are slowed down per address and for everyone at once, and
  logged.
- A Content-Security-Policy that lets the page load only its own files, and
  HSTS.
- SSH: keys only, each one bound to its forced command.

### A compromised dashboard

The dashboard runs as an unprivileged user, `mcu-web`. It reaches root only
through `sudo mcu-admin` with a fixed list of subcommands, each validating its
arguments. It is not covered by the projects' sudo rule for the helper.

## Side channels

- **Energy counters** (RAPL) are readable by the dashboard's collector only,
  not by projects.
- **`sandbox = true`** (an opt-in for jobs that need more of `/proc`, named at
  upload) widens what a job sees of the host.
- **Timing:** jobs share the CPU. A job can observe the load others create.
  Timing jobs (`timing = true`) hold the whole machine.

## What mcu does not try to do

- **It is not a VM boundary.** Rootless podman and SELinux separate projects.
  The admin trusts the projects they invite not to attack the kernel itself.
- **Shared scheduler state.** The runners run as each project's user and
  write the shared queue state. A project that found a way to run code on the
  host could disturb scheduling. The defence is that project code runs only in
  containers.
- **No encryption at rest.** The admin can read every project's data.
- **The dashboard is for a LAN**, not the internet.
- **No backups.** See `docs/ADMIN.md`.

Report a vulnerability as `SECURITY.md` says.
