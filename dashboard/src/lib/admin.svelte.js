// what the admin's sections share: an action with its outcome in the toast
// and the state read again after it; the admin tasks not ended yet; the
// envelope's limits, each with its control and its group (the same groups as
// the effective configuration)
import { get, post } from "./api.js";
import { refreshState } from "./live.svelte.js";
import { say } from "./widgets/toast.svelte.js";

export async function act(fn) {
  try {
    const note = await fn();
    if (note) say(note);
  } catch (e) {
    say(e.message, true);
  }
  // show what the box now holds, not what the page last read
  refreshState().catch(() => {});
}
export const postNote = (path, body) => act(async () => (await post(path, body)).note);
export const queueTask = (body) => postNote("/api/admin/task", body);

// the admin tasks not ended yet, for the buttons that wait on them and the
// header's count; read again on each task event of the live stream
export const tasks = $state({ active: [] });
export async function loadTasks() {
  let r;
  try { r = await get("/api/admin/tasks?limit=1"); } catch { return; }
  // a task that just ended changed the box: show its result now
  const ended = tasks.active.some((o) => !r.active.some((t) => t.id === o.id));
  tasks.active = r.active;
  if (ended) refreshState().catch(() => {});
}
// a project's move, purge or reclaim queued or running: its buttons wait
export const busyTask = (p) => tasks.active.find((t) => ["migrate", "purge", "reclaim"].includes(t.kind) && t.args[0] === p);

export const gbOf = (v) => parseFloat(String(v ?? "0")) * ({ k: 1 / 2 ** 20, m: 1 / 1024, g: 1, t: 1024 }[String(v ?? "g").slice(-1).toLowerCase()] ?? 1);
export const hoursOf = (v) => { const m = String(v ?? "").match(/^(\d+)([smh]?)$/); return m ? Math.max(1, Math.round(Number(m[1]) / ({ s: 3600, m: 60, h: 1, "": 3600 }[m[2]]))) : 1; };

// the envelope's limits: bounds from the box (mcuconf.bounds: this
// machine's threads, memory and largest pool), the same mcu-admin checks
export function envelopeFields(state) {
  const B = state?.envelope_bounds || {};
  const lo = (k, d) => B[k]?.[0] ?? d;
  const hi = (k, d) => B[k]?.[1] ?? d;
  const memMax = Math.max(5, Math.floor((state?.machine?.mem_total || 16 * 2 ** 30) / 2 ** 30) - 1);
  const threads = state?.threads || 12;
  const declared = Object.keys(state?.declared || { render: 1, gpu: 1 });
  return [
    { key: "jobs_memory", group: "Machine", kind: "gb", min: lo("jobs_memory", Math.min(4, memMax)), max: hi("jobs_memory", memMax), label: "memory all jobs share (the rest stays for the system)" },
    { key: "max_cores", group: "Per job", kind: "int", min: lo("max_cores", 1), max: hi("max_cores", threads), label: "threads one job may ask for" },
    { key: "build_cores", group: "Per job", kind: "int", min: lo("build_cores", 1), max: hi("build_cores", threads), label: "threads an image build gets" },
    { key: "memory", group: "Per job", kind: "gb", min: lo("memory", 1), max: hi("memory", Math.min(memMax, 64)), label: "memory one job may declare (and its hard cap)" },
    { key: "max_jobs", group: "Per job", kind: "int", min: lo("max_jobs", 1), max: hi("max_jobs", 10), label: "one project's jobs running at once" },
    { key: "timeout_max", group: "Per job", kind: "hours", min: lo("timeout_max", 1), max: hi("timeout_max", 24), label: "the longest a job may run" },
    { key: "default_disk", group: "Disk", kind: "gb", min: lo("default_disk", 1), max: hi("default_disk", 120), label: "the quota of a project that set none (it follows a change)" },
    { key: "max_disk_ssd", group: "Disk", kind: "gb", min: lo("max_disk_ssd", 1), max: hi("max_disk_ssd", 270), label: "the most a project may give itself on SSD" },
    { key: "max_disk_hdd", group: "Disk", kind: "gb", min: lo("max_disk_hdd", 0), max: hi("max_disk_hdd", 800), label: "the most a project may give itself on HDD (0: only the admin gives room there)" },
    { key: "max_volumes", group: "Disk", kind: "int", min: lo("max_volumes", 1), max: hi("max_volumes", 20), label: "shared volumes one project may own" },
    { key: "network", group: "Network and hardware", kind: "select", options: ["none", "fetch", "full"], label: "network: none; fetch (checkouts, [fetch], provisioning); full (jobs may ask for it)" },
    { key: "exclusive", group: "Network and hardware", kind: "multi", options: ["machine", "disk", ...declared.filter((n) => state?.declared?.[n]?.share === "exclusive")], label: "exclusive resources a job may hold" },
    { key: "resources", group: "Network and hardware", kind: "multi", options: declared, label: "hardware resources a job may ask for" },
    { key: "retention_days", group: "Housekeeping", kind: "int", min: lo("retention_days", 1), max: hi("retention_days", 90), label: "days job results are kept" },
  ];
}
export const shownValue = (f, v) => (f.kind === "gb" ? Math.round(gbOf(v)) : f.kind === "hours" ? hoursOf(v) : f.kind === "int" ? Number(v ?? f.min)
  : f.kind === "multi" ? [...(v || [])] : v);
const toValue = (f, d) => (f.kind === "gb" ? `${d}g` : f.kind === "hours" ? `${d}h` : d);
export const setEnvelope = (f, v) => postNote("/api/admin/envelope", { key: f.key, value: toValue(f, v) });
export const resetEnvelope = (f) => postNote("/api/admin/envelope", { key: f.key, reset: true });
// where each limit is edited: Settings → Limits, at its row
export const editHref = (key) => `#/settings/limits/${key}`;

// a project's new key, shown once (invite, or a new key for a project):
// kept here so the panel survives moving between pages until "done"
export const keys = $state({ invited: null });
export const invite = (p) => act(async () => { keys.invited = await post("/api/admin/invite", { project: p }); });
