<script>
  // one run: what ran (parameters, commits, Benchfile), its result and its
  // cost, with the CPU and memory curve over the run
  import { get, dur, gb, color } from "../lib/api.js";
  import Chart from "../lib/Chart.svelte";
  import { live } from "../lib/live.svelte.js";
  import { aboutOf, fromQueue } from "../lib/jobs.js";
  let { project, id } = $props();
  let d = $state(null);
  let error = $state("");
  // a job still in the queue has no run yet: what it asks and why it waits,
  // from the live state; once it starts, the run's own record takes over
  const queued = $derived.by(() => {
    const q = (live.state?.queue || []).find((e) => e.id === id);
    return q ? { ...fromQueue(q), position: (live.state.queue || []).indexOf(q) + 1 } : null;
  });
  $effect(() => {
    if (queued) return;
    get(`/api/runs/${project}/${id}`).then((x) => (d = x)).catch((e) => (error = e.message));
  });
  const curve = $derived(d?.cost?.curve || []);
</script>

{#if queued}
  <h1><a href="#/p/{project}" style:color={color(project)}>{project}</a> / {queued.job} <span class="mute">{id}</span></h1>
  <p><b class="warn">waiting</b> · position {queued.position} in the queue · since {new Date(queued.queued * 1000).toLocaleTimeString()}</p>
  <p>{queued.note}</p>
  <p class="mute">asks {queued.threads} threads, {gb(queued.memory)}{queued.exclusive.length ? `, ${queued.exclusive.join(", ")}` : ""}{queued.params ? ` · ${queued.params}` : ""}</p>
{:else if error}<p class="bad">{error}</p>{:else if d}
  {@const e = d.event || {}}
  {@const c = d.cost || {}}
  <h1><a href="#/p/{project}" style:color={color(project)}>{project}</a> /
      <a href="#/p/{project}/jobs/{e.job || c.job}">{e.job || c.job}</a> <span class="mute mono">{id}</span></h1>
  {#if aboutOf(live.state, project, e.job || c.job)}<p class="mute">{aboutOf(live.state, project, e.job || c.job)}</p>{/if}
  <div class="panel grid">
    <div><span class="mute">result</span>
      <b class={e.status === 0 ? "ok" : e.status === 75 || e.unavailable ? "warn" : "bad"}>{e.kind === "refused" ? "refused" : e.status === 0 ? "ok" : e.status === 75 ? "unavailable (exit 75, retry later)" : `exit ${e.status}`}</b>
      {#if e.reason}<div class="warn">{e.reason}</div>{/if}
      {#if e.summary?.tests}<div>{e.summary.tests.passed} passed{#if e.summary.tests.failed}, <span class="bad">{e.summary.tests.failed} failed</span>{/if}{#if e.summary.tests.skipped}, {e.summary.tests.skipped} skipped{/if}</div>{/if}
      {#each e.summary?.failures || [] as f}<div class="small mono bad">{f.test}: {f.message}</div>{/each}
      {#each e.summary?.errors || [] as l}<div class="small mono bad">{l}</div>{/each}</div>
    <div><span class="mute">when</span> {e.t ? new Date(e.t * 1000).toLocaleString() : "-"}</div>
    <div><span class="mute">duration</span> {dur(e.seconds)}</div>
    <div><span class="mute">what</span> {e.branch || Object.entries(e.params || {}).map(([k, v]) => `${k}=${v}`).join(" ") || "-"}
      {#if e.benchfile}<span class="mute"> · Benchfile {e.benchfile_label ? `${e.benchfile_label} ` : ""}v{e.benchfile}</span>{/if}</div>
    <div><span class="mute">commits</span>
      {#each Object.entries(e.commits || {}) as [repo, sha]}<div class="mono">{repo} {sha.slice(0, 12)}</div>{:else}-{/each}</div>
    <div><span class="mute">CPU</span> {c.cpu_seconds ?? "-"} s · {c.cpu_use ?? "-"} % of {c.threads ?? "?"} threads</div>
    <div><span class="mute">memory peak</span> {gb(c.memory_peak)}</div>
    <div><span class="mute">disk</span> read {((c.read_bytes || 0) / 1e6).toFixed(0)} MB · written {((c.write_bytes || 0) / 1e6).toFixed(0)} MB</div>
  </div>
  {#if c.conditions}
    {@const k = c.conditions}
    <h2>Conditions</h2>
    <div class="panel grid">
      <div><span class="mute">comparable</span>
        {#if k.comparable}<b class="ok">yes</b>{:else}<b class="warn">no</b>{#each k.flags as f}<div class="warn">{f}</div>{/each}{/if}</div>
      <div><span class="mute">CPU temperature</span> {k.cpu_temp?.max ?? "-"} °C max · {k.cpu_temp?.mean ?? "-"} mean</div>
      <div><span class="mute">frequency</span> {k.cpu_mhz_mean ?? "-"} MHz mean on its threads</div>
      <div><span class="mute">governor · turbo</span> {k.governor?.join(" → ")} · {k.turbo?.map((t) => (t ? "on" : "off")).join(" → ")}</div>
      <div><span class="mute">throttling</span>
        {#if k.throttle_seconds != null}{k.throttle_seconds} s{#if k.throttle_share != null} ({(k.throttle_share * 100).toFixed(1)} % of the run){/if} · {/if}{k.throttle_events ?? "-"} events</div>
      <div><span class="mute">machine</span> {k.machine_held ? "held (alone)" : "shared"}</div>
      <div><span class="mute">other jobs meanwhile</span>
        {#each k.overlaps as o}<div><span style:color={color(o.project)}>{o.project}</span>/{o.job}{o.exclusive?.length ? ` (${o.exclusive.join(", ")})` : ""}</div>{:else}none{/each}</div>
    </div>
  {/if}
  {#if curve.length > 1}
    <h2>Over the run</h2>
    <div class="panel">
      <Chart data={[curve.map((p) => p[0]), curve.map((p) => p[1]), curve.map((p) => p[2] / 2 ** 30)]}
             series={[{ label: "CPU %", stroke: "#3b6fd8", width: 2, scale: "%" },
                      { label: "memory GB", stroke: "#d8613b", width: 2, scale: "gb" }]}
             scales={{ "%": { range: [0, 100] }, gb: {} }} />
    </div>
  {/if}
{/if}

<style>
  h1 { font-size: 20px; margin: 4px 0 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px 20px; }
  .grid > div > .mute:first-child { display: block; font-size: 12px; }
</style>
