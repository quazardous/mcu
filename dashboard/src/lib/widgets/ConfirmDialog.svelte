<script>
  // the app's confirmation dialog (see confirm.svelte.js): in the page, never
  // a browser dialog; Escape or a click outside cancels
  import { asking } from "./confirm.svelte.js";
  const a = $derived(asking.current);
  function confirm() {
    const go = asking.current.go;
    asking.current = null;
    go();
  }
</script>

<svelte:window onkeydown={(e) => e.key === "Escape" && (asking.current = null)} />

{#if a}
  <div class="veil" role="presentation" onclick={(e) => e.target === e.currentTarget && (asking.current = null)}>
    <div class="panel ask" role="alertdialog" aria-modal="true" aria-labelledby="ask-title">
      <h2 id="ask-title">{a.title}</h2>
      {#if a.detail?.includes("\n")}<pre class="diff">{a.detail}</pre>{:else}<p>{a.detail}</p>{/if}
      {#if a.word}
        <p class="mute">Type <b class="mono">{a.word}</b> to confirm.</p>
        <!-- svelte-ignore a11y_autofocus -->
        <input bind:value={asking.current.typed} autofocus aria-label="type {a.word} to confirm" />
      {/if}
      <div class="row">
        <button class="danger" disabled={a.word && a.typed.trim() !== a.word} onclick={confirm}>confirm</button>
        <button onclick={() => (asking.current = null)}>cancel</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .diff { max-height: 50vh; overflow: auto; font: 12px/1.4 ui-monospace, monospace; background: var(--free); padding: 8px; border-radius: 6px; white-space: pre; }
  .veil { position: fixed; inset: 0; background: rgb(0 0 0 / 0.45); display: grid; place-items: center; z-index: 50; padding: 16px; }
  .ask { max-width: 460px; width: 100%; border-color: var(--bad); box-shadow: 0 10px 40px rgb(0 0 0 / 0.35); }
  .ask h2 { margin-top: 0; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
</style>
