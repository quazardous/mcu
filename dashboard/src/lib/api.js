// the box's API: session, JSON resources, and the SSE stream that keeps the
// shared state current
let csrf = null;
// called once when the server says the session is gone (expired, logged out
// elsewhere, password changed): the shell shows the login form again
let onExpired = () => {};
export const whenExpired = (fn) => (onExpired = fn);
const expired = () => { csrf = null; onExpired(); };
// npm run demo: made-up data, no box (lib/demo.js); left out of a real build
const DEMO = import.meta.env.MODE === "demo";

export async function get(path) {
  if (DEMO) return (await import("./demo.js")).demoGet(path);
  const r = await fetch(path, { credentials: "same-origin" });
  if (r.status === 401) {
    if (path !== "/api/session") expired();
    throw Object.assign(new Error("log in"), { status: 401 });
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

export async function post(path, body = {}) {
  if (DEMO) return (await import("./demo.js")).demoPost(path, body);
  const r = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(csrf ? { "X-Mcu-CSRF": csrf } : {}) },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/api/login") expired();
  if (!r.ok) throw new Error(data.error || r.statusText);
  if ("csrf" in data) csrf = data.csrf;
  return data;
}

export async function session() {
  const s = await get("/api/session");
  csrf = s.csrf;
  return s.logged_in;
}

// one SSE connection; `on(kind, ev)` for every event, reconnecting by itself
// (the browser sends Last-Event-ID)
export function connect(on, onStatus) {
  if (DEMO) {
    let stop = () => {};
    import("./demo.js").then((d) => (stop = d.demoConnect(on, onStatus)));
    return () => stop();
  }
  const es = new EventSource("/api/events");
  const kinds = ["queued", "waiting", "started", "done", "refused", "memory", "provisioned", "state", "box", "samples", "task"];
  for (const k of kinds) es.addEventListener(k, (m) => on(k, JSON.parse(m.data)));
  es.onopen = () => onStatus(true);
  // an error may be the network (EventSource retries by itself) or a dead
  // session (it would retry forever on 401): ask which
  es.onerror = () => {
    onStatus(false);
    fetch("/api/session", { credentials: "same-origin" }).then((r) => r.json())
      .then((s) => { if (!s.logged_in) { es.close(); expired(); } }).catch(() => {});
  };
  return () => es.close();
}

// a project's colour on the light pages: by rank among the box's projects,
// the same rank as the home page's neon (teal ~ cyan, magenta, orange, ...)
const palette = ["#1593a8", "#c2338f", "#d8613b", "#6a4fd1", "#3b6fd8", "#cf3f7a", "#2e9a6a", "#9a4fd1"];
let ranked = [];
export const setProjects = (names) => (ranked = [...names].sort());
const colors = {};
export const color = (p) => {
  const i = ranked.indexOf(p);
  return i >= 0 ? palette[i % palette.length] : (colors[p] ??= palette[(ranked.length + Object.keys(colors).length) % palette.length]);
};

export const gb = (b) => (b == null ? "?" : `${(b / 2 ** 30).toFixed(1)} GB`);
export const dur = (s) => (s == null ? "-" : s < 90 ? `${s.toFixed(s < 10 ? 1 : 0)} s`
  : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
export const ago = (t) => dur(Date.now() / 1000 - t);
