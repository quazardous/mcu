<script>
  // results kept, and pruned
  import { live } from "../lib/live.svelte.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import { queueTask } from "../lib/admin.svelte.js";
  import EnvFields from "./EnvFields.svelte";
  let { focus = "" } = $props();
  const days = $derived(live.state?.envelope?.retention_days ?? 14);
</script>

<h2>Retention</h2>
<EnvFields groups={["Housekeeping"]} {focus} />
<div class="panel row">
  <button onclick={() => ask("Prune old results now?", `Every project's job results older than ${days} days are deleted.`, () => queueTask({ kind: "prune" }))}>prune old results now</button>
  <span class="mute">removes job results older than {days} days (also done daily)</span>
</div>
<p class="mute small">Moves, purges, reclaims, prunes and the eGPU's eject run as tasks: <a href="#/tasks">Tasks</a>.
  Freeing a project's room: its page, Storage.</p>

<style>
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .small { font-size: 12px; }
</style>
