<script>
  // a job's history: duration, CPU time and memory peak per run, with the
  // p10-p90 band of its successful runs; failed runs marked; a click on a
  // point opens the run (Bencher / asv / Chromium perf ideas)
  import { get, dur, gb, color } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { go } from "../lib/router.svelte.js";
  import Chart from "../lib/Chart.svelte";
  import { aboutOf } from "../lib/jobs.js";
  let { project, job } = $props();
  let runs = $state([]);
  $effect(() => {
    void live.version;
    get(`/api/projects/${project}/jobs/${job}`).then((x) => (runs = x.runs));
  });
  const q = (xs, p) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : null; };
  function chart(get1, label, fmt) {
    const xs = runs.map((r) => r.t);
    const ys = runs.map((r) => (r.kind === "done" ? get1(r) : null));
    const okv = runs.filter((r) => r.status === 0).map(get1).filter((v) => v != null);
    const lo = q(okv, 0.1), hi = q(okv, 0.9);
    const failed = runs.map((r) => (r.status !== 0 ? get1(r) ?? 0 : null));
    return {
      data: [xs, ys, xs.map(() => lo), xs.map(() => hi), failed],
      series: [
        { label, stroke: color(project), width: 2, points: { show: true, size: 6 }, value: (u, v) => fmt(v) },
        { label: "p10", stroke: "transparent", value: (u, v) => fmt(v) },
        { label: "p90", stroke: "transparent", value: (u, v) => fmt(v), fill: color(project) + "22", band: true },
        { label: "failed", stroke: "transparent", points: { show: true, size: 9, stroke: "#c0392b", fill: "#c0392b" } },
      ],
    };
  }
  const bands = (c) => ({ ...c });
  const ok = $derived(runs.filter((r) => r.status === 0).length);
</script>

<h1><a href="#/p/{project}" style:color={color(project)}>{project}</a> / {job}</h1>
{#if aboutOf(live.state, project, job)}<p class="mute">{aboutOf(live.state, project, job)}</p>{/if}
<p class="mute">{runs.length} runs · {ok} ok · the shaded band is the p10-p90 range of successful runs; red dots are failures; click a point to open the run</p>

{#if runs.length}
  <h2>Duration</h2>
  <div class="panel"><Chart {...chart((r) => r.seconds, "duration", dur)} onpoint={(i) => go("r", project, runs[i].id)} /></div>
  <h2>CPU time</h2>
  <div class="panel"><Chart {...chart((r) => r.cost?.cpu_seconds, "CPU seconds", (v) => (v == null ? "-" : v + " s"))} onpoint={(i) => go("r", project, runs[i].id)} /></div>
  <h2>Memory peak</h2>
  <div class="panel"><Chart {...chart((r) => (r.cost?.memory_peak ?? null) && r.cost.memory_peak / 2 ** 30, "memory GB", (v) => (v == null ? "-" : v.toFixed(2) + " GB"))} onpoint={(i) => go("r", project, runs[i].id)} /></div>
{:else}
  <p class="mute">no run yet</p>
{/if}

<style>h1 { font-size: 20px; margin: 4px 0; }</style>
