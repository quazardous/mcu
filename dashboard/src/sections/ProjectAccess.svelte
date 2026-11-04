<script>
  // one project's key: active or revoked, a new one (shown once), revoke
  import { live } from "../lib/live.svelte.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import { postNote, invite, keys } from "../lib/admin.svelte.js";
  import KeyPanel from "./KeyPanel.svelte";
  let { project } = $props();
  const v = $derived(live.state?.projects?.[project] || {});
</script>

{#if keys.invited?.project === project}<KeyPanel />{/if}
<div class="panel">
  <p>Key: {#if v.key}<span class="ok">active</span>{:else}<span class="warn">revoked</span>{/if}
    <span class="mute small">· the project reaches the box as <span class="mono">mcu-{project}@{live.state?.host}</span> with it</span></p>
  <div class="btns">
    <button onclick={() => ask(`New key for ${project}?`, "Its current key stops working at once; its agent needs the new one before its next job.", () => invite(project))}>new key</button>
    <button class="danger" disabled={!v.key} onclick={() => ask(`Revoke ${project}'s key?`, "Its agent can no longer reach the box. Its data stays; a new key brings it back.",
      () => postNote("/api/admin/revoke", { project }), project)}>revoke</button>
  </div>
</div>

<style>.small { font-size: 12px; }</style>
