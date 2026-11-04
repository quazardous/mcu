// the box's live state: /api/state once, then every SSE event applied to it
import { get, connect, setProjects } from "./api.js";

export const live = $state({
  ready: false,
  connected: false,
  state: null,        // /api/state: threads, reserved, running, queue, machine, projects
  runs: {},           // id -> {id, project, job, threads, exclusive, memory, started, ended, status, seconds, cost}
  version: 0,         // bumped on every change, for views that redraw
  samples: [],        // [time, cpu %, memory %] every 2 s, the last 15 minutes
  tasks: 0,           // bumped on each admin task event (the admin page reloads its list)
});

function touch(id, patch) {
  live.runs[id] = { ...(live.runs[id] || { id }), ...patch };
}

function apply(kind, ev) {
  if (kind === "samples") {
    // the server sends its backlog on each (re)connection: keep what is new
    const last = live.samples.length ? live.samples[live.samples.length - 1][0] : 0;
    const fresh = ev.filter((x) => x[0] > last);
    if (fresh.length) live.samples = [...live.samples, ...fresh].slice(-450);
    return;
  } else if (kind === "task") {
    live.tasks++;
    return;
  } else if (kind === "state") {
    if (live.state) {
      live.state.running = ev.running;
      live.state.queue = ev.queue;
    }
  } else if (kind === "box") {
    // the GPU came or went, or maintenance changed: take the API's whole view
    get("/api/state").then((s) => { if (live.state) { live.state.resources = s.resources; live.state.power = s.power; live.state.shutdown = s.shutdown; } live.version++; }).catch(() => {});
  } else if (kind === "queued") {
    // what the job reserved (memory) and asked for (threads), known from the queue on
    touch(ev.id, { project: ev.project, job: ev.job, memory: ev.memory, cores: ev.cores });
  } else if (kind === "started") {
    touch(ev.id, { project: ev.project, job: ev.job, threads: ev.threads, exclusive: ev.exclusive, started: ev.t,
                   ...(ev.memory != null ? { memory: ev.memory } : {}) });
  } else if (kind === "done" || kind === "refused") {
    touch(ev.id, {
      project: ev.project, job: ev.job, ended: ev.t, status: kind === "refused" ? "refused" : ev.status,
      seconds: ev.seconds, cost: ev.cost, reason: ev.reason, params: ev.params, branch: ev.branch,
      commits: ev.commits,
    });
  }
  live.version++;
}

let stop = null, timer = null;
export function stopLive() {
  stop?.();
  stop = null;
  clearInterval(timer);
  live.ready = false;
}
export async function start() {
  live.state = await get("/api/state");
  setProjects(Object.keys(live.state.projects || {}));
  for (const ev of live.state.recent) apply(ev.kind, ev);
  live.ready = true;
  stop?.();
  stop = connect(apply, (ok) => (live.connected = ok));
  // the machine figures are not events: refresh them now and then
  clearInterval(timer);
  timer = setInterval(() => refreshState().catch(() => {}), 30000);
}

// the parts of /api/state that are not events (machine figures, projects,
// the admin's settings: pools, disks, envelope, timing): read again now and
// then, and right after an admin action
const SETTLED = ["shutdown", "machine", "projects", "resources", "pools", "disks", "envelope", "envelope_defaults",
                 "envelope_set", "timing", "declared", "threads", "power"];
export async function refreshState() {
  const s = await get("/api/state");
  if (!live.state) return;
  for (const k of SETTLED) if (k in s) live.state[k] = s[k];
  setProjects(Object.keys(s.projects || {}));
  live.version++;
}
