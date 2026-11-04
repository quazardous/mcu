<script>
  // a project's new API key, shown once: copy, download, and how to start
  import { keys } from "../lib/admin.svelte.js";
  const k = $derived(keys.invited);
  let copied = $state(false);
  // the clipboard API works on the dashboard's https; the textarea is selected as a fallback
  async function copyKey(e) {
    try {
      await navigator.clipboard.writeText(k.key);
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch {
      e.currentTarget.closest(".key")?.querySelector("textarea")?.select();
    }
  }
  const download = $derived(k ? URL.createObjectURL(new Blob([k.key], { type: "application/octet-stream" })) : null);
</script>

{#if k}
  <div class="panel key">
    <p><b>{k.project}</b> has a new key. It is shown <b>once</b> and is not kept on the box: give it to the project's agent.</p>
    <textarea readonly rows="8" aria-label="the private key">{k.key}</textarea>
    <p><button onclick={copyKey}>{copied ? "copied ✓" : "copy the key"}</button>
       · <a href={download} download="mcu-{k.project}.key">download the key</a>
       · <button onclick={() => (keys.invited = null)}>done</button></p>
    <pre class="mono"># anywhere outside a repository, mode 600 (~/.ssh is not required)
install -m 600 mcu-{k.project}.key .secrets/mcu.key
ssh -i .secrets/mcu.key -o IdentitiesOnly=yes mcu-{k.project}@{k.host} docs agents   # the project's starting point
ssh -i .secrets/mcu.key -o IdentitiesOnly=yes mcu-{k.project}@{k.host} help          # every command</pre>
  </div>
{/if}

<style>
  .key { border-color: var(--accent); margin-bottom: 14px; }
  textarea { width: 100%; font: 12px ui-monospace, monospace; background: var(--free); color: var(--fg);
             border: 1px solid var(--line); border-radius: 6px; }
  pre { overflow-x: auto; }
</style>
