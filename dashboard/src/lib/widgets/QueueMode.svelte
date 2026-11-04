<script>
  // the queue's mode in the header: a pill (open, paused, closed) that opens
  // a menu to change it; pausing or closing asks a reason, shown to the agents
  import { live, refreshState } from "../live.svelte.js";
  import { post } from "../api.js";
  import { ask } from "./confirm.svelte.js";
  const MODES = {
    open: "jobs are accepted and start as soon as they fit",
    paused: "jobs are accepted and queue, none starts; running ones finish",
    closed: "new jobs are refused (exit 75); queued ones wait; running ones finish",
  };
  const res = $derived(live.state?.resources || {});
  const mode = $derived(res.queue || "open");
  let menu = $state(false);
  let reason = $state("");
  let until = $state("");
  let error = $state("");
  function pick(m) {
    if (m === mode) return (menu = false);
    if (m !== "open" && !reason.trim()) return (error = "give a reason, shown to the agents");
    ask(`${m === "open" ? "Open" : m === "paused" ? "Pause" : "Close"} the queue?`, MODES[m], async () => {
      try {
        await post("/api/admin/queue", { mode: m, reason: reason.trim(), until: until.trim() });
        menu = false; reason = ""; until = ""; error = "";
        await refreshState();
      } catch (e) { error = e.message; }
    });
  }
</script>

<div class="qm">
  <button class="pill {mode}" onclick={() => (menu = !menu)} aria-expanded={menu}
          title={mode === "open" ? "the queue is open" : `the queue is ${mode}: ${res.hold || res.maintenance || ""}`}>
    queue: {mode} ▾
  </button>
  {#if menu}
    <div class="drop" role="menu">
      {#each Object.entries(MODES) as [m, about]}
        <button class="item" class:on={m === mode} onclick={() => pick(m)} role="menuitem">
          <b class="dot {m}"></b><span><b>{m}</b><span class="mute small"> · {about}</span></span>
        </button>
      {/each}
      <input bind:value={reason} placeholder="reason (to pause or close)" maxlength="200" />
      <input bind:value={until} placeholder="expected back (optional): 18:30, +30m, +2h" maxlength="20" />
      {#if error}<p class="bad small">{error}</p>{/if}
    </div>
  {/if}
</div>

<style>
  .qm { position: relative; }
  .pill { border-radius: 999px; padding: 3px 12px; font-weight: 600; font-size: 13px; border: 1px solid var(--line); }
  .pill.open { color: var(--ok); }
  .pill.paused { background: var(--warn); color: #fff; border-color: var(--warn); }
  .pill.closed { background: var(--bad); color: #fff; border-color: var(--bad); }
  .drop { position: absolute; right: 0; top: calc(100% + 6px); z-index: 30; width: 360px; background: var(--panel);
          border: 1px solid var(--line); border-radius: 8px; padding: 8px; box-shadow: 0 8px 24px rgb(0 0 0 / 0.2);
          display: grid; gap: 4px; }
  .item { display: flex; gap: 8px; align-items: flex-start; text-align: left; border: 0; background: none; padding: 6px 8px; border-radius: 6px; }
  .item:hover, .item.on { background: var(--free); }
  .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 4px; flex: none; }
  .dot.open { background: var(--ok); } .dot.paused { background: var(--warn); } .dot.closed { background: var(--bad); }
  input { width: 100%; }
  .small { font-size: 12px; }
</style>
