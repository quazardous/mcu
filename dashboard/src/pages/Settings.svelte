<script>
  // the box's settings: the effective configuration (everything in force and
  // where it comes from), the limits any project works within, housekeeping
  import Config from "../sections/Config.svelte";
  import Limits from "../sections/Limits.svelte";
  import Housekeeping from "../sections/Housekeeping.svelte";
  let { tab = "config", focus = "" } = $props();
</script>

<h1>Settings</h1>
{#if tab === "limits"}
  <Limits {focus} />
{:else if tab === "housekeeping"}
  <Housekeeping {focus} />
{:else}
  <Config />
{/if}

<style>h1 { font-size: 20px; }</style>
