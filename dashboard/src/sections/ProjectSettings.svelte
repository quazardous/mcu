<script>
  // one project's settings, as the project set them (it manages itself:
  // config repo / manifest / disk, its Benchfiles), and its quota's source
  import { gb } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  let { project, detail = null } = $props();
  const v = $derived(live.state?.projects?.[project] || {});
  const SOURCE = { admin: "set by the admin (sticky, over the project's own)", "config disk": "the project's config disk",
                   default: "the box's default_disk (the project set none)" };
  const why = (s) => SOURCE[s] || (s?.startsWith("Benchfile") ? `its ${s} ([project] disk)` : s || "the default");
</script>

<div class="panel grid">
  <span class="k">source</span><span>{v.benchfile ? `Benchfile v${v.benchfile}` : v.repo ? "repository mode" : "not set yet"}</span>
  {#if Object.keys(v.benchfiles || {}).length}<span class="k">per branch</span><span>{Object.entries(v.benchfiles).map(([b, n]) => `${b} v${n}`).join(", ")}</span>{/if}
  <span class="k">repository</span><span class="mono">{v.repo || "—"}</span>
  <span class="k">SSD quota</span><span>{gb(v.quota)} · {why(v.quota_source?.ssd)}</span>
  {#if v.media?.hdd?.quota || v.disk_hdd || v.admin_disk_hdd}<span class="k">HDD quota</span><span>{gb(v.media?.hdd?.quota)} · {why(v.quota_source?.hdd)}</span>{/if}
  {#if detail}<span class="k">volumes</span><span>{Object.keys(detail.volumes || {}).join(", ") || "none"}</span>{/if}
</div>
<p class="mute small">The project sets these itself through its key (<span class="mono">config repo|manifest|disk</span>, <span class="mono">config benchfile</span>), within the <a href="#/settings/limits">limits</a>.
  Its quota has one writer: a Benchfile in use that sets <span class="mono">[project] disk</span>, else <span class="mono">config disk</span>; the admin's own, on Storage, sticks over both.</p>

<style>
  .grid { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; align-items: baseline; }
  .k { color: var(--mute); white-space: nowrap; }
  .small { font-size: 12px; }
</style>
