<script>
  // the app's toast (see toast.svelte.js), mounted once in App
  import { toast, say } from "./toast.svelte.js";
</script>

{#if toast.text}
  <div class="toast" class:bad={toast.bad} role="status">
    <span>{toast.text}</span>
    <button class="link small" onclick={() => say("")} aria-label="dismiss">✕</button>
  </div>
{/if}

<style>
  .toast { position: fixed; left: 50%; bottom: 18px; transform: translateX(-50%); z-index: 40; max-width: min(720px, calc(100% - 32px));
           display: flex; gap: 12px; align-items: center; background: var(--panel); border: 1px solid var(--ok); border-left: 4px solid var(--ok);
           border-radius: 8px; padding: 10px 14px; box-shadow: 0 6px 24px rgb(0 0 0 / 0.25); }
  .toast.bad { border-color: var(--bad); border-left-color: var(--bad); color: var(--bad); }
</style>
