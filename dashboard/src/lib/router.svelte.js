// a hash router:
//   #/ (the graph), #/jobs (the list)
//   #/p (the projects), #/p/<project>[/runs|storage|settings|access], #/p/<project>/jobs/<job>
//   #/r/<project>/<id> (a run)
//   #/machine[/overview|stats|disks|power] (#/unit, its older name, still leads there)
//   #/tasks, #/settings[/config|limits[/<key>]|housekeeping]
// Older routes are rewritten in place, so links and bookmarks keep working
export const route = $state({ parts: [] });

export const PROJECT_TABS = ["runs", "storage", "settings", "access"];

function moved(parts) {
  const [a, b, c] = parts;
  if (a === "unit") {
    if (b === "tasks") return ["tasks"];
    return ["machine", ...(b === "system" || b === "info" ? ["overview"] : b ? [b] : [])];
  }
  if (a === "machine" && (b === "system" || b === "info")) return ["machine", "overview"];
  if (a === "admin") {
    return {
      projects: ["p"], disk: ["machine", "disks"], timing: ["machine", "power"], hardware: ["settings", "limits"],
      queue: ["settings", "limits"], envelope: ["settings", "limits"], housekeeping: ["settings", "housekeeping"],
    }[b] || ["settings"];
  }
  // #/p/<project>/<job>: a job's page before the project's tabs
  if (a === "p" && b && c && parts.length === 3 && !PROJECT_TABS.includes(c) && c !== "jobs") return ["p", b, "jobs", c];
  return null;
}

function read() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const to = moved(parts);
  if (to) {
    history.replaceState(null, "", "#/" + to.map(encodeURIComponent).join("/"));
    route.parts = to;
    return;
  }
  route.parts = parts;
}
window.addEventListener("hashchange", read);
read();

export const go = (...parts) => (location.hash = "#/" + parts.map(encodeURIComponent).join("/"));
