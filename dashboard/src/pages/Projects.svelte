<script>
  // the projects, one row each: key, disk used against quota, Benchfile
  // version, what runs and waits now, the last run; a row opens the project
  import { gb, color } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { invite } from "../lib/admin.svelte.js";
  import KeyPanel from "../sections/KeyPanel.svelte";
  let name = $state("");
  const P = $derived(live.state?.projects || {});
  const active = (p, list) => (live.state?.[list] || []).filter((e) => e.project === p).length;
  const lastRun = (p) => {
    const done = (live.state?.recent || []).filter((e) => e.kind === "done" && e.project === p);
    return done.length ? done[done.length - 1] : null;
  };
  const ago = (t) => {
    const s = Date.now() / 1000 - t;
    return s < 3600 ? `${Math.max(1, Math.round(s / 60))} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
</script>

<div class="head">
  <h1>Projects</h1>
  <form class="row" onsubmit={(e) => { e.preventDefault(); invite(name); name = ""; }}>
    <input bind:value={name} placeholder="project name" aria-label="project name" pattern="[a-z][a-z0-9-]{'{'}1,20{'}'}" required />
    <button title="creates the project and shows its key once; on an existing project, replaces its key">invite</button>
  </form>
</div>
<KeyPanel />
{#if !Object.keys(P).length}
  <div class="panel first">
    <p><b>No project yet.</b> A project is one user on this box with its own key, storage and queue share.
      Invite one above (or <span class="mono">mcu-admin invite &lt;project&gt;</span>): its key is shown once, for the project's agent.</p>
    <p class="mute">The agent then starts with <span class="mono">ssh … docs agents</span> (the guide, <span class="mono">docs/USING.md</span>, served by the box);
      the box's own settings are under <a href="#/settings">Settings</a>.</p>
  </div>
{:else}
  <table>
    <thead><tr><th>project</th><th>key</th><th>disk</th><th>Benchfile</th><th>running</th><th>waiting</th><th>last run</th></tr></thead>
    <tbody>
      {#each Object.entries(P) as [p, info]}
        {@const last = lastRun(p)}
        <tr>
          <td><a href="#/p/{p}" style:color={color(p)}>{p}</a></td>
          <td class:mute={!info.key}>{info.key ? "active" : "revoked"}</td>
          <td>{gb(info.used)} / {info.quota ? gb(info.quota) : "?"}</td>
          <td>{info.benchfile ? `v${info.benchfile}` : "none"}{Object.keys(info.benchfiles || {}).length ? ` + ${Object.keys(info.benchfiles).length} branch` : ""}</td>
          <td>{active(p, "running") || ""}</td>
          <td>{active(p, "queue") || ""}</td>
          <td>{#if last}<a href="#/r/{p}/{last.id}" class:bad={last.status !== 0}>{last.job} · {last.status === 0 ? "ok" : `exit ${last.status}`} · {ago(last.t || last.time || 0)}</a>{:else}<span class="mute">none</span>{/if}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<style>
  .head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
  .row { display: flex; gap: 8px; align-items: center; }
  .first { max-width: 720px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); }
  th { font-weight: 600; font-size: 12px; color: var(--mute); }
</style>
