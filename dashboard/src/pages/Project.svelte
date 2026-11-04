<script>
  // a project, in tabs (#/p/<project>/<tab>): Runs, a heatmap of its jobs'
  // recent runs; Storage, its room and the admin's quota, units and freeing;
  // Settings, as the project set them; Access, its key
  // (colour = duration against that job's median; ✗ = failed, ⏸ = the box
  // could not serve it: GPU absent, maintenance, restart). One glance
  // tells what got slower or broke (the asv/Bencher idea)
  import { get, gb, dur, color } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { go } from "../lib/router.svelte.js";
  import ProjectStorage from "../sections/ProjectStorage.svelte";
  import ProjectSettings from "../sections/ProjectSettings.svelte";
  import ProjectAccess from "../sections/ProjectAccess.svelte";
  let { project, tab = "runs" } = $props();
  let d = $state(null);
  let error = $state("");
  $effect(() => {
    void live.version;
    get(`/api/projects/${project}`).then((x) => (d = x)).catch((e) => (error = e.message));
  });
  const median = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : 0; };
  function cell(run, med) {
    if (run.kind === "refused") return { bg: "var(--sys)", label: "∅" };
    if (run.status === 75 || run.unavailable) return { bg: "var(--warn)", label: "⏸" };
    if (run.status !== 0) return { bg: "var(--bad)", label: "✗" };
    const r = med ? run.seconds / med : 1;
    // green when faster than usual, red when slower, neutral around the median
    const hue = r <= 1 ? 140 : Math.max(0, 140 - (r - 1) * 280);
    const sat = Math.min(70, Math.abs(r - 1) * 140 + 15);
    return { bg: `hsl(${hue} ${sat}% 45%)`, label: "" };
  }
</script>

{#if error}<p class="bad">{error}</p>{:else if d}
  <h1 style:color={color(project)}>{project}</h1>
  {#if tab === "storage"}
    <ProjectStorage {project} />
  {:else if tab === "settings"}
    <ProjectSettings {project} detail={d} />
  {:else if tab === "access"}
    <ProjectAccess {project} />
  {:else}
  <div class="panel facts">
    <div><span class="mute">disk</span> {gb(d.used)} of {gb(d.quota)}{#if d.media?.hdd?.quota || d.media?.hdd?.used} · <span class="mute">hdd</span> {gb(d.media.hdd.used)} of {gb(d.media.hdd.quota)}{/if}</div>
    <div><span class="mute">source</span> {d.benchfile ? `Benchfile v${d.benchfile}` : d.repo || "not set"}{#if Object.keys(d.benchfiles || {}).length}<span class="mute"> · per branch:</span> {Object.entries(d.benchfiles).map(([b, n]) => `${b} v${n}`).join(", ")}{/if}</div>
    <div><span class="mute">key</span> {d.key ? "active" : "revoked"}</div>
    <div><span class="mute">volumes</span> {Object.keys(d.volumes).join(", ") || "none"}</div>
  </div>

  <h2>Last runs per job <span class="mute">(newest right; colour: duration vs the job's median)</span></h2>
  <div class="panel">
    <table class="heat">
      <tbody>
        {#each Object.entries(d.jobs) as [job, j]}
          {@const ok = j.last.filter((r) => r.status === 0 && r.seconds != null).map((r) => r.seconds)}
          {@const med = median(ok)}
          <tr>
            <td class="job"><a href="#/p/{project}/jobs/{job}">{job}</a>
              {#if j.about}<div class="about">{j.about}</div>{/if}
              <div class="mute">{j.ok}/{j.runs} ok · median {dur(med)} · mem {gb(j.memory_learned)}</div></td>
            <td class="cells">
              {#each j.last as run}
                {@const c = cell(run, med)}
                <button class="cell" style:background={c.bg} onclick={() => go("r", project, run.id)}
                        title="{new Date(run.t * 1000).toLocaleString()} · {run.kind === 'refused' ? 'refused: ' + run.reason : (run.status === 0 ? 'ok' : run.status === 75 ? 'unavailable: ' + (run.reason || 'retry later') : 'exit ' + run.status) + ' · ' + dur(run.seconds)}">{c.label}</button>
              {/each}
            </td>
          </tr>
        {:else}
          <tr><td class="mute">no run yet</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  {/if}
{/if}

<style>
  h1 { font-size: 22px; margin: 4px 0 12px; }
  .facts { display: flex; gap: 28px; flex-wrap: wrap; }
  .job { width: 260px; }
  .about { font-size: 12px; margin: 1px 0 2px; }
  .cells { display: flex; gap: 3px; flex-wrap: wrap; }
  .cell { width: 26px; height: 26px; border: none; border-radius: 4px; color: #fff; font-weight: 700; padding: 0; }
</style>
