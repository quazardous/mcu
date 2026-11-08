<script>
  // the shell: the login gate, the header (home, projects menu, machine,
  // the tasks' count, the queue's mode, power, settings) with each page's
  // tabs under it, and the page of the current route
  import { session, post, color, whenExpired } from "./lib/api.js";
  import { live, start, stopLive } from "./lib/live.svelte.js";
  import { route } from "./lib/router.svelte.js";
  import { tasks, loadTasks } from "./lib/admin.svelte.js";
  import Login from "./pages/Login.svelte";
  import ConfirmDialog from "./lib/widgets/ConfirmDialog.svelte";
  import InviteDialog from "./lib/widgets/InviteDialog.svelte";
  import { outside } from "./lib/widgets/outside.js";
  import { openInvite } from "./lib/widgets/invite.svelte.js";
  import Toast from "./lib/widgets/Toast.svelte";
  import QueueMode from "./lib/widgets/QueueMode.svelte";
  import PowerMenu from "./lib/widgets/PowerMenu.svelte";
  import Tabs from "./lib/widgets/Tabs.svelte";
  import { cancelShutdown, shutdownNow } from "./lib/widgets/shutdown.js";
  import Home from "./pages/Home.svelte";
  import Jobs from "./pages/Jobs.svelte";
  import Project from "./pages/Project.svelte";
  import Projects from "./pages/Projects.svelte";
  import Job from "./pages/Job.svelte";
  import Run from "./pages/Run.svelte";
  import Machine from "./pages/Machine.svelte";
  import Settings from "./pages/Settings.svelte";
  import Tasks from "./pages/Tasks.svelte";

  let loggedIn = $state(null);
  let menu = $state(false);
  let ended = $state("");

  // the session ended under an open page: back to the login form, and to
  // the same page (the route is the URL's hash, kept) once logged in again
  whenExpired(() => {
    if (loggedIn === false) return;
    stopLive();
    ended = "Your session ended (expired, or the dashboard restarted without it). Log in again.";
    loggedIn = false;
  });

  // the restart/power-off countdown ticks here; cancel undoes the sequence
  let clock = $state(Date.now() / 1000);
  $effect(() => { const t = setInterval(() => (clock = Date.now() / 1000), 1000); return () => clearInterval(t); });

  async function boot() {
    loggedIn = await session().catch(() => false);
    if (loggedIn) {
      ended = "";
      await start();
    }
  }
  boot();

  async function logout() {
    await post("/api/logout");
    location.reload();
  }
  const page = $derived(route.parts[0] || "");
  const projects = $derived(Object.keys(live.state?.projects || {}));
  $effect(() => { document.title = live.state?.host ? `mcu · ${live.state.host.split(".")[0]}` : "mcu"; });
  // each page's tab, from the URL
  const MACHINE_TABS = [["overview", "Overview"], ["stats", "Stats"], ["disks", "Disks and pools"], ["power", "Power and timing"]];
  const SETTINGS_TABS = [["config", "Effective configuration"], ["limits", "Limits"], ["housekeeping", "Housekeeping"]];
  const PROJECT_TABS = [["runs", "Runs"], ["storage", "Storage"], ["settings", "Settings"], ["access", "Access"]];
  const pick = (list, t) => (list.some(([k]) => k === t) ? t : list[0][0]);
  const machineTab = $derived(pick(MACHINE_TABS, route.parts[1]));
  const settingsTab = $derived(pick(SETTINGS_TABS, route.parts[1]));
  const projectPage = $derived(page === "p" && route.parts[1] && route.parts[2] !== "jobs");
  const projectTab = $derived(pick(PROJECT_TABS, route.parts[2]));
  // the admin tasks not ended: a count in the header, read again on each task event
  $effect(() => { if (live.ready) { void live.tasks; loadTasks(); } });
</script>

{#if loggedIn === false}
  <Login onin={boot} notice={ended} />
{:else if loggedIn && live.ready}
  <div class="top">
  <header class:joined={["machine", "settings", "", "home", "jobs"].includes(page) || projectPage}>
    <a href="#/" class="brand" title="mcu: multiple compute unit"><img src="/logo.svg" alt="" class="logo" />mcu{#if live.state?.host} <span class="host">· {live.state.host.split(".")[0]}</span>{/if}</a>
    <div class="menu" use:outside={() => (menu = false)}>
      <button onclick={() => (menu = !menu)}>Projects ▾</button>
      {#if menu}
        <div class="drop" role="menu">
          <a href="#/p" onclick={() => (menu = false)}>all projects</a>
          {#each projects as p}
            <a href="#/p/{p}" onclick={() => (menu = false)} style:color={color(p)}>{p}</a>
          {/each}
          <button class="link invite" role="menuitem" onclick={() => { menu = false; openInvite(); }}>+ invite a project…</button>
        </div>
      {/if}
    </div>
    <a href="#/machine" class:on={page === "machine"}>Machine</a>
    <a href="#/tasks" class:on={page === "tasks"} title="the admin's long operations">Tasks{#if tasks.active.length}
      <span class="badge" aria-label="{tasks.active.length} not ended">{tasks.active.length}</span>{/if}</a>
    <span class="grow"></span>
    <QueueMode />
    <PowerMenu />
    <a href="#/settings" class="gear" class:on={page === "settings"} title="settings" aria-label="settings">⚙</a>
    <button class="link" onclick={logout}>log out</button>
  </header>
  {#if page === "" || page === "home" || page === "jobs"}
    <Tabs label="jobs" current={page === "jobs" ? "list" : "graph"}
      tabs={[["graph", "Graph", "#/"], ["list", "List", "#/jobs"]]} />
  {:else if page === "machine"}
    <Tabs label="machine" current={machineTab} tabs={MACHINE_TABS.map(([k, t]) => [k, t, `#/machine/${k}`])} />
  {:else if page === "settings"}
    <Tabs label="settings" current={settingsTab} tabs={SETTINGS_TABS.map(([k, t]) => [k, t, `#/settings/${k}`])} />
  {:else if projectPage}
    <Tabs label="project" current={projectTab} tabs={PROJECT_TABS.map(([k, t]) => [k, t, `#/p/${route.parts[1]}/${k}`])} />
  {/if}
  </div>
  {@const res = live.state.resources || {}}
  {#if live.state.shutdown}
    {@const sd = live.state.shutdown}
    {@const word = sd.action === "reboot" ? "Restart" : "Power-off"}
    <div class="banner maint"><b>{word} requested:</b>
      {#if sd.step === "closing"}closing the queue…
      {:else if sd.step === "ejecting"}ejecting the eGPU (its running jobs first)…
      {:else if sd.step === "waiting"}waiting for {sd.running?.length || "the"} running job(s){#if sd.running?.length} ({sd.running.join(", ")}){/if}.
      {:else if sd.step === "countdown"}<b>{sd.action === "reboot" ? "restarting" : "powering off"} in {Math.max(0, Math.ceil(sd.deadline - clock))} s</b>.
      {:else}{sd.action === "reboot" ? "restarting" : "powering off"} now.{/if}
      {#if sd.step === "countdown"}
        <button class="link cancel" onclick={() => shutdownNow(sd.action)}>{sd.action === "reboot" ? "restart now" : "power off now"}</button>
      {/if}
      {#if sd.step !== "reboot" && sd.step !== "poweroff"}
        <button class="link cancel" onclick={cancelShutdown}>cancel</button>
      {/if}
    </div>
  {/if}
  {#if res.queue === "paused"}
    <div class="banner maint"><b>Queue paused:</b> {res.hold}{#if res.until} (expected back {new Date(res.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}){/if}. Jobs are accepted but none starts; running ones finish.{#if res.queue_by}<span class="by"> (by {res.queue_by})</span>{/if}</div>
  {/if}
  {#if res.status && res.status !== "ok"}
    <div class="banner" class:maint={res.status === "maintenance"}>
      {#if res.maintenance}<b>Queue closed:</b> {res.maintenance}{#if res.until} (expected back {new Date(res.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}){/if}. New jobs are refused (exit 75), queued ones wait, running ones finish.{#if res.queue_by}<span class="by"> (by {res.queue_by})</span>{/if}
      {:else}<b>Degraded:</b> {res.problems.join(", ")}.
        {#if res.gpu === false}GPU jobs are refused (exit 75); other jobs run normally.{/if}
        {#if res.gpu_note}<b>{res.gpu_note}.</b>{/if}
      {/if}
    </div>
  {/if}
  <main class="page" class:wide={page === "" || page === "home" || page === "jobs"}>
    {#if page === "p" && route.parts[2] === "jobs" && route.parts[3]}
      <Job project={route.parts[1]} job={route.parts[3]} />
    {:else if page === "p" && route.parts[1]}
      {#key route.parts[1]}<Project project={route.parts[1]} tab={projectTab} />{/key}
    {:else if page === "p"}
      <Projects />
    {:else if page === "r"}
      <Run project={route.parts[1]} id={route.parts[2]} />
    {:else if page === "machine"}
      <Machine tab={machineTab} />
    {:else if page === "settings"}
      <Settings tab={settingsTab} focus={route.parts[2] || ""} />
    {:else if page === "tasks"}
      <Tasks />
    {:else if page === "jobs"}
      <Jobs />
    {:else}
      <Home />
    {/if}
  </main>
  {#if live.state?.version}<footer class="version">mcu {live.state.version}</footer>{/if}
{:else}
  <p class="page mute">loading…</p>
{/if}

<ConfirmDialog />
<InviteDialog />
<Toast />

<style>
  .version { text-align: center; color: var(--mute); font-size: 11px; padding: 12px 0 18px; }
  .banner { background: var(--warn); color: #fff; padding: 8px 16px; font-size: 14px; }
  .banner.maint { background: var(--bad); }
  .banner .by { opacity: .8; }
  .banner .cancel { color: #fff; text-decoration: underline; font-weight: 700; margin-left: 8px; }
  .top { position: sticky; top: 0; z-index: 5; }
  header { display: flex; align-items: center; gap: 16px; padding: 10px 16px; background: var(--panel);
           border-bottom: 1px solid var(--line); }
  header.joined { border-bottom: 0; padding-bottom: 6px; }
  .brand { font-weight: 700; color: var(--fg); display: inline-flex; align-items: center; gap: 8px; }
  .brand .logo { width: 26px; height: 26px; }
  .brand .host { font-weight: 400; color: var(--mute); }
  .grow { flex: 1; }
  .on { font-weight: 600; }
  .gear { font-size: 18px; color: var(--fg); }
  .badge { display: inline-block; min-width: 18px; padding: 0 5px; margin-left: 2px; border-radius: 9px; background: var(--warn);
           color: #fff; font-size: 11px; text-align: center; }
  @media (max-width: 720px) { header { flex-wrap: wrap; gap: 10px; } }
  .menu { position: relative; }
  .drop { position: absolute; top: 34px; left: 0; background: var(--panel); border: 1px solid var(--line);
          border-radius: 8px; padding: 6px; display: grid; min-width: 160px; box-shadow: 0 6px 18px rgb(0 0 0 / .15); }
  .drop a { padding: 5px 8px; font-weight: 600; }
  .drop .invite { text-align: left; padding: 7px 8px 5px; margin-top: 4px; border-top: 1px solid var(--line); border-radius: 0;
                 color: var(--fg); font-weight: 600; white-space: nowrap; }
  button.link { border: none; background: none; color: var(--mute); }
</style>
