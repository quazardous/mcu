<script>
  import { post } from "../lib/api.js";
  let { onin, notice = "" } = $props();
  let password = $state("");
  let error = $state("");
  async function submit(e) {
    e.preventDefault();
    try {
      await post("/api/login", { password });
      onin();
    } catch (err) {
      error = err.message;
    }
  }
</script>

<form class="panel login" onsubmit={submit}>
  <img src="/logo.svg" alt="" class="logo" />
  <h1>mcu</h1>
  <p class="mute small sub">multiple compute unit</p>
  {#if notice}<p class="warn">{notice}</p>{/if}
  <input type="password" bind:value={password} placeholder="admin password" autocomplete="current-password" required />
  <button>log in</button>
  {#if error}<p class="bad">{error}</p>{/if}
</form>

<style>
  .login { max-width: 320px; margin: 15vh auto; display: grid; gap: 10px; }
  .logo { width: 72px; height: 72px; display: block; margin: 0 auto 6px; }
  h1 { font-size: 18px; margin: 0; }
  .sub { margin: 0 0 10px; }
</style>
