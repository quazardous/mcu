<script>
  // the box's restart and power-off, from the main bar: a confirm, then the
  // graceful sequence (mcu-admin shutdown: queue closed, eGPU ejected,
  // running jobs waited for, a 60 s countdown), followed in the banner
  import { post } from "../api.js";
  import { live } from "../live.svelte.js";
  import { ask } from "./confirm.svelte.js";
  import { say } from "./toast.svelte.js";
  let open = $state(false);
  const busy = $derived(!!live.state?.shutdown);
  const ACTIONS = {
    reboot: { label: "Restart", title: "Restart the box?" },
    poweroff: { label: "Power off", title: "Power off the box?" },
  };
  function pick(action) {
    open = false;
    const a = ACTIONS[action];
    ask(a.title, "The queue closes (new jobs get exit 75 with the reason), the eGPU is ejected, the running jobs finish, "
      + `then a 60 s countdown before the ${action === "reboot" ? "restart" : "power-off"}. You can cancel until the countdown ends.`
      + (action === "poweroff" ? " Powering it on again needs someone at the machine." : ""),
      async () => { try { say((await post("/api/admin/shutdown", { action })).note); } catch (e) { say(e.message, true); } },
      action === "poweroff" ? "power off" : null);
  }
</script>

<div class="power">
  <button class="link" title="restart or power off the box" disabled={busy} onclick={() => (open = !open)}>⏻ ▾</button>
  {#if open}
    <div class="drop" role="menu">
      {#each Object.entries(ACTIONS) as [k, a] (k)}
        <button class="link" role="menuitem" onclick={() => pick(k)}>{a.label}</button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .power { position: relative; display: inline-flex; align-items: center; gap: 6px; }
  .power > button { font-size: 16px; color: var(--fg); }
  .drop { position: absolute; top: 30px; right: 0; background: var(--panel); border: 1px solid var(--line);
          border-radius: 6px; padding: 4px; display: flex; flex-direction: column; z-index: 20; min-width: 110px; }
  .drop button { text-align: left; padding: 5px 8px; font-weight: 600; color: var(--fg); }
  .drop button:hover { background: var(--free); }
</style>
