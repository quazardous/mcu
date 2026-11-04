<script>
  // the jobs as a table (#/jobs), beside the timeline (#/): queued first, in
  // the scheduler's order, then running, then the ended ones newest first;
  // the same fields as the timeline's tooltips, filtered by state and
  // project, 50 a page; a row opens its run (a queued one: why it waits)
  import { dur, gb, post } from "../lib/api.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import { say } from "../lib/widgets/toast.svelte.js";
  import { live } from "../lib/live.svelte.js";
  import { allJobs, statusText, aboutOf } from "../lib/jobs.js";
  import Chips from "../lib/widgets/Chips.svelte";
  import Pager from "../lib/widgets/Pager.svelte";
  const LIMIT = 50;
  let f = $state({ state: "", project: "", offset: 0 });
  const setFilter = (k, v) => (f = { ...f, [k]: v, offset: 0 });
  let now = $state(Date.now() / 1000);
  $effect(() => { const t = setInterval(() => (now = Date.now() / 1000), 1000); return () => clearInterval(t); });
  const GROUPS = { active: ["queued", "running"], ok: ["ok"], failed: ["failed", "timeout", "unavailable", "refused"], cancelled: ["cancelled"] };
  const jobs = $derived.by(() => { void live.version; return allJobs(live.state, live.runs); });
  const projects = $derived([...new Set(jobs.map((j) => j.project))].sort());
  const shown = $derived(jobs.filter((j) => (!f.state || GROUPS[f.state].includes(j.state)) && (!f.project || j.project === f.project)));
  const page = $derived(shown.slice(f.offset, f.offset + LIMIT));
  const count = (g) => jobs.filter((j) => GROUPS[g].includes(j.state)).length;
  const clock = (t) => (t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
  const took = (j) => (j.state === "running" ? dur(now - j.started) : j.seconds != null ? dur(j.seconds) : "");
  // the peak against the reservation: within it, far under it, above it
  const fit = (j) => {
    const p = j.peak ?? j.used;
    if (!p || !j.memory) return "";
    return p > j.memory ? "over" : p < j.memory / 2 ? "under" : "fits";
  };
  // a job stopped from here, as its project would (exit 130 for it)
  const cancel = (j) => ask(`Cancel ${j.project}/${j.job}?`,
    `${j.state === "running" ? "It is stopped (SIGTERM, then SIGKILL after 10 s); its log and /out stay." : "It leaves the queue."} The project's agent sees exit 130.`,
    async () => { try { say((await post("/api/admin/cancel", { project: j.project, id: j.id })).note); } catch (e) { say(e.message, true); } });
  const result = (j) => (j.state === "queued" ? j.note : j.state === "running" ? `threads ${(j.lanes || []).join(",")}` : statusText(j) + (j.note && j.note !== j.state ? ` · ${j.note}` : ""));
</script>

<div class="panel">
  <div class="filters">
    <Chips label="state" value={f.state} onchange={(v) => setFilter("state", v)}
           options={[["", "all"], ["active", `active (${count("active")})`], ["ok", "ok"], ["failed", `failed (${count("failed")})`], ["cancelled", "cancelled"]]} />
    {#if projects.length > 1}
      <Chips label="project" value={f.project} onchange={(v) => setFilter("project", v)}
             options={[["", "all"], ...projects.map((p) => [p, p])]} />
    {/if}
  </div>
  <table>
    <thead><tr><th>state</th><th>job</th><th>parameters</th><th class="num">threads</th><th class="num">reserved</th><th class="num" title="green: within the reservation; yellow: under half of it (reserves too much); red: above it">peak</th>
      <th>queued / started</th><th class="num">took</th><th>result</th><th class="num">CPU</th><th></th></tr></thead>
    <tbody>
      {#each page as j (j.id)}
        <tr class="link" onclick={() => (location.hash = `#/r/${j.project}/${j.id}`)}>
          <td><span class="st {j.state}">{j.state}</span></td>
          <td title={aboutOf(live.state, j.project, j.job)}><b>{j.project}</b>/{j.job}
            {#if aboutOf(live.state, j.project, j.job)}<div class="mute small ellip">{aboutOf(live.state, j.project, j.job)}</div>{/if}</td>
          <td class="mono small ellip" title={j.params}>{j.params}</td>
          <td class="num">{j.threads ?? ""}</td>
          <td class="num">{j.memory ? gb(j.memory) : ""}</td>
          <td class="num small {fit(j)}" title={j.used != null ? `now ${gb(j.used)}` : ""}>{j.peak ? gb(j.peak) : j.used != null ? gb(j.used) : ""}</td>
          <td class="small">{clock(j.started ?? j.queued)}</td>
          <td class="num small">{took(j)}</td>
          <td class="small ellip" title={result(j)}>{result(j)}</td>
          <td class="num small">{j.cost ? `${j.cost.cpu_seconds} s · ${j.cost.cpu_use} %` : ""}</td>
          <td>{#if j.state === "queued" || j.state === "running" || j.state === "waiting"}
            <button class="small" onclick={(e) => { e.stopPropagation(); cancel(j); }}>cancel</button>{/if}</td>
        </tr>
      {:else}
        <tr><td colspan="11" class="mute">{f.state || f.project ? "no job matches these filters" : "no job yet"}</td></tr>
      {/each}
    </tbody>
  </table>
  <Pager offset={f.offset} limit={LIMIT} total={shown.length} onchange={(o) => (f = { ...f, offset: o })} />
  <p class="mute small">The box's recent jobs, as the dashboard has seen them (the last 800 events, then live).</p>
</div>

<style>
  .filters { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 6px 0 10px; }
  .num { text-align: right; white-space: nowrap; }
  .ellip { max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tr.link { cursor: pointer; }
  tr.link:hover td { background: var(--free, rgba(127, 127, 127, 0.08)); }
  .st { font: 600 11px ui-monospace, monospace; padding: 1px 6px; border-radius: 4px; background: var(--free); }
  .st.running { color: var(--warn); }
  .st.queued { color: var(--mute); }
  .st.ok { color: var(--ok); }
  .st.failed, .st.timeout, .st.refused, .st.unavailable { color: var(--bad); }
  .st.cancelled { color: var(--mute); }
  td.fits { color: var(--ok); }
  td.under { color: #d4b106; }
  td.over { color: var(--bad); font-weight: 600; }
</style>
