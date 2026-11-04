<script>
  // the admin task queue (long operations, run by mcu-task.service), a
  // page of it with filters (20 a page), reloaded on each task event of the
  // live stream; the chevron opens a console under a task's row, following
  // its log while it runs; a task not started yet can be cancelled
  import { get, post } from "./api.js";
  import { live, refreshState } from "./live.svelte.js";
  import Chips from "./widgets/Chips.svelte";
  import Pager from "./widgets/Pager.svelte";
  import { say } from "./widgets/toast.svelte.js";
  let tasks = $state([]);
  let active = $state([]);
  let page = $state({ total: 0, offset: 0, limit: 20, projects: [] });
  let tf = $state({ state: "", kind: "", project: "", offset: 0 });
  let log = $state(null);   // { id, text }
  const setFilter = (k, v) => (tf = { ...tf, [k]: v, offset: 0 });
  async function load() {
    const q = new URLSearchParams({ offset: tf.offset, limit: 20 });
    for (const k of ["state", "kind", "project"]) if (tf[k]) q.set(k, tf[k]);
    let r;
    try { r = await get(`/api/admin/tasks?${q}`); } catch { return; }
    // a task that just ended changed the box: show its result now
    const ended = active.some((o) => !r.active.some((t) => t.id === o.id));
    tasks = r.tasks;
    active = r.active;
    page = { total: r.total, offset: r.offset, limit: r.limit, projects: r.projects };
    if (ended) refreshState().catch(() => {});
  }
  $effect(() => { void live.tasks; void tf; load(); });
  async function showLog(id) {
    try { log = { id, text: (await get(`/api/admin/tasks?log=${id}`)).log }; } catch {}
  }
  let open = $state(null);
  let consoleEl = $state(null);
  const toggle = (id) => { open = open === id ? null : id; if (open) showLog(open); };
  $effect(() => { void live.tasks; if (open) showLog(open); });
  $effect(() => { void log; if (consoleEl) queueMicrotask(() => (consoleEl.scrollTop = consoleEl.scrollHeight)); });
  async function cancel(id) {
    try { say((await post("/api/admin/task", { kind: "cancel", id })).note); } catch (e) { say(e.message, true); }
    load();
  }
</script>

<div class="panel">
  <p class="mute small">Long operations (move, purge, reclaim, prune, the eGPU's eject, a restart or power-off) run here in the background, in order within a project; a task not started yet (queued, or waiting for a job to free its unit) can be cancelled.</p>
  <div class="filters">
    <Chips label="state" value={tf.state} onchange={(v) => setFilter("state", v)}
           options={[["", "all"], ["active", `active${active.length ? ` (${active.length})` : ""}`], ["done", "done"], ["failed", "failed"], ["cancelled", "cancelled"]]} />
    <Chips label="kind" value={tf.kind} onchange={(v) => setFilter("kind", v)}
           options={[["", "all"], ["migrate", "move"], ["purge", "purge"], ["reclaim", "reclaim"], ["prune", "prune"], ["gpu", "eGPU"], ["shutdown", "restart"]]} />
    {#if page.projects.length > 1}
      <Chips label="project" value={tf.project} onchange={(v) => setFilter("project", v)}
             options={[["", "all"], ...page.projects.map((p) => [p, p])]} />
    {/if}
  </div>
  <table>
    <thead><tr><th></th><th>task</th><th>what</th><th>state</th><th>progress</th><th>took</th><th></th></tr></thead>
    <tbody>
      {#each tasks as t (t.id)}
        <tr>
          <td><button class="link chev" aria-expanded={open === t.id} aria-label="show the log"
                      onclick={() => toggle(t.id)}>{open === t.id ? "▾" : "▸"}</button></td>
          <td class="mono small">{t.id}</td>
          <td>{t.kind} <span class="mono small">{t.args.filter((a) => a !== "--run").join(" ")}</span></td>
          <td class:ok={t.state === "done"} class:bad={t.state === "failed" || t.state === "interrupted"} class:warn={t.state === "running" || t.state === "waiting"}>{t.state}</td>
          <td class="prog">
            <div class="bar" class:failed={t.state === "failed"}><span style:width="{t.state === "done" ? 100 : (t.percent ?? 0)}%"></span></div>
            <span class="small mono">{t.state === "done" ? "100" : (t.percent ?? 0)} %</span>
            <div class="small mute ellip" title={t.progress || ""}>{t.progress || ""}</div>
          </td>
          <td class="small">{t.started ? Math.round(((t.ended || Date.now() / 1000) - t.started)) + " s" : "-"}</td>
          <td>{#if t.state === "queued" || t.state === "waiting"}<button class="link small" onclick={() => cancel(t.id)}>cancel</button>{/if}</td>
        </tr>
        {#if open === t.id}
          <tr class="console-row"><td colspan="7"><pre class="console" bind:this={consoleEl}>{log?.id === t.id ? log.text || "(no output yet)" : "…"}</pre></td></tr>
        {/if}
      {:else}
        <tr><td colspan="7" class="mute">{tf.state || tf.kind || tf.project ? "no task matches these filters" : "no task yet"}</td></tr>
      {/each}
    </tbody>
  </table>
  <Pager offset={page.offset} limit={page.limit} total={page.total} onchange={(o) => (tf = { ...tf, offset: o })} />
</div>

<style>
  .filters { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 6px 0 10px; }
  .chev { font-size: 14px; width: 22px; padding: 0; color: var(--mute); }
  .prog { min-width: 220px; }
  .bar { display: inline-block; width: 140px; height: 8px; background: var(--free); border-radius: 4px; overflow: hidden; vertical-align: middle; margin-right: 6px; }
  .bar span { display: block; height: 100%; background: var(--ok); transition: width .4s; }
  .bar.failed span { background: var(--bad); }
  .ellip { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .console-row td { padding: 0 0 10px; }
  .console { margin: 0; max-height: 320px; overflow: auto; background: #0b0e14; color: #c9d1d9; padding: 10px 12px;
             border-radius: 6px; font: 12px/1.45 ui-monospace, monospace; white-space: pre-wrap; }
  button.link { border: none; background: none; color: var(--accent, #3b6fd8); cursor: pointer; }
</style>
