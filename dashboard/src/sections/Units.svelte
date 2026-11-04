<script>
  // the storage units (storage/<name>, images, results, volume/<name>), each
  // on a pool, and their moves as tasks; every project's, or one's
  import { gb, color } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import { tasks, queueTask } from "../lib/admin.svelte.js";
  let { project = null } = $props();
  const rows = $derived(Object.entries(live.state?.projects || {})
    .filter(([p]) => !project || p === project)
    .flatMap(([p, v]) => (v.units || []).map((u) => [p, u])));
  const pools = $derived(Object.keys(live.state?.pools || {}));
  // a storage's stated end, as its last mount wrote it
  const ends = (u) => {
    if (!u.unit.startsWith("storage/") || u.unit === "storage/.git") return "";
    const l = u.life;
    if (!l) return "no end stated";
    return [l.drop && "after the job", l.until && "until its branch goes", l.ttl && `${(l.ttl / 86400).toFixed(0)} d unused`,
            l.max && `cap ${gb(l.max)}`].filter(Boolean).join(", ");
  };
</script>

<div class="panel">
  <p class="mute small">Each unit lives on a pool of its own and moves as a background task: if a job uses it, the move waits (new jobs on that unit wait for the move) and can be cancelled until it starts.</p>
  <div class="scroll">
  <table>
    <thead><tr>{#if !project}<th>project</th>{/if}<th>unit</th><th>kind</th><th>size</th><th>ends</th><th>on</th>{#if pools.length > 1}<th>move to</th>{/if}</tr></thead>
    <tbody>
      {#each rows as [p, u] (p + u.unit)}
        {@const moving = tasks.active.find((t) => t.kind === "migrate" && t.args[0] === p && (t.args.length === 2 || t.args[1] === u.unit))}
        <tr>
          {#if !project}<td style:color={color(p)}><a href="#/p/{p}/storage" style:color={color(p)}><b>{p}</b></a></td>{/if}
          <td class="mono small">{u.unit}</td>
          <td class="small">{u.kind}</td>
          <td class="small">{gb(u.bytes)}</td>
          <td class="small" class:warn={u.unit.startsWith("storage/") && u.unit !== "storage/.git" && !u.life}>{ends(u)}</td>
          <td class="small">{u.pool}</td>
          {#if pools.length > 1}
          <td><span class="btns">
            {#if moving}<span class="warn small" title={moving.progress || ""}>move {moving.state}…</span>{/if}
            {#each pools.filter((n) => n !== u.pool) as n}
              <button class="small" disabled={!!moving} onclick={() => ask(`Move ${p}'s ${u.unit} to ${n}?`,
                u.unit === "images" ? "Its images are dropped there and rebuilt by its next job." : `${gb(u.bytes)} copied in the background; refused while a job uses it; its agent is told.`,
                () => queueTask({ kind: "migrate", project: p, unit: u.unit, pool: n }))}>{n}</button>
            {/each}
          </span></td>
          {/if}
        </tr>
      {:else}
        <tr><td colspan="7" class="mute">no unit measured yet (the usage timer runs every 5 minutes)</td></tr>
      {/each}
    </tbody>
  </table>
  </div>
</div>

<style>
  .small { font-size: 12px; }
  .scroll { overflow-x: auto; }
</style>
