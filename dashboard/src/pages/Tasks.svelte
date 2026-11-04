<script>
  // the admin's long operations (moves, purges, reclaims, prunes, the eGPU's
  // eject and re-attach, a restart): queued, running, ended, with their logs
  import TaskList from "../lib/TaskList.svelte";
</script>

<h1>Tasks</h1>
<TaskList />

<style>h1 { font-size: 20px; }</style>
