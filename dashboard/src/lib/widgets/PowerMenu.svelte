<script>
  // the box's restart and power-off, from the main bar: a confirm, then the
  // graceful sequence (mcu-admin shutdown: queue closed, eGPU ejected,
  // running jobs waited for, a 60 s countdown), followed in the banner
  import { post } from "../api.js";
  import { live } from "../live.svelte.js";
  import { ask } from "./confirm.svelte.js";
  import { say } from "./toast.svelte.js";
  import { cancelShutdown, shutdownNow } from "./shutdown.js";
  import { outside } from "./outside.js";
  let open = $state(false);
  // a restart or power-off in progress: the menu offers what applies to it
  const sd = $derived(live.state?.shutdown);
  const stopping = $derived(sd && (sd.step === "reboot" || sd.step === "poweroff"));
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

<div class="power" use:outside={() => (open = false)}>
  <button class="link" title="restart or power off the box" disabled={stopping} onclick={() => (open = !open)}>⏻ ▾</button>
  {#if open && !stopping}
    <div class="drop" role="menu">
      {#if sd}
        {#if sd.step === "countdown"}
          <button class="link" role="menuitem" onclick={() => { open = false; shutdownNow(sd.action); }}>
            {sd.action === "reboot" ? "Restart now" : "Power off now"}</button>
        {:else}
          <span class="note">now: once the running jobs end</span>
        {/if}
        <button class="link" role="menuitem" onclick={() => { open = false; cancelShutdown(); }}>Cancel</button>
      {:else}
        {#each Object.entries(ACTIONS) as [k, a] (k)}
          <button class="link" role="menuitem" onclick={() => pick(k)}>{a.label}</button>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  .power { position: relative; display: inline-flex; align-items: center; gap: 6px; }
  .power > button { font-size: 16px; color: var(--fg); }
  .drop { position: absolute; top: 30px; right: 0; background: var(--panel); border: 1px solid var(--line);
          border-radius: 6px; padding: 4px; display: flex; flex-direction: column; z-index: 20; min-width: 110px; }
  .drop button { text-align: left; padding: 5px 8px; font-weight: 600; color: var(--fg); white-space: nowrap; }
  .drop button:hover { background: var(--free); }
  .note { padding: 5px 8px; color: var(--mute); font-size: 12px; white-space: nowrap; }
</style>
