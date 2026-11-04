<script>
  // the envelope's limits of some groups, each with its control: a change is
  // written into box.toml (its line, comments kept), reset removes the line
  // and the default is back. `focus`: the key to scroll to and mark
  import { live } from "../lib/live.svelte.js";
  import { envelopeFields, shownValue, setEnvelope, resetEnvelope } from "../lib/admin.svelte.js";
  import Setting from "../lib/widgets/Setting.svelte";
  let { groups, focus = "" } = $props();
  const E = $derived(live.state?.envelope || {});
  const ED = $derived(live.state?.envelope_defaults || {});
  const ES = $derived(new Set(live.state?.envelope_set || []));
  const fields = $derived(envelopeFields(live.state).filter((f) => groups.includes(f.group)));
  $effect(() => {
    if (focus) document.getElementById(`limit-${focus}`)?.scrollIntoView({ block: "center" });
  });
</script>

<div class="panel">
  {#each fields as f (f.key)}
    <div id="limit-{f.key}" class:focus={focus === f.key}>
      <Setting label={f.key} about={f.label}
               kind={f.kind === "select" ? "select" : f.kind === "multi" ? "checks" : "range"}
               value={shownValue(f, E[f.key])} min={f.min} max={f.max} options={f.options || []}
               unit={f.kind === "gb" ? " GB" : f.kind === "hours" ? " h" : ""}
               overridden={ES.has(f.key)} resetLabel={`reset to the default, ${JSON.stringify(ED[f.key])}`}
               onset={(v) => setEnvelope(f, v)} onreset={() => resetEnvelope(f)} />
    </div>
  {/each}
  {#if fields.some((f) => ES.has(f.key))}
    <p class="mute small">📌 = set in box.toml; the others have their default.</p>
  {/if}
</div>

<style>
  .focus { box-shadow: inset 3px 0 0 var(--accent, #3b6fd8); padding-left: 8px; }
  .small { font-size: 12px; }
</style>
