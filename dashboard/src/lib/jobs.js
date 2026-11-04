// one shape for a job whatever its state (queued, running, ended), for the
// timeline's tooltips and the jobs list: {id, project, job, params, state,
// threads, lanes, memory (reserved), used (now, while running), peak,
// exclusive, queued, started, ended, seconds, status, note, cost}

const paramsOf = (x) =>
  x?.branch || Object.entries(x?.params || {}).map(([k, v]) => `${k}=${v}`).join(" ");

export function endState(status) {
  return status === 0 ? "ok" : status === "refused" ? "refused" : status === 75 ? "unavailable"
    : status === 130 ? "cancelled" : status === 124 ? "timeout" : "failed";
}

export function fromQueue(q) {
  return { id: q.id, project: q.project, job: q.job, params: paramsOf(q), state: "queued",
           threads: q.cores, memory: q.memory, exclusive: q.exclusive || [], queued: q.queued,
           note: q.waiting || "about to start" };
}

// r: the scheduler's running entry (threads, memory); ev: the run's events, if any
export function fromRunning(r, ev = {}) {
  return { id: r.id, project: r.project ?? ev.project, job: r.job ?? ev.job, params: paramsOf(r) || paramsOf(ev),
           state: "running", threads: (r.threads || ev.threads || []).length, lanes: r.threads || ev.threads,
           memory: r.memory ?? ev.memory, exclusive: r.exclusive || ev.exclusive || [],
           started: r.started ?? ev.started, used: r.mem_now, peak: r.mem_peak };
}

export function fromEnded(ev) {
  return { id: ev.id, project: ev.project, job: ev.job, params: paramsOf(ev), state: endState(ev.status),
           status: ev.status, threads: ev.threads?.length ?? ev.cores ?? ev.cost?.threads, lanes: ev.threads,
           memory: ev.memory ?? ev.cost?.memory_reserved, exclusive: ev.exclusive || [], started: ev.started,
           ended: ev.ended, seconds: ev.seconds, note: ev.reason, cost: ev.cost, peak: ev.cost?.memory_peak };
}

// every job the dashboard knows: queued first (in order), then running, then
// the ended ones, newest first
export function allJobs(state, runs) {
  const running = state?.running || [];
  const ids = new Set(running.map((r) => r.id));
  const ended = Object.values(runs).filter((r) => r.ended && !ids.has(r.id)).sort((a, b) => b.ended - a.ended);
  return [...(state?.queue || []).map(fromQueue), ...running.map((r) => fromRunning(r, runs[r.id])),
          ...ended.map(fromEnded)];
}

// what a job is, in its Benchfile's words (`about`), or ""
export function aboutOf(state, project, job) {
  return state?.projects?.[project]?.abouts?.[job] || "";
}

export function statusText(j) {
  if (j.state === "ok") return "ok";
  if (j.state === "refused") return "refused";
  if (j.state === "unavailable") return "unavailable (75)";
  if (j.state === "cancelled") return "cancelled (130)";
  if (j.state === "timeout") return "timeout (124)";
  return `exit ${j.status}`;
}
