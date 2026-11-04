<script>
  // pages of a list the server cuts: "1-20 of 57", newer / older; offset
  // moves by `limit`
  let { offset = 0, limit = 20, total = 0, onchange } = $props();
  const last = $derived(Math.min(offset + limit, total));
</script>

{#if total > limit || offset > 0}
  <div class="pager small">
    <button class="small" disabled={offset === 0} onclick={() => onchange?.(0)} aria-label="newest">«</button>
    <button class="small" disabled={offset === 0} onclick={() => onchange?.(Math.max(offset - limit, 0))}>newer</button>
    <span class="mute">{total ? offset + 1 : 0}–{last} of {total}</span>
    <button class="small" disabled={last >= total} onclick={() => onchange?.(offset + limit)}>older</button>
    <button class="small" disabled={last >= total} onclick={() => onchange?.(Math.floor((total - 1) / limit) * limit)} aria-label="oldest">»</button>
  </div>
{/if}

<style>
  .pager { display: flex; align-items: center; gap: 6px; justify-content: flex-end; margin-top: 8px; }
</style>
