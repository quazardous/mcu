// the demo mode (npm run demo): the dashboard against made-up, neutral data,
// with no box behind it, to try it or take screenshots. Every GET is
// answered here; a POST changes the little that shows (the envelope, the
// queue's mode) and says it was not applied anywhere
const now = () => Date.now() / 1000;
const G = 2 ** 30;
const T0 = now();
const PROJECTS = ["alpha", "beta", "gamma"];
const JOBS = { alpha: ["build", "test", "bench"], beta: ["render", "test"], gamma: ["train"] };

// the runs of the last three days, every project's
const recent = [];
let n = 0;
for (let i = 72; i > 0; i--) {
  for (const p of PROJECTS) {
    if ((i + p.length) % 3) continue;
    for (const job of JOBS[p]) {
      const t = T0 - i * 3600 - n * 37;
      const seconds = { build: 420, test: 95, bench: 610, render: 260, train: 1500 }[job] * (0.85 + ((n * 7) % 30) / 100);
      const failed = n % 23 === 5;
      recent.push({ kind: "done", id: `d${String(++n).padStart(4, "0")}`, project: p, job, status: failed ? 1 : 0, seconds,
        branch: "main", params: { branch: "main" }, benchfile: 3, t,
        cost: { threads: 4, cpu_seconds: seconds * 3.1, cpu_use: 0.78, memory_peak: (2 + (n % 5)) * G, memory_reserved: 8 * G } });
    }
  }
}
// the last 45 minutes, as the graph draws them: each run's start (its
// threads) then its end
for (const [p, job, threads, ago, seconds, extra] of [
  ["alpha", "build", [1, 2, 3, 4], 2700, 420, {}], ["beta", "render", [5, 7], 2500, 280, { exclusive: ["gpu"] }],
  ["gamma", "train", [8, 9, 10, 11], 2400, 1500, { exclusive: ["gpu"] }], ["alpha", "test", [1, 2], 2150, 95, {}],
  ["beta", "build", [3, 4, 5], 1900, 400, {}], ["alpha", "bench", [1, 2, 3, 4, 5, 7, 8, 9, 10, 11], 820, 610, { exclusive: ["machine"] }],
  ["gamma", "test", [10, 11], 160, 95, {}],
]) {
  const t = T0 - ago, id = `l${String(++n).padStart(4, "0")}`;
  recent.push({ kind: "started", id, project: p, job, threads, exclusive: extra.exclusive || [], memory: 4 * G, t });
  if (t + seconds < T0)
    recent.push({ kind: "done", id, project: p, job, status: 0, seconds, branch: "main", params: { branch: "main" }, benchfile: 3,
      t: t + seconds, cost: { threads: threads.length, cpu_seconds: seconds * threads.length * 0.8, cpu_use: 0.8, memory_peak: 3 * G, memory_reserved: 4 * G } });
}
// the job running now (state().running): its start is in the events too
recent.push({ kind: "started", id: "r0001", project: "alpha", job: "test", threads: [2, 3, 8, 9], exclusive: [], memory: 4 * G, t: T0 - 190 });
recent.sort((a, b) => a.t - b.t);
const ENV_DEFAULTS = { default_disk: "10g", max_disk_ssd: "60g", max_disk_hdd: "0", max_volumes: 4, max_cores: 6,
  build_cores: 4, max_jobs: 2, memory: "14g", jobs_memory: "28g", exclusive: ["machine", "disk", "gpu"], network: "fetch",
  resources: ["render", "gpu"], timeout_max: "3h", retention_days: 14 };
const envSet = { max_cores: 8, max_disk_hdd: "200g" };
const queue = { mode: "open", reason: null, by: null };

const units = (p, k) => [
  { unit: "images", kind: "images", pool: "data", bytes: (3 + k) * G },
  { unit: "storage/cache", kind: "storage", pool: "data", bytes: (5 + 2 * k) * G, life: { ttl: 30 * 86400 } },
  { unit: "storage/target", kind: "storage", pool: "data", bytes: (2 + k) * G, life: null },
  { unit: "results", kind: "results", pool: k === 2 ? "bulk" : "data", bytes: 0.4 * G },
];
function projects() {
  return Object.fromEntries(PROJECTS.map((p, k) => [p, {
    repo: `https://git.example.org/${p}/${p}`, benchfile: 3 + k, benchfiles: k === 0 ? { "feature-x": 1 } : {},
    abouts: Object.fromEntries(JOBS[p].map((j) => [j, `the ${j} job`])),
    disk: k === 1 ? "40g" : "", admin_disk: k === 2 ? "80g" : null, disk_hdd: k === 2 ? "200g" : "", admin_disk_hdd: null,
    quota_source: { ssd: ["Benchfile v3", "config disk", "admin"][k], hdd: k === 2 ? "config disk" : "default" },
    media: { ssd: { used: (9 + 4 * k) * G, quota: [30, 40, 80][k] * G }, hdd: { used: k === 2 ? 120 * G : 0, quota: k === 2 ? 200 * G : 0 } },
    used: (9 + 4 * k) * G, quota: [30, 40, 80][k] * G, tree: true, pool: "data", units: units(p, k), key: true,
    parts: { images: (3 + k) * G, tmp_storage: 1 * G, kept_storage: (7 + 3 * k) * G, src: 0.5 * G, old_results: 0.3 * G,
             recent_results: 0.1 * G, volumes: 0, reclaimable: (4.3 + k) * G, purgeable: (12 + 4 * k) * G, measured: now() - 120 },
  }]));
}
const fs = (mount, device, size, free, extra = {}) => ({ mount, device, fstype: "xfs", size: size * G, free: free * G, prjquota: true, usable: true, why: null, ...extra });
function state() {
  const envelope = { ...ENV_DEFAULTS, ...envSet };
  return {
    now: now(), version: "0.1.0 (demo)", host: "demo-box", threads: 12, reserved: [0, 6], exclusive: ["machine", "gpu", "disk"],
    envelope_bounds: { max_cores: [1, 12], build_cores: [1, 12], jobs_memory: [4, 31], memory: [1, 31], default_disk: [1, 900],
      max_disk_ssd: [1, 900], max_disk_hdd: [0, 1800], max_volumes: [1, 50], max_jobs: [1, 32], retention_days: [1, 365], timeout_max: [1, 24] },
    declared: { render: { share: "shared", about: "the integrated GPU's render node", present: true, hotplug: false },
                gpu: { share: "exclusive", about: "an NVIDIA GPU", present: true, hotplug: true } },
    running: [{ id: "r0001", project: "alpha", job: "test", threads: [2, 3, 8, 9], exclusive: [], memory: 4 * G, started: now() - 190 }],
    queue: [{ id: "q0001", project: "beta", job: "render", cores: 4, exclusive: ["gpu"], memory: 6 * G, t: now() - 15 }],
    resources: { updated: now(), gpu: true, gpu_state: "present", gpu_name: "a desktop GPU", gpu_since: T0 - 86400,
      queue: queue.mode, hold: queue.mode === "open" ? null : queue.reason, maintenance: queue.mode === "closed" ? queue.reason : null,
      until: null, queue_by: queue.by, status: queue.mode === "closed" ? "maintenance" : "ok", problems: [] },
    envelope, envelope_defaults: ENV_DEFAULTS, envelope_set: Object.keys(envSet),
    power: { profile: "balanced", profiles: { fast: 65, balanced: 45, quiet: 28 }, limit_w: 45 },
    shutdown: null,
    pools: {
      data: { ...fs("/srv/data", "/dev/nvme0n1p3", 900, 610), path: "/srv/data/mcu", default: true, media: "ssd", budget: 600 * G, projects: PROJECTS, quotas: 150 * G },
      bulk: { ...fs("/srv/bulk", "/dev/sda1", 1800, 1500), path: "/srv/bulk/mcu", default: false, media: "hdd", budget: 1200 * G, projects: ["gamma"], quotas: 200 * G },
    },
    disks: { filesystems: [fs("/srv/data", "/dev/nvme0n1p3", 900, 610), fs("/srv/bulk", "/dev/sda1", 1800, 1500)],
             system: [{ mount: "/", device: "/dev/nvme0n1p2", fstype: "xfs", size: 100 * G, free: 61 * G }], vg_free: [], blank_disks: [] },
    timing: { cooldown: true, target: "auto", cap_pct: null, idle_temp: 41 },
    projects: projects(), recent,
    machine: { load: [2.1, 1.8, 1.5], mem_total: 32 * G, mem_available: 21 * G, reserved_memory: "3g", disk_total: 100 * G, disk_free: 61 * G, governor: "powersave" },
  };
}
const ROWS = [
  ["reserved", "Machine", "file", "next job", "0,6", "box.toml", 4], ["reserved_memory", "Machine", "file", "next job", "3g", "computed"],
  ["backfill_max_wait", "Machine", "file", "next job", "30m", "default"], ["dashboard_port", "Machine", "file", "after init (the dashboard restarts)", 8080, "default"],
  ["tls_cert", "Machine", "file", "after init (the dashboard restarts)", null, "unset"], ["power", "Machine", "file", "at once", null, "unset"],
  ["projects_dir", "Disk", "file", "new projects (after init)", "/srv/data/mcu", "box.toml", 9], ["home_quota", "Disk", "file", "after init", "5g", "default"],
  ["resources", "Network and hardware", "file", "next job", ["gpu", "render"], "box.toml", 30],
];
const GROUP = { jobs_memory: "Machine", max_cores: "Per job", build_cores: "Per job", memory: "Per job", max_jobs: "Per job", timeout_max: "Per job",
  default_disk: "Disk", max_disk_ssd: "Disk", max_disk_hdd: "Disk", max_volumes: "Disk", resources: "Network and hardware",
  exclusive: "Network and hardware", network: "Network and hardware", retention_days: "Housekeeping" };
function config() {
  const env = { ...ENV_DEFAULTS, ...envSet };
  const settings = ROWS.map(([key, group, writer, effect, value, source, line]) => ({ key, group, writer, effect, value, source, line: line || null, default: null }));
  for (const [k, group] of Object.entries(GROUP)) {
    const set = k in envSet;
    settings.push({ key: `envelope.${k}`, group, writer: "admin", effect: "next job", value: env[k], source: set ? "box.toml" : "default",
                    line: set ? 20 + Object.keys(envSet).indexOf(k) : null, default: set ? ENV_DEFAULTS[k] : null });
  }
  const order = ["Machine", "Per job", "Disk", "Network and hardware", "Housekeeping"];
  settings.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
  return { version: "0.1.0 (demo)", file: "/etc/mcu/box.toml", settings };
}
function series(db, win) {
  const span = { "1h": 3600, "6h": 21600, "1d": 86400, "1w": 604800, "1m": 2592000, "1y": 31536000 }[win] || 86400;
  const names = db === "machine" ? ["load", "mem_used", "cpu_temp", "cpu_mhz"] : db === "cpu" ? ["t0", "t1", "t2", "t3"]
    : db === "gpu" ? ["util", "temp", "power"] : db === "power" ? ["total", "platform", "package", "gpu"]
    : db === "wear" ? ["ssd_wear", "ssd_written", "hdd_realloc"] : ["used", "quota"];
  const rows = [];
  for (let i = 0; i < 120; i++) {
    const t = now() - span + (i * span) / 120, w = Math.sin(i / 9) + 1;
    rows.push([t, ...names.map((x, j) => ({ load: 1.5 * w, mem_used: (8 + 4 * w) * G, cpu_temp: 45 + 12 * w, cpu_mhz: 2400 + 600 * w,
      util: 40 * w, temp: 50 + 8 * w, power: 60 * w, total: 40 + 30 * w, platform: 25 + 10 * w, package: 18 + 9 * w, gpu: 15 + 20 * w,
      ssd_wear: 3, ssd_written: 12.4, hdd_realloc: 0, used: (9 + i / 30) * G, quota: 30 * G }[x] ?? 30 * w + j * 5))]);
  }
  return { names, rows };
}
const TASKS = [
  { id: "20261104-101500-a1b2", kind: "migrate", args: ["gamma", "results", "bulk"], state: "done", started: now() - 3000, ended: now() - 2900, percent: 100 },
  { id: "20261104-093000-c3d4", kind: "prune", args: [], state: "done", started: now() - 7200, ended: now() - 7190 },
];
function info() {
  return {
    system: { host: "demo-box", product: "a small desktop", board: "a board", bios: "1.0", os: "Linux", kernel: "6.x", uptime: 86400 * 3 },
    cpu: { model: "a 12-thread CPU", threads: 12, governor: "powersave", driver: "intel_pstate", max_perf_pct: "100", no_turbo: "0", mhz: 2800, temp: 52,
           throttle_events: 0, status: "ok", note: null, power: { platform: 31.2, package: 22.5, total: 48.0, limit: { profile: "balanced", limit_w: 45 } } },
    memory: { total: 32 * G, available: 21 * G, swap_total: 8 * G, swap_free: 8 * G, jobs: 28 * G },
    disks: [{ name: "nvme0n1", model: "an NVMe SSD", size: 1000 * G, mounts: ["/", "/srv/data"], media: "ssd", wear_pct: 3, written: 12.4e12, hours: 2100, temp: 41, status: "ok" },
            { name: "sda", model: "a hard disk", size: 2000 * G, mounts: ["/srv/bulk"], media: "hdd", reallocated: 0, hours: 9100, temp: 35, status: "ok" }],
    graphics: [{ address: "0000:00:02.0", name: "an integrated GPU", driver: "i915", egpu: false, status: "ok" },
               { address: "0000:05:00.0", name: "a desktop GPU", driver: "nvidia", egpu: true, enclosure: true, state: "present", status: "ok",
                 note: "in its enclosure, answering", since: T0 - 86400, temp: 44, power: 18, pstate: "P8", vram_used: 0.4 * G, vram_total: 8 * G }],
    thunderbolt: [{ name: "an enclosure", id: "0-1", host: false, authorized: true, speed: "40 Gb/s", status: "ok", note: null }],
    network: [{ name: "eth0", state: "up", speed: "1000", addresses: ["192.0.2.10"], status: "ok" }],
    sensors: [{ sensor: "coretemp", label: "Package", c: 52 }, { sensor: "nvme", label: "Composite", c: 41 }],
  };
}
const HARDWARE = { updated: now(), features: [
  { key: "cpufreq", feature: "CPU frequency control", state: "present", detail: "intel_pstate", fix: null },
  { key: "nvidia", feature: "NVIDIA GPU", state: "present", detail: "one card, CDI spec found", fix: null },
  { key: "rapl", feature: "power measure (RAPL)", state: "present", detail: "package and platform", fix: null },
  { key: "smart", feature: "disk health (SMART)", state: "present", detail: "2 disks", fix: null },
  { key: "virt", feature: "running in a VM", state: "absent", detail: "bare metal", fix: null },
] };

export async function demoGet(path) {
  const [p, qs] = path.split("?");
  const q = new URLSearchParams(qs || "");
  if (p === "/api/session") return { logged_in: true, csrf: "demo" };
  if (p === "/api/state") return state();
  if (p === "/api/config") return config();
  if (p === "/api/hardware") return HARDWARE;
  if (p === "/api/info") return info();
  if (p === "/api/admin/tasks") {
    const list = q.get("kind") ? TASKS.filter((t) => t.kind === q.get("kind")) : TASKS;
    return { tasks: list, total: list.length, offset: 0, limit: 20, projects: ["gamma"], active: [], log: q.get("log") ? "demo: no log here\n" : null };
  }
  let m = p.match(/^\/api\/series\/([a-z0-9-]+)$/);
  if (m) return series(m[1], q.get("window"));
  m = p.match(/^\/api\/projects\/([a-z0-9-]+)\/jobs\/([a-z0-9-]+)$/);
  if (m) return { runs: recent.filter((e) => e.project === m[1] && e.job === m[2]) };
  m = p.match(/^\/api\/projects\/([a-z0-9-]+)$/);
  if (m && projects()[m[1]]) {
    const info = projects()[m[1]];
    const jobs = Object.fromEntries(JOBS[m[1]].map((j) => {
      const r = recent.filter((e) => e.project === m[1] && e.job === j);
      return [j, { runs: r.length, ok: r.filter((e) => e.status === 0).length, last: r.slice(-20), memory_learned: 3 * G, about: `the ${j} job` }];
    }));
    return { project: m[1], ...info, volumes: m[1] === "alpha" ? { "shared-data": { owner: "alpha", readers: ["beta"] } } : {}, jobs };
  }
  m = p.match(/^\/api\/runs\/([a-z0-9-]+)\/([a-z0-9-]+)$/);
  if (m) {
    const event = recent.find((e) => e.id === m[2]);
    if (event) return { event, cost: event.cost };
  }
  throw new Error(`demo: nothing at ${p}`);
}

export async function demoPost(path, body = {}) {
  if (path === "/api/login" || path === "/api/logout") return { csrf: "demo" };
  if (path === "/api/admin/envelope") {
    if (body.reset) delete envSet[body.key];
    else envSet[body.key] = body.value;
    return { note: `demo: envelope ${body.key} ${body.reset ? "back to its default" : `= ${body.value}`} (written nowhere)` };
  }
  if (path === "/api/admin/queue") {
    Object.assign(queue, { mode: body.mode, reason: body.reason || null, by: body.mode === "open" ? null : "dashboard" });
    return { note: `demo: queue ${body.mode}` };
  }
  if (path === "/api/admin/invite") {
    return { project: body.project, host: "demo-box", key: "-----BEGIN OPENSSH PRIVATE KEY-----\n(demo: not a key)\n-----END OPENSSH PRIVATE KEY-----" };
  }
  return { note: `demo: ${path.replace("/api/admin/", "")} ${JSON.stringify(body)} (not applied: no box behind the demo)` };
}

// the live stream: a sample every 2 s, nothing else happens
export function demoConnect(on, onStatus) {
  onStatus(true);
  const timer = setInterval(() => {
    const t = now(), w = Math.sin(t / 60) + 1;
    on("samples", [[t, 30 + 20 * w, 40 + 5 * w]]);
  }, 2000);
  return () => clearInterval(timer);
}
