<script>
  // one project's room: what it uses against its quota per pool type, the
  // admin's sticky quota, its units and their moves, and freeing room
  // (reclaim: what regenerates; purge: all but key, settings, Benchfile)
  import { gb } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import { postNote, queueTask, busyTask } from "../lib/admin.svelte.js";
  import Setting from "../lib/widgets/Setting.svelte";
  import Units from "./Units.svelte";
  let { project } = $props();
  const v = $derived(live.state?.projects?.[project] || {});
  const hddPools = $derived(Object.values(live.state?.pools || {}).filter((pl) => pl.media === "hdd"));
  // the admin may give up to twice what a project may ask for itself
  const maxQuota = $derived(2 * (parseInt(live.state?.envelope?.max_disk_ssd ?? live.state?.envelope?.max_disk) || 60));
  const maxHdd = $derived(Math.floor(hddPools.reduce((a, pl) => a + (pl.budget || pl.size || 0), 0) / 2 ** 30) || 1);
  const setQuota = (size, media = "ssd") => postNote("/api/admin/quota", { project, size, media });
  let freeing = $state(null);   // { level, reason, volumes }
  const free = () => { const f = freeing; freeing = null;
    return queueTask({ kind: f.level, project, reason: f.reason.trim(), volumes: f.volumes }); };
  const busy = $derived(busyTask(project));
</script>

<h2>Quota</h2>
<div class="panel">
  <p><span class="mono small">{hddPools.length ? "SSD" : "disk"}</span> {gb(v.used)} of {gb(v.quota)}
    <span class="mute small">· from {v.quota_source?.ssd || "default"}</span></p>
  <Setting label={hddPools.length ? "SSD quota" : "quota"} about="yours sticks over the project's own until reset" kind="range"
           value={Math.round((v.quota || 0) / 2 ** 30)} min={1} max={maxQuota} unit=" GB"
           overridden={!!v.admin_disk} resetLabel={`back to the project's ${v.disk || "default"}`}
           onset={(g) => setQuota(`${g}g`)} onreset={() => setQuota("none")} />
  {#if hddPools.length}
    {@const h = v.media?.hdd || { used: 0, quota: 0 }}
    <p><span class="mono small">HDD</span> {gb(h.used)} of {h.quota ? gb(h.quota) : "none"} <span class="mute small">· from {v.quota_source?.hdd || "default"}</span></p>
    <Setting label="HDD quota" kind="range" value={Math.round((h.quota || 0) / 2 ** 30)} min={0} max={maxHdd} unit=" GB"
             format={(g) => (g === 0 ? "none" : `${g} GB`)}
             overridden={!!v.admin_disk_hdd} resetLabel={`back to the project's ${v.disk_hdd || "none"}`}
             onset={(g) => setQuota(`${g}g`, "hdd")} onreset={() => setQuota("none", "hdd")} />
  {/if}
</div>

<h2>Units</h2>
<Units {project} />

<h2>Freeing room</h2>
<div class="panel">
  {#if v.parts}<p class="small">reclaimable {gb(v.parts.reclaimable)} · a purge frees {gb(v.parts.purgeable)}
    <span class="mute">(measured {new Date(v.parts.measured * 1000).toLocaleTimeString()})</span></p>{/if}
  {#if busy}<p class="warn small">{busy.kind} {busy.state}…</p>{/if}
  {#if !freeing}
    <div class="btns">
      <button disabled={!!busy} onclick={() => (freeing = { level: "reclaim", reason: "", volumes: false })} title="free what the project can regenerate">reclaim…</button>
      <button class="danger" disabled={!!busy} onclick={() => (freeing = { level: "purge", reason: "", volumes: false })} title="free everything but its key, settings and Benchfile">purge…</button>
    </div>
  {:else}
    {#if v.parts}
      {@const P = v.parts}
      <table class="parts">
        <tbody>
          <tr class="gone"><td>images and build leftovers</td><td>{gb(P.images)}</td><td>reclaim, purge</td></tr>
          <tr class="gone"><td>storage tagged tmp</td><td>{gb(P.tmp_storage)}</td><td>reclaim, purge</td></tr>
          <tr class="gone"><td>results older than 2 days</td><td>{gb(P.old_results)}</td><td>reclaim, purge</td></tr>
          <tr class:gone={freeing.level === "purge"}><td>storage not tmp, git cache</td><td>{gb(P.kept_storage)}</td><td>purge</td></tr>
          <tr class:gone={freeing.level === "purge"}><td>src (repository mode)</td><td>{gb(P.src)}</td><td>purge</td></tr>
          <tr class:gone={freeing.level === "purge"}><td>recent results</td><td>{gb(P.recent_results)}</td><td>purge</td></tr>
          <tr class:gone={freeing.level === "purge" && freeing.volumes}><td>its volumes</td><td>{gb(P.volumes)}</td><td>purge with "its volumes too"</td></tr>
          <tr class="gone"><td><b>this {freeing.level} frees about</b></td>
              <td><b>{gb(freeing.level === "reclaim" ? P.reclaimable : P.purgeable + (freeing.volumes ? P.volumes : 0))}</b></td><td></td></tr>
        </tbody>
      </table>
    {/if}
    <p class="mute">{freeing.level === "reclaim"
      ? "Removes what the project regenerates: its images and build leftovers, its storage tagged tmp, results older than 2 days. Its next job rebuilds them."
      : "Removes everything but its key, settings and Benchfile: images, every cache, src, all results."}
      Refused while it has jobs queued or running. Its agent is told, with the reason, until its next successful job.</p>
    <div class="row reason">
      <input bind:value={freeing.reason} placeholder="reason, shown to the project's agent" maxlength="200" aria-label="reason" />
      {#if freeing.level === "purge"}<label><input type="checkbox" bind:checked={freeing.volumes} /> its volumes too</label>{/if}
      <button class="danger" onclick={() => freeing.level === "purge"
        ? ask(`Purge ${project}?`, `Everything but its key, settings and Benchfile is deleted${freeing.volumes ? ", its volumes too" : ""}. This cannot be undone.`, free, project)
        : ask(`Reclaim ${project}?`, "Its images, tmp storage and old results are deleted; its next job rebuilds them.", free)}>{freeing.level} now</button>
      <button onclick={() => (freeing = null)}>cancel</button>
    </div>
  {/if}
</div>

<style>
  .small { font-size: 12px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .reason input { flex: 1; min-width: 200px; }
  .reason label input { flex: none; min-width: 0; }
  .parts { margin: 6px 0 10px; font-size: 13px; }
  .parts td { padding: 2px 14px 2px 0; }
  .parts tr:not(.gone) td { opacity: .45; }   /* kept by this action */
</style>
