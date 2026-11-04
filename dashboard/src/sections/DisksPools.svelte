<script>
  // the machine's filesystems, the pools made of them (a budget each: the
  // quotas the box may promise there), and every project's units on them
  import { gb } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import { postNote } from "../lib/admin.svelte.js";
  import Setting from "../lib/widgets/Setting.svelte";
  import Units from "./Units.svelte";
  const pools = $derived(live.state?.pools || {});
  const poolAt = (mount) => Object.keys(pools).find((n) => (pools[n].mount ?? pools[n].path) === mount);
</script>

<h2>Partitions and pools</h2>
<div class="panel">
  <p class="mute small">What the machine really has. Give a partition a budget (the quotas the box may promise there); 0 disables it: no new project goes there, the ones on it stay until moved. The system disk is never offered; the box never formats nor edits fstab.</p>
  {#each live.state?.disks?.filesystems || [] as fs (fs.mount)}
    {@const pname = poolAt(fs.mount)}
    {@const pl = pname ? pools[pname] : null}
    <div class="part">
      <div><b class="mono">{fs.mount}</b> <span class="mute small">{fs.device} · {fs.fstype} · {gb(fs.free)} free of {gb(fs.size)}</span>
        {#if fs.system}<span class="warn small"> · the system's disk: projects here are policed by their user quota; keep the budget well under the free space</span>{/if}
        {#if pl}<span class="small"> · pool <b>{pname}</b>{#if pl.media} <span class="media {pl.media}">{pl.media.toUpperCase()}</span>{/if}{#if pl.default} <span class="ok">default</span>{/if} · promised {gb(pl.quotas)} · {(pl.projects || []).join(", ") || "no project"}</span>{/if}
        {#if pl && !pl.default && pl.media !== "hdd"}<button class="link small" onclick={() => ask(`Make ${pname} the default?`, "New projects go there first.",
          () => postNote("/api/admin/pool", { action: "default", name: pname }))}>make default</button>{/if}
      </div>
      {#if fs.usable}
        <Setting label="budget" about={pl ? "" : "not used yet"} kind="range" value={pl ? Math.round((pl.budget ?? fs.size) / 2 ** 30) : 0}
                 min={0} max={Math.floor(fs.size / 2 ** 30)} unit=" GB" format={(v) => (v === 0 ? "disabled" : `${v} GB`)}
                 onset={(g) => postNote("/api/admin/pool", { action: "budget", path: fs.mount, size: `${g}g` })} />
      {:else}
        <p class="warn small">not usable: {fs.why}</p>
      {/if}
    </div>
  {:else}
    <p class="mute">no filesystem besides the system's</p>
  {/each}
  {#each (live.state?.disks?.system || []).filter((x) => x.mount !== "/") as fs (fs.mount)}
    <div class="part sys">
      <b class="mono">{fs.mount}</b> <span class="mute small">{fs.device} · {fs.fstype} · {gb(fs.free)} free of {gb(fs.size)}</span>
      <p class="mute small">the system's own filesystem: not offered to projects (no project quotas, and a full system disk would stop the box)</p>
    </div>
  {/each}
  {#each live.state?.disks?.vg_free || [] as v}
    <p class="small"><b>LVM volume group</b> <span class="mono">{v.vg}</span>: {gb(v.free)} unallocated <span class="mute">(make a volume by hand: lvcreate, mkfs.xfs, mount with prjquota)</span></p>
  {/each}
  {#each live.state?.disks?.blank_disks || [] as d}
    <p class="small"><b>blank disk</b> <span class="mono">{d.device}</span>, {gb(d.size)} <span class="mute">(partition and format it by hand first)</span></p>
  {/each}
</div>

<h2>Units</h2>
<Units />

<style>
  .small { font-size: 12px; }
  .part { padding: 8px 0; border-bottom: 1px solid var(--line); }
  .part.sys { opacity: .6; }
  .media { display: inline-block; margin-left: 4px; padding: 0 6px; border-radius: 4px; font: 600 11px ui-monospace, monospace;
           border: 1px solid var(--line); vertical-align: 1px; }
  .media.hdd { color: var(--warn); border-color: var(--warn); }
  .media.ssd { color: var(--accent, #3b6fd8); border-color: var(--accent, #3b6fd8); }
  button.link { border: none; background: none; color: var(--accent, #3b6fd8); }
</style>
