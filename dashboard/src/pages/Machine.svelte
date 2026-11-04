<script>
  // the machine, in tabs kept in the URL (#/machine/<tab>): its overview
  // (hardware and state, like a system information page, the optional
  // hardware, the eGPU's eject and re-attach), its stats over time (rrdtool
  // series: load and memory, each thread's busy share, the GPU, each
  // project's disk against its quota), its disks and pools, its power and
  // the timing jobs' profile
  import { get, post, color, gb, dur, ago } from "../lib/api.js";
  import { live, refreshState } from "../lib/live.svelte.js";
  import { ask } from "../lib/widgets/confirm.svelte.js";
  import Chart from "../lib/Chart.svelte";
  import DisksPools from "../sections/DisksPools.svelte";
  import PowerTiming from "../sections/PowerTiming.svelte";
  let { tab = "overview" } = $props();
  const WINDOWS = ["1h", "6h", "1d", "1w", "1m", "1y"];
  let win = $state("1d");
  let data = $state({});
  // the optional hardware (box/hw): what is absent is not drawn; not
  // measured yet (no hardware.json), nothing is hidden
  let hardware = $state([]);
  get("/api/hardware").then((h) => (hardware = h.features || [])).catch(() => {});
  const has = (key) => !hardware.length || hardware.some((f) => f.key === key && f.state === "present");
  const dbs = $derived(tab !== "stats" ? [] : ["machine", "cpu", ...(has("nvidia") ? ["gpu"] : []), ...(has("rapl") ? ["power"] : []),
    ...(has("smart") ? ["wear"] : []), ...Object.keys(live.state?.projects || {}).map((p) => `disk-${p}`)]);
  $effect(() => {
    for (const db of dbs) get(`/api/series/${db}?window=${win}`).then((d) => { data[db] = d; }).catch(() => { data[db] = null; });
  });
  const col = (d, n) => d.rows.map((r) => r[1 + d.names.indexOf(n)]);
  const xs = (d) => d.rows.map((r) => r[0]);
  // the overview: read now, again every 5 s while open, and at once when the
  // GPU's state or a task changes
  let info = $state(null);
  let tasks = $state([]);
  async function loadInfo() {
    // the tasks first: a task that just ended has already written the state the info then reads
    try { tasks = (await get("/api/admin/tasks?kind=gpu&limit=5")).active.filter((t) => t.kind === "gpu"); } catch {}
    try { info = await get("/api/info"); } catch {}
  }
  $effect(() => {
    if (tab !== "overview") return;
    void live.tasks; void live.state?.resources?.gpu_state;
    loadInfo();
    const timer = setInterval(loadInfo, 5000);
    return () => clearInterval(timer);
  });
  // the eGPU's buttons follow one state, never a mix of half-updated
  // sources: the box's gpu_state (pushed live; the back end's state machine
  // shows ejecting and attaching for as long as the operation runs), the
  // admin task while it is queued or waiting (its cancel), and the click
  // itself until one of those has moved. When the task ends, the buttons
  // wait for the state to be re-read rather than show the old one
  const gpuTask = $derived(tasks[0]);
  const gpuState = $derived(live.state?.resources?.gpu_state);
  let pending = $state(null);     // { action, from }: clicked, nothing has moved yet
  let settling = $state(false);   // the task just ended: re-reading the state
  let error = $state("");
  let hadTask = false;
  $effect(() => {
    if (pending && (gpuTask || gpuState !== pending.from)) pending = null;
    if (hadTask && !gpuTask) {
      settling = true;
      refreshState().catch(() => {}).finally(() => { settling = false; loadInfo(); });
    }
    hadTask = !!gpuTask;
  });
  const gpuBusy = $derived(!!pending || !!gpuTask || settling || ["ejecting", "attaching"].includes(gpuState));
  // a queued gpu task starts 10 s after the click (mcu-task): count it down
  let now = $state(Date.now() / 1000);
  $effect(() => {
    if (!gpuTask?.not_before || gpuTask.state !== "queued") return;
    const timer = setInterval(() => (now = Date.now() / 1000), 250);
    return () => clearInterval(timer);
  });
  const startsIn = $derived(gpuTask?.state === "queued" && gpuTask.not_before ? Math.max(0, Math.ceil(gpuTask.not_before - now)) : null);
  const gpuLabel = $derived(startsIn !== null ? `${gpuTask.args[0] === "attach" ? "re-attach" : gpuTask.args[0]} in ${startsIn} s…`
    : gpuTask ? `${gpuTask.args[0]} ${gpuTask.state}${gpuTask.progress ? `: ${gpuTask.progress}` : ""}`
    : gpuState === "ejecting" ? "ejecting…" : gpuState === "attaching" ? "re-attaching…"
    : pending ? `${pending.action === "attach" ? "re-attach" : pending.action}…` : "…");
  async function gpuAct(body) {
    error = "";
    if (body.kind === "gpu") pending = { action: body.action, from: gpuState };
    try { await post("/api/admin/task", body); } catch (e) { error = e.message; pending = null; }
    loadInfo();
  }
  const palette = ["#3b6fd8", "#d8613b", "#2e9a6a", "#9a4fd1", "#c79a1e", "#cf3f7a", "#2a9bb0", "#7a8a1f", "#8b5a2b", "#5a6b8b", "#b03a64", "#3a8b6b"];
</script>

<div class="bar">
  <h1>Machine <span class="mute small">{live.state?.host?.split(".")[0]}</span></h1>
  {#if tab === "stats"}
    <div>{#each WINDOWS as w}<button class:on={w === win} onclick={() => (win = w)}>{w}</button>{/each}</div>
  {:else}<span></span>{/if}
</div>

{#if tab === "overview"}
  {@render infoTab()}
{:else if tab === "disks"}
  <DisksPools />
{:else if tab === "power"}
  <PowerTiming />
{:else}
{#if data.machine}
  {@const m = data.machine}
  <h2>Load and memory</h2>
  <div class="panel"><Chart data={[xs(m), col(m, "load"), col(m, "mem_used").map((v) => v && v / 2 ** 30)]}
    series={[{ label: "load", stroke: "#3b6fd8", width: 2, scale: "load" }, { label: "memory used GB", stroke: "#d8613b", width: 2, scale: "gb" }]}
    scales={{ load: {}, gb: {} }} /></div>
  <h2>Temperature and frequency</h2>
  <div class="panel"><Chart data={[xs(m), col(m, "cpu_temp"), col(m, "cpu_mhz")]}
    series={[{ label: "CPU °C", stroke: "#cf3f7a", width: 2, scale: "c" }, { label: "MHz", stroke: "#2e9a6a", width: 2, scale: "mhz" }]}
    scales={{ c: {}, mhz: {} }} /></div>
{/if}
{#if data.cpu}
  {@const c = data.cpu}
  <h2>Each thread's busy share (%)</h2>
  <div class="panel"><Chart data={[xs(c), ...c.names.map((n) => col(c, n))]} scales={{ y: { range: [0, 100] } }}
    series={c.names.map((n, i) => ({ label: n.replace("t", "T"), stroke: palette[i % palette.length], width: 1 }))} /></div>
{/if}
{#if data.gpu}
  {@const g = data.gpu}
  <h2>GPU</h2>
  <div class="panel"><Chart data={[xs(g), col(g, "util"), col(g, "temp"), col(g, "power")]}
    series={[{ label: "use %", stroke: "#2e9a6a", width: 2 }, { label: "°C", stroke: "#cf3f7a", width: 2 }, { label: "W", stroke: "#c79a1e", width: 2 }]} /></div>
{/if}
{#if data.wear}
  {@const w = data.wear}
  <h2>Disk wear</h2>
  <div class="panel"><Chart data={[xs(w), col(w, "ssd_wear"), col(w, "ssd_written"), col(w, "hdd_realloc")]}
    series={[{ label: "SSD wear %", stroke: "#cf3f7a", width: 2, scale: "pct" }, { label: "SSD TB written", stroke: "#3b6fd8", width: 2, scale: "tb" },
      { label: "HDD reallocated sectors", stroke: "#c79a1e", width: 2, scale: "n" }]} scales={{ pct: {}, tb: {}, n: {} }} /></div>
{/if}
{#if data.power}
  {@const w = data.power}
  <h2>Power (W)</h2>
  {@const lim = live.state?.power?.limit_w}
  <div class="panel"><Chart data={[xs(w), col(w, "total"), col(w, "platform"), col(w, "package"), col(w, "gpu"), ...(lim ? [xs(w).map(() => lim)] : [])]}
    series={[{ label: "total", stroke: "#e0702a", width: 2, fill: "#e0702a18" }, { label: "unit (platform)", stroke: "#3b6fd8", width: 2 },
      { label: "CPU", stroke: "#cf3f7a", width: 1 }, { label: "GPU (card)", stroke: "#2e9a6a", width: 2 },
      ...(lim ? [{ label: `CPU limit (${live.state.power.profile})`, stroke: "#cf3f7a", width: 1, dash: [6, 4] }] : [])]} />
    <p class="mute small">unit: the platform as RAPL measures it (CPU, and memory and chipset where the CPU reports them),
      without the power supply's losses; GPU: the card's own sensor (nvidia-smi), not at the wall (an enclosure's
      power supply and fan come on top); total: the two</p></div>
{/if}
<h2>Disk per project (GB)</h2>
{#each dbs.filter((d) => d.startsWith("disk-")) as db}
  {#if data[db]}
    {@const d = data[db]}
    {@const p = db.slice(5)}
    <div class="panel"><b style:color={color(p)}>{p}</b>
      <Chart height={140} data={[xs(d), col(d, "used").map((v) => v && v / 2 ** 30), col(d, "quota").map((v) => v && v / 2 ** 30)]}
        series={[{ label: "used", stroke: color(p), width: 2, fill: color(p) + "22" }, { label: "quota", stroke: "#888", dash: [4, 4] }]} /></div>
  {/if}
{/each}
{/if}

{#snippet dot(st)}<span class="dot {st || 'ok'}" title={st}></span>{/snippet}

{#snippet infoTab()}
  {#if !info}<p class="mute">reading the hardware…</p>
  {:else}
    {@const sy = info.system}
    {@const c = info.cpu}
    {@const m = info.memory}
    <h2>System</h2>
    <div class="panel grid">
      <span class="k">product</span><span>{sy.product} <span class="mute">(board {sy.board})</span></span>
      <span class="k">BIOS</span><span>{sy.bios}</span>
      <span class="k">system</span><span>{sy.os}, kernel {sy.kernel}</span>
      <span class="k">up</span><span>{dur(sy.uptime)}</span>
    </div>
    {#if hardware.length}
      <h2>Optional hardware</h2>
      <div class="panel grid">
        {#each hardware as f}
          <span class="k">{f.feature}</span>
          <span><span class="pill {f.state === 'present' ? 'ok' : f.state === 'absent' ? 'off' : 'warn'}">{f.state}</span>
            {f.detail}{#if f.fix}<span class="mute"> · fix: <span class="mono">{f.fix}</span></span>{/if}</span>
        {/each}
      </div>
      <p class="mute small">The box works without any of these, minus that feature; absent ones are not drawn (<span class="mono">mcu-admin doctor</span> says the same).</p>
    {/if}
    <h2>Processor and memory</h2>
    <div class="panel grid">
      <span class="k">{@render dot(c.status)} CPU</span><span>{c.model}, {c.threads} threads {#if c.note}<span class="warn">· {c.note}</span>{/if}</span>
      <span class="k">now</span><span>{c.temp ?? "?"} °C · {c.mhz} MHz · {c.throttle_events} throttle events since boot</span>
      {#if c.power?.platform != null || c.power?.package != null}<span class="k">power</span><span>{#if c.power.platform != null}unit {c.power.platform.toFixed(1)} W · {/if}CPU {c.power.package?.toFixed(1) ?? "?"} W{#if c.power.limit} (limit {c.power.limit.limit_w} W, {c.power.limit.profile}){/if}{#if c.power.total != null} · with the GPU {c.power.total.toFixed(1)} W{/if}
        <span class="mute">(the last minute's mean)</span></span>{/if}
      <span class="k">policy</span><span>{c.driver}, governor {c.governor}, turbo {c.no_turbo === "1" ? "off" : "on"}, cap {c.max_perf_pct} %
        <span class="mute">(the timing jobs' cap applies only while one runs)</span></span>
      <span class="k">memory</span><span>{gb(m.total)} · {gb(m.available)} available · swap {gb(m.swap_total - m.swap_free)} of {gb(m.swap_total)} used</span>
      {#if m.jobs}<span class="k">for jobs</span><span>{gb(m.jobs)} shared by all jobs, the rest for the system · <a href="#/settings/limits/jobs_memory">change</a></span>{/if}
    </div>
    <h2>Graphics</h2>
    {#each info.graphics as g (g.address || g.name)}
      <div class="panel dev">
        <div class="head">{@render dot(g.status)} <b>{g.name}</b>
          {#if g.egpu}<span class="pill {g.status}">{g.state}</span>{/if}</div>
        <div class="grid">
          {#if g.driver}<span class="k">driver</span><span>{g.driver}{g.driver_version ? ` ${g.driver_version}` : ""}{g.link ? ` · PCIe ${g.link}` : ""}{g.address ? ` · ${g.address}` : ""}</span>{/if}
          {#if g.temp != null}<span class="k">now</span><span>{g.temp} °C · {g.power ?? "?"} W · {g.pstate} · VRAM {gb(g.vram_used)} of {gb(g.vram_total)}</span>{/if}
          {#if g.egpu}<span class="k">state</span><span class={g.status === "ok" ? "" : "warn"}>{g.note}{g.since ? ` · since ${ago(g.since)}` : ""}</span>{/if}
        </div>
        {#if g.egpu}
          <div class="btns actions">
            {#if gpuBusy}
              <span class="warn small">{gpuLabel}</span>
              {#if gpuTask && (gpuTask.state === "queued" || gpuTask.state === "waiting")}<button class="small" onclick={() => gpuAct({ kind: "cancel", id: gpuTask.id })}>cancel</button>{/if}
            {:else if gpuState === "present"}
              <button onclick={() => ask("Eject the eGPU?",
                "New GPU jobs get exit 75 from now on; running GPU jobs finish first, then the card leaves the bus and its enclosure can be unplugged. Cancellable while it waits.",
                () => gpuAct({ kind: "gpu", action: "eject" }))}>eject</button>
            {:else if gpuState !== "lost" && g.enclosure}
              <button onclick={() => ask("Re-attach the eGPU?", "The enclosure must be plugged in and on: the bus is rescanned and GPU jobs are accepted again.",
                () => gpuAct({ kind: "gpu", action: "attach" }))}>re-attach</button>
            {/if}
            {#if error}<span class="bad small">{error}</span>{/if}
          </div>
        {/if}
      </div>
    {/each}
    {#if info.thunderbolt.length}
      <h2>Thunderbolt</h2>
      <div class="panel grid">
        {#each info.thunderbolt as t (t.id)}
          <span class="k">{@render dot(t.status)} {t.host ? "host" : "device"}</span>
          <span>{t.name} <span class="mute">({t.id})</span>{t.speed ? ` · ${t.speed}` : ""}{t.note ? ` · ${t.note}` : ""}</span>
        {/each}
      </div>
    {/if}
    <h2>Disks</h2>
    <div class="panel grid">
      {#each info.disks as d (d.name)}
        <span class="k">{@render dot(d.status)} {d.name}</span><span>{d.model} · {gb(d.size)}{d.media ? ` · ${d.media.toUpperCase()}` : ""}
          {#if d.wear_pct != null} · wear {d.wear_pct} %{/if}{#if d.written} · {(d.written / 1e12).toFixed(2)} TB written{/if}
          {#if d.reallocated != null} · {d.reallocated} reallocated sectors{/if}{#if d.hours != null} · {d.hours} h on{/if}{#if d.temp != null} · {d.temp} °C{/if}
          <span class="mute mono small">{d.mounts.join("  ")}</span></span>
      {/each}
    </div>
    <h2>Network</h2>
    <div class="panel grid">
      {#each info.network as n (n.name)}
        <span class="k">{@render dot(n.status)} {n.name}</span><span>{n.state}{n.speed && n.speed !== "-1" ? ` · ${n.speed} Mb/s` : ""}{n.addresses.length ? ` · ${n.addresses.join(", ")}` : ""}</span>
      {/each}
    </div>
    <h2>Sensors</h2>
    <div class="panel sensors">
      {#each info.sensors as t, i (i)}
        <span class:warn={t.c >= 90}><span class="mute">{t.sensor} {t.label}</span> {t.c.toFixed(0)} °C</span>
      {/each}
    </div>
  {/if}
{/snippet}

<style>
  .bar { display: flex; justify-content: space-between; align-items: center; }
  .bar button { margin-left: 4px; padding: 3px 9px; }
  .bar button.on { background: var(--accent); color: #fff; border-color: var(--accent); }
  h1 { font-size: 20px; }
  .grid { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; align-items: baseline; }
  .k { color: var(--mute); white-space: nowrap; }
  .dev .head { margin-bottom: 6px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .actions { margin-top: 10px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--ok); }
  .dot.warn { background: var(--warn); }
  .dot.off { background: var(--mute); }
  .pill { font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); }
  .pill.ok { color: var(--ok); border-color: var(--ok); }
  .pill.warn { color: var(--warn); border-color: var(--warn); }
  .pill.off { color: var(--mute); }
  .sensors { display: flex; flex-wrap: wrap; gap: 6px 18px; }
  .panel + .panel { margin-top: 10px; }
</style>
