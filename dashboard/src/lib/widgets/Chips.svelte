<script>
  // a single choice among a few, as a row of pills (a filter): options are
  // [value, label] pairs, "" usually meaning "all"
  let { label = "", options = [], value = "", onchange } = $props();
</script>

<span class="chips" role="radiogroup" aria-label={label}>
  {#if label}<span class="mute small">{label}</span>{/if}
  {#each options as [v, text] (v)}
    <button class="chip small" role="radio" aria-checked={value === v} class:on={value === v}
            onclick={() => value !== v && onchange?.(v)}>{text}</button>
  {/each}
</span>

<style>
  .chips { display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap; }
  .chips .mute { margin-right: 2px; }
  .chip { border-radius: 999px; padding: 1px 10px; }
  .chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
</style>
