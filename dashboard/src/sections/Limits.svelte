<script>
  // what any project may ask for by itself, with no admin step; above it a
  // request is refused with the limit in the message
  import { live } from "../lib/live.svelte.js";
  import EnvFields from "./EnvFields.svelte";
  let { focus = "" } = $props();
</script>

<p class="mute">What any project may ask for by itself, with no admin step; above it, a request is refused with the limit in the message.
  A change is written into <span class="mono">/etc/mcu/box.toml</span> (a backup kept); reset removes its line.</p>
<h2>Per job</h2>
<EnvFields groups={["Per job"]} {focus} />
<h2>Machine</h2>
<EnvFields groups={["Machine"]} {focus} />
<h2>Disk</h2>
<p class="mute small">A project's own quota is set by its Benchfile or <span class="mono">config disk</span>; yours, sticky, on its page (Storage).</p>
<EnvFields groups={["Disk"]} {focus} />
<h2>Network and hardware</h2>
<EnvFields groups={["Network and hardware"]} {focus} />
<div class="panel">
  <table>
    <thead><tr><th>resource</th><th>held</th><th>what</th><th>now</th></tr></thead>
    <tbody>
      {#each Object.entries(live.state?.declared || {}) as [n, r] (n)}
        <tr><td class="mono">{n}</td><td>{r.share === "exclusive" ? "one job at a time" : "shared"}</td><td class="mute">{r.about || ""}</td>
          <td>{#if r.present === false}<span class="warn">absent</span>{:else}present{/if}{r.hotplug ? " · hot-plug" : ""}</td></tr>
      {:else}
        <tr><td colspan="4" class="mute">none declared</td></tr>
      {/each}
    </tbody>
  </table>
  <p class="mute small">the resources the box declares (box.toml [resources], edited by hand); <span class="mono">mcu-job box topology</span> shows the hardware behind them</p>
</div>
<p class="mute small">The queue's mode (open, paused, closed) is set from the pill in the header, with a reason and an expected end.</p>

<style>
  .small { font-size: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid var(--line); }
  th { font-weight: 600; font-size: 12px; color: var(--mute); }
</style>
