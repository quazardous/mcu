<script>
  // invite a project: its name, what that creates, then its key (shown once,
  // on the project's Access tab). An existing name is refused here: a new
  // key for it is "new key" on that tab, which asks first
  import { live } from "../live.svelte.js";
  import { invite, keys } from "../admin.svelte.js";
  import { inviting } from "./invite.svelte.js";
  const NAME = /^[a-z][a-z0-9-]{1,20}$/;
  let name = $state("");
  const taken = $derived(!!live.state?.projects?.[name]);
  const valid = $derived(NAME.test(name) && !taken);
  function close() {
    inviting.open = false;
    name = "";
  }
  async function go() {
    const p = name;
    close();
    await invite(p);
    if (keys.invited?.project === p) location.hash = `#/p/${p}/access`;
  }
</script>

<svelte:window onkeydown={(e) => inviting.open && e.key === "Escape" && close()} />

{#if inviting.open}
  <div class="veil" role="presentation" onclick={(e) => e.target === e.currentTarget && close()}>
    <form class="panel ask" role="dialog" aria-modal="true" aria-labelledby="invite-title"
          onsubmit={(e) => { e.preventDefault(); if (valid) go(); }}>
      <h2 id="invite-title">Invite a project</h2>
      <p>Creates the project's user on the box, its storage and its SSH key. The key is shown
        <b>once</b>, on the project's Access tab, for its agent.</p>
      <!-- svelte-ignore a11y_autofocus -->
      <input bind:value={name} autofocus placeholder="project name" aria-label="project name" spellcheck="false" />
      <p class="mute small">
        {#if taken}<span class="bad">{name} already exists</span>: a new key for it is <a href="#/p/{name}/access" onclick={close}>“new key” on its Access tab</a>.
        {:else}lowercase letters, digits and -, starting with a letter, 2 to 21 characters{/if}
      </p>
      <div class="row">
        <button type="submit" class="primary" disabled={!valid}>Invite</button>
        <button type="button" onclick={close}>cancel</button>
      </div>
    </form>
  </div>
{/if}

<style>
  .veil { position: fixed; inset: 0; background: rgb(0 0 0 / 0.45); display: grid; place-items: center; z-index: 50; padding: 16px; }
  .ask { max-width: 460px; width: 100%; box-shadow: 0 10px 40px rgb(0 0 0 / 0.35); }
  .ask h2 { margin-top: 0; }
  .ask input { display: block; width: 100%; box-sizing: border-box; margin: 4px 0 4px; }
  .small { font-size: 12px; margin: 0 0 14px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
</style>
