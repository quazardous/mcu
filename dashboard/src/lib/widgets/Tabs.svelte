<script>
  // a page's tabs, drawn as real tabs on a strip right under the header: the
  // current one opens into the page below. tabs: [[key, label, href]]
  let { tabs, current, label = "sections" } = $props();
</script>

<div class="strip" role="tablist" aria-label={label}>
  {#each tabs as [k, text, href] (k)}
    <a role="tab" {href} aria-selected={current === k} class:on={current === k}>{text}</a>
  {/each}
</div>

<style>
  .strip { display: flex; gap: 2px; padding: 0 16px; background: var(--panel); border-bottom: 1px solid var(--line);
           overflow-x: auto; scrollbar-width: none; }
  a { white-space: nowrap; padding: 7px 18px 6px; margin-bottom: -1px; color: var(--mute); font-weight: 500;
      border: 1px solid transparent; border-bottom: 0; border-radius: 8px 8px 0 0; }
  a:hover { color: var(--fg); background: var(--free, var(--bg)); text-decoration: none; }
  a.on { color: var(--fg); font-weight: 600; background: var(--bg); border-color: var(--line);
         box-shadow: inset 0 2px 0 var(--accent, #3b6fd8); }
</style>
