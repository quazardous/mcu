<script>
  // the home page: one row per job thread, plus GPU and MACHINE. Time runs
  // left to right: the queue waits on the left of the "now" line, running
  // jobs grow from it, and finished runs drift away to the right. Neon on
  // black whatever the theme: colour = project, outline = result.
  import { live } from "../lib/live.svelte.js";
  import { dur, gb, get } from "../lib/api.js";
  import { fromQueue, fromRunning, fromEnded, statusText, aboutOf } from "../lib/jobs.js";
  import { go } from "../lib/router.svelte.js";

  // project colours: no green nor yellow, which belong to the GPU and MACHINE rows
  const NEON = ["#00f5ff", "#ff2bd6", "#ff7b00", "#7b61ff", "#4d9fff", "#ff8fd8", "#00ffc8", "#c792ff"];
  // the shared resources' own colours: NVIDIA green, machine yellow
  const RES = { gpu: "#76ff03", machine: "#ffe600" };
  // by rank among the box's projects (sorted by name), so up to eight projects
  // never share a colour and every viewer sees the same; a name the box does
  // not list (yet) falls back to a hash
  const hashed = (p) => NEON[[...(p || "")].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % NEON.length];
  const neon = (p) => {
    const i = Object.keys(live.state?.projects || {}).sort().indexOf(p);
    return i >= 0 ? NEON[i % NEON.length] : hashed(p);
  };
  const FAIL = "#ff2e63", UNAVAILABLE = "#ffb000", NOW = "#00f5ff";

  // zoom: the history's time span, 5 min to 24 h on a log scale
  const MIN_SPAN = 300, MAX_SPAN = 86400;
  let zoom = $state(load());
  function load() {
    try {
      const v = Number(localStorage.getItem("mcu-zoom") ?? 400);
      return v >= 0 && v <= 1000 ? v : 400;
    } catch { return 400; }
  }
  $effect(() => { try { localStorage.setItem("mcu-zoom", String(zoom)); } catch {} });
  const span = $derived(MIN_SPAN * (MAX_SPAN / MIN_SPAN) ** (zoom / 1000));

  let width = $state(900);
  let now = $state(Date.now() / 1000);
  let hover = $state(null);
  $effect(() => {
    const t = setInterval(() => (now = Date.now() / 1000), 1000);
    return () => clearInterval(t);
  });

  // two views: "jobs" (tall lanes, CPU and RAM) and "detail" (lanes at half
  // height, every machine curve: temperatures, GPU, VRAM, disk, network)
  let view = $state((() => { try { return localStorage.getItem("mcu-view") || "jobs"; } catch { return "jobs"; } })());
  $effect(() => { try { localStorage.setItem("mcu-view", view); } catch {} });
  const detail = $derived(view === "detail");
  const LABEL = 70, TOP = 26, PAD = 8;
  const ROW = $derived(detail ? 14 : 28);
  const s = $derived(live.state);
  const jobLanes = $derived(s ? Array.from({ length: s.threads }, (_, t) => t).filter((t) => !s.reserved.includes(t)) : []);
  // one lane per exclusive hardware resource the box declares (gpu...),
  // then the whole machine
  const exclusiveRes = $derived(Object.entries(s?.declared || { gpu: { share: "exclusive" } })
    .filter(([, r]) => r.share === "exclusive").map(([n]) => n));
  const lanes = $derived([...jobLanes.map((t) => ({ key: t, label: `T${t}` })),
                          ...exclusiveRes.map((n) => ({ key: n, label: n.toUpperCase(), special: true, color: RES[n] || "#9fe8ff" })),
                          { key: "machine", label: "MACHINE", special: true, color: RES.machine }]);
  // the legend: the projects with a job now, or one in the last 24 h, in their colour
  const projectNames = $derived(Object.entries(s?.projects || {}).filter(([p, v]) =>
    [...(s?.running || []), ...(s?.queue || []), ...(s?.deps || [])].some((e) => e.project === p)
    || (v.last_seen && Date.now() / 1000 - v.last_seen < 86400)).map(([p]) => p).sort());
  const runningProjects = $derived(new Set((s?.running || []).map((r) => r.project)));
  const height = $derived(TOP + lanes.length * ROW + PAD);
  const queueW = $derived(Math.max(160, Math.min(360, (width - LABEL) * 0.22)));
  const nowX = $derived(LABEL + queueW);
  const scale = $derived((width - nowX - PAD) / span);
  const resources = $derived(s?.resources || {});
  const gpuGone = $derived(resources.gpu === false);
  // the hot-plug GPU's lanes (an eGPU): greyed while it is unplugged or ejected
  const offLane = (key) => gpuGone && (s?.declared?.[key]?.hotplug ?? key === "gpu");
  const GPU_STATE = { absent: "unplugged", ejecting: "being ejected", ejected: "ejected by the admin", attaching: "being re-attached", lost: "unplugged without an eject" };
  // who holds each lane right now: its thread, the gpu or the whole machine
  const holders = $derived.by(() => {
    const out = {};
    for (const r of s?.running || []) {
      for (const t of r.threads || []) out[t] = r;
      for (const res of [...exclusiveRes, "machine"]) if ((r.exclusive || []).includes(res)) out[res] = r;
    }
    return out;
  });

  // one band per contiguous run of rows a job holds
  function bands(threads, exclusive) {
    const idx = (threads || []).map((t) => lanes.findIndex((l) => l.key === t)).filter((i) => i >= 0);
    for (const res of [...exclusiveRes, "machine"]) {
      if ((exclusive || []).includes(res)) idx.push(lanes.findIndex((l) => l.key === res));
    }
    const out = [];
    for (const i of idx.sort((a, b) => a - b)) {
      const last = out[out.length - 1];
      if (last && last.end === i - 1) last.end = i;
      else out.push({ start: i, end: i });
    }
    return out.map((g) => ({ y: TOP + g.start * ROW + 3, h: (g.end - g.start + 1) * ROW - 6 }));
  }

  // running and finished runs, right of the now line
  const timeline = $derived.by(() => {
    void live.version;
    const out = [];
    const running = new Set((s?.running || []).map((r) => r.id));
    for (const r of Object.values(live.runs)) {
      if (!r.started || !r.threads) continue;
      const end = r.ended ?? (running.has(r.id) ? now : null);
      if (end == null || now - end > span) continue;
      const x = nowX + (now - end) * scale;
      const w = Math.max(Math.min((end - r.started) * scale, width - PAD - x), 3);
      const isRunning = r.ended == null;
      const tone = isRunning || r.status === 0 ? neon(r.project) : r.status === 75 ? UNAVAILABLE : FAIL;
      out.push({ run: r, running: isRunning, x, w, tone, bands: bands(r.threads, r.exclusive) });
    }
    return out;
  });

  // the queue, in the scheduler's order: the next to start nearest "now"
  const queued = $derived.by(() => {
    if (!s) return [];
    const busy = new Set((s.running || []).flatMap((r) => r.threads));
    const free = jobLanes.filter((t) => !busy.has(t));
    const shown = s.queue.slice(0, 8);
    const slot = queueW / Math.max(shown.length, 3);
    return shown.map((q, i) => {
      const pick = (free.length >= q.cores ? free : jobLanes).slice(0, q.cores);
      const threads = q.exclusive.includes("machine") ? jobLanes : pick;
      return { q, x: nowX - (i + 1) * slot + 4, w: slot - 8, bands: bands(threads, q.exclusive) };
    });
  });

  // ticks at a round step, at most seven across the history
  const ticks = $derived.by(() => {
    const steps = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200];
    const step = steps.find((x) => span / x <= 7) ?? 43200;
    return Array.from({ length: Math.floor(span / step) }, (_, i) => ({
      x: nowX + (i + 1) * step * scale, label: `-${dur((i + 1) * step)}`,
    }));
  });

  // image builds are the box's own jobs, scheduled like the others
  const isBuild = (r) => r?.job === "image build";
  // a block's about: a third line when the block is tall enough, else after
  // the project's name; cut to the block's width (monospace, 8.5px ≈ 5.2px a character)
  const clip = (t, w) => { const n = Math.max(0, Math.floor((w - 12) / 5.2)); return t.length > n ? t.slice(0, Math.max(0, n - 1)) + "…" : t; };
  const headline = (project, job, h, w) => {
    const a = h < 34 && aboutOf(s, project, job);
    return a ? clip(`${project} · ${a}`, w) : project;
  };
  function open(run) {
    if (run.project && run.id && !isBuild(run)) go("r", run.project, run.id);
  }

  // the machine's CPU and memory under the lanes, on the same time axis
  // (rrd series, one point a minute)
  const CH = 84;
  let series = $state({ cpu: null, machine: null, net: null, gpu: null });
  const win = $derived(span <= 3600 ? "1h" : span <= 21600 ? "6h" : "1d");
  $effect(() => {
    const w = win;
    const load = () => Promise.all(["cpu", "machine", "net", "gpu", "power"].map((n) => get(`/api/series/${n}?window=${w}`).catch(() => null)))
      .then(([cpu, machine, net, gpu, power]) => (series = { cpu, machine, net, gpu, power }));
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  });
  // [time, percent] points: CPU as the mean busy share of every thread,
  // memory as used over total. The rrd minutes up to the live samples (every
  // 2 s over the event stream), which carry the curve up to "now"
  const liveFrom = $derived(live.samples.length ? live.samples[0][0] : Infinity);
  const cpuPoints = $derived([
    ...(series.cpu ? series.cpu.rows.filter(([t]) => t < liveFrom).map(([t, ...v]) => {
      const vals = v.filter((x) => x != null);
      return [t, vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null];
    }) : []),
    ...live.samples.map(([t, cpu]) => [t, cpu]),
  ]);
  const memPoints = $derived.by(() => {
    const m = series.machine;
    const out = [];
    if (m) {
      const used = m.names.indexOf("mem_used") + 1, avail = m.names.indexOf("mem_available") + 1;
      for (const r of m.rows) {
        if (r[0] < liveFrom) out.push([r[0], r[used] != null && r[avail] != null ? (100 * r[used]) / (r[used] + r[avail]) : null]);
      }
    }
    return [...out, ...live.samples.map(([t, , mem]) => [t, mem])];
  });
  const cy = (v) => CH - 8 - (Math.min(Math.max(v, 0), 100) / 100) * (CH - 22);
  // a line path and its area down to `base`, broken where a sample is missing
  function curve(points, y = cy, base = CH - 8) {
    let line = "", area = "", run = [];
    const flush = () => {
      if (run.length > 1) {
        line += "M" + run.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join("L");
        area += `M${run[0][0].toFixed(1)},${base}L` + run.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join("L")
              + `L${run[run.length - 1][0].toFixed(1)},${base}Z`;
      }
      run = [];
    };
    for (const [t, v] of points) {
      const x = nowX + (now - t) * scale;
      if (v == null || x < nowX || x > width - PAD) { flush(); continue; }
      run.push([x, y(v)]);
    }
    flush();
    return { line, area };
  }
  const cpuCurve = $derived(curve(cpuPoints));
  const memCurve = $derived(curve(memPoints));

  // the NVIDIA GPU, when the box has one: utilization and VRAM in percent
  // (the rrd keeps VRAM used; the total comes from the live samples)
  const vramTotal = $derived([...live.samples].reverse().find((x) => x[9])?.[9] ?? null);
  const gpuSeries = $derived.by(() => {
    const g = series.gpu, util = [], vram = [];
    if (g) {
      const u = g.names.indexOf("util") + 1, m = g.names.indexOf("mem_used") + 1;
      for (const r of g.rows) {
        if (r[0] >= liveFrom) continue;
        util.push([r[0], u ? r[u] : null]);
        vram.push([r[0], m && r[m] != null && vramTotal ? (100 * r[m]) / vramTotal : null]);
      }
    }
    for (const x of live.samples) {
      util.push([x[0], x[7] ?? null]);
      vram.push([x[0], x[8] != null && x[9] ? (100 * x[8]) / x[9] : null]);
    }
    return { util, vram };
  });
  const lastVram = $derived([...live.samples].reverse().find((x) => x[8] != null));
  // temperatures in °C on the same 0-100 scale (100 °C is the CPU's limit)
  const temps = $derived.by(() => {
    const cpu = [], gpu = [];
    const m = series.machine, g = series.gpu;
    if (m) {
      const i = m.names.indexOf("cpu_temp") + 1;
      for (const r of m.rows) if (r[0] < liveFrom) cpu.push([r[0], i ? r[i] : null]);
    }
    if (g) {
      const i = g.names.indexOf("temp") + 1;
      for (const r of g.rows) if (r[0] < liveFrom) gpu.push([r[0], i ? r[i] : null]);
    }
    for (const x of live.samples) {
      cpu.push([x[0], x[10] ?? null]);
      gpu.push([x[0], x[11] ?? null]);
    }
    return { cpu, gpu };
  });
  // the CPU package's watts: the rrd minutes, then the live samples (every 2 s)
  const watts = $derived.by(() => {
    const out = [], m = series.power;
    if (m) {
      const i = m.names.indexOf("package") + 1;
      for (const r of m.rows) if (r[0] < liveFrom) out.push([r[0], i ? r[i] : null]);
    }
    for (const x of live.samples) out.push([x[0], x[12] ?? null]);
    return out;
  });
  const powerLimit = $derived(live.state?.power?.limit_w);
  const hasGpu = $derived(resources.gpu === true || gpuSeries.util.some(([, v]) => v != null));
  // the memory the jobs share (the envelope's jobs_memory) and what the
  // running jobs reserve of it, in bytes, drawn on the RAM chart
  const sizeOf = (v) => { const m = String(v ?? "").toLowerCase().match(/^([\d.]+)([kmgt]?)$/); return m ? parseFloat(m[1]) * 2 ** (10 * " kmgt".indexOf(m[2] || " ")) : null; };
  const jobsPool = $derived(sizeOf(s?.envelope?.jobs_memory));
  const reserved = $derived((s?.running || []).reduce((a, r) => a + (r.memory || 0), 0));
  const g1 = (b) => (b / 2 ** 30).toFixed(1);
  const pct = (b) => (s?.machine?.mem_total ? (100 * b) / s.machine.mem_total : null);
  const charts = $derived([
    { key: "cpu", label: "CPU", color: "#00f5ff", curve: cpuCurve, pts: cpuPoints, sub: `${jobLanes.length} job threads` },
    { key: "ram", label: "RAM", color: "#ff2bd6", curve: memCurve, pts: memPoints,
      sub: s ? `${g1(s.machine.mem_total - s.machine.mem_available)}/${g1(s.machine.mem_total)} GB`
        + (jobsPool ? ` · jobs ${g1(reserved)}/${g1(jobsPool)}` : "") : "",
      marks: jobsPool ? [{ v: pct(jobsPool), label: `jobs ${gb(jobsPool)}` }, ...(reserved ? [{ v: pct(reserved), label: `reserved ${gb(reserved)}`, dim: true }] : [])] : [] },
    ...(watts.some(([, v]) => v != null) ? [{ key: "watt", label: "CPU", unit: " W", color: "#ffd166", curve: curve(watts), pts: watts,
        sub: powerLimit ? `package watts · limit ${powerLimit} W (${live.state.power.profile})` : "package watts" }] : []),
    ...(detail ? [{ key: "temp", label: "TEMP", unit: "°", color: "#ff7b00", curve: curve(temps.cpu), pts: temps.cpu,
        color2: RES.gpu, curve2: hasGpu ? curve(temps.gpu) : null, pts2: temps.gpu,
        sub: `CPU (orange) · GPU (green)${hasGpu && last(temps.gpu) != null ? " " + Math.round(last(temps.gpu)) + "°" : ""} · 100° = CPU limit` }] : []),
    ...(detail && hasGpu ? [
      { key: "gpu", label: "GPU", color: RES.gpu, curve: curve(gpuSeries.util), pts: gpuSeries.util,
        sub: resources.gpu_name || "NVIDIA GPU" },
      { key: "vram", label: "VRAM", color: "#b9ff7a", curve: curve(gpuSeries.vram), pts: gpuSeries.vram,
        sub: lastVram ? gb(lastVram[8]) + " of " + gb(lastVram[9]) : "" },
    ] : []),
  ]);

  // disk and network throughput (bytes/s), one strip each under CPU/RAM:
  // reads and receptions as areas, writes and sends as lines. Each strip
  // scales to the largest value in view, rounded up to 1, 2 or 5 x 10^n
  const IH = 100;
  function rates(name, cols, at) {
    const m = series[name], out = cols.map(() => []);
    if (m) {
      const idx = cols.map((c) => m.names.indexOf(c) + 1);
      for (const r of m.rows) if (r[0] < liveFrom) idx.forEach((i, k) => out[k].push([r[0], i ? r[i] : null]));
    }
    for (const x of live.samples) at.forEach((i, k) => out[k].push([x[0], x[i] ?? null]));
    return out;
  }
  const disk = $derived(rates("machine", ["disk_read", "disk_write"], [3, 4]));
  const net = $derived(rates("net", ["rx", "tx"], [5, 6]));
  function top(floor, ...curves) {
    let max = floor;
    for (const pts of curves) for (const [t, v] of pts) if (v != null && now - t <= span && v > max) max = v;
    // round up to 1, 2 or 5 x 10^n in the unit the label will use (kB, MB,
    // GB of 1024), so the scale reads 200 MB/s rather than 191
    const unit = max < 2 ** 20 ? 1024 : max < 2 ** 30 ? 2 ** 20 : 2 ** 30;
    const v = max / unit, p = 10 ** Math.floor(Math.log10(v));
    return [1, 2, 5, 10].map((k) => k * p).find((k) => k >= v) * unit;
  }
  const diskTop = $derived(top(2 ** 20, ...disk));
  const netTop = $derived(top(100 * 2 ** 10, ...net));
  // mirrored around zero: reads and receptions above, writes and sends below
  const MID = IH / 2;
  const up = (max) => (v) => MID - (Math.min(Math.max(v, 0), max) / max) * (MID - 8);
  const down = (max) => (v) => MID + (Math.min(Math.max(v, 0), max) / max) * (MID - 8);
  const strips = $derived([
    { key: "disk", label: "DISK", max: diskTop, a: { name: "read", pts: disk[0], color: "#4d9fff" },
      b: { name: "write", pts: disk[1], color: "#ff7b00" } },
    { key: "net", label: "NET", max: netTop, a: { name: "in", pts: net[0], color: "#00ffc8" },
      b: { name: "out", pts: net[1], color: "#c792ff" } },
  ].map((st) => ({ ...st, ca: curve(st.a.pts, up(st.max), MID), cb: curve(st.b.pts, down(st.max), MID) })));
  const rate = (b) => (b == null ? "-" : b < 1024 ? `${Math.round(b)} B/s` : b < 2 ** 20 ? `${(b / 1024).toFixed(0)} kB/s`
    : b < 2 ** 30 ? `${(b / 2 ** 20).toFixed(b < 10 * 2 ** 20 ? 1 : 0)} MB/s` : `${(b / 2 ** 30).toFixed(1)} GB/s`);
  const last = (points) => [...points].reverse().find(([, v]) => v != null)?.[1];
</script>

<div class="bar">
  <div>
    <b>{s?.host}</b>
    <span class="mute">· {jobLanes.length} job threads · {s?.running.length} running · {s?.queue.length} queued</span>
    {#if gpuGone}<span class="warn"> · GPU {GPU_STATE[resources.gpu_state] || "absent"}</span>{/if}
    <span class={live.connected ? "ok" : "warn"}> · {live.connected ? "live" : "reconnecting…"}</span>
  </div>
  <div class="views" role="radiogroup" aria-label="view">
    <button role="radio" aria-checked={!detail} class:on={!detail} onclick={() => (view = "jobs")}>jobs</button>
    <button role="radio" aria-checked={detail} class:on={detail} onclick={() => (view = "detail")}>detail</button>
  </div>
  <label class="zoom">
    <span class="mute">zoom</span>
    <input type="range" min="0" max="1000" step="1" bind:value={zoom} aria-label="history time span" />
    <span class="span">{dur(span)}</span>
  </label>
</div>

<div class="arena" bind:clientWidth={width}>
  <div class="legend" aria-label="colours">
    {#each projectNames as p}
      <span class="chip" class:on={runningProjects.has(p)} style:--c={neon(p)}>{p}</span>
    {/each}
    <span class="chip res" style:--c={RES.gpu}>gpu</span>
    <span class="chip res" style:--c={RES.machine}>machine</span>
  </div>
  <svg {width} {height} role="img" aria-label="job lanes, time running left to right">
    <defs>
      <!-- a lane whose hardware is away (the eGPU unplugged): grey hatching -->
      <pattern id="off" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="8" height="8" fill="#20243a" fill-opacity="0.55" />
        <line x1="0" y1="0" x2="0" y2="8" stroke="#3a4060" stroke-width="3" />
      </pattern>
      <filter id="glow" x="-20%" y="-50%" width="140%" height="200%">
        <feGaussianBlur stdDeviation="2.4" result="b" />
        <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
      </filter>
      <filter id="glow-strong" x="-20%" y="-50%" width="140%" height="200%">
        <feGaussianBlur stdDeviation="4.5" result="b" />
        <feMerge><feMergeNode in="b" /><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
      </filter>
      <pattern id="scan" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="6" stroke="rgb(255 255 255 / 0.10)" stroke-width="2" />
      </pattern>
      <!-- the whole machine held by one job: darker, hatched the other way -->
      <pattern id="held" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
        <rect width="8" height="8" fill="rgb(0 0 0 / 0.45)" />
        <line x1="0" y1="0" x2="0" y2="8" stroke="rgb(255 43 214 / 0.18)" stroke-width="2" />
      </pattern>
    </defs>

    {#each lanes as lane, i}
      {@const h = holders[lane.key]}
      {@const hc = h ? lane.color || neon(h.project) : null}
      <rect x={LABEL} y={TOP + i * ROW} width={Math.max(width - LABEL, 0)} height={ROW}
            class="row" class:alt={i % 2} class:special={lane.special}
            opacity={offLane(lane.key) ? 0.3 : 1} />
      {#if offLane(lane.key)}
        <rect x={LABEL} y={TOP + i * ROW} width={Math.max(width - LABEL, 0)} height={ROW} fill="url(#off)" />
        <text x={LABEL + queueW + 10} y={TOP + i * ROW + ROW / 2 + 4} class="off-note">{lane.label} · {GPU_STATE[resources.gpu_state] || "unplugged"}</text>
      {/if}
      {#if h}
        <!-- busy: the label takes the holder's colour, with a lit bar at the edge -->
        <rect x={2} y={TOP + i * ROW + 4} width={LABEL - 6} height={ROW - 8} rx="3"
              fill={hc} fill-opacity="0.12" />
        <rect x={2} y={TOP + i * ROW + 4} width={3} height={ROW - 8} rx="1.5" fill={hc} filter="url(#glow)" />
      {/if}
      <text x={LABEL - 10} y={TOP + i * ROW + ROW / 2 + (detail ? 3 : 4)} class="lane-label" class:small={detail} class:special={lane.special}
            class:dead={offLane(lane.key)} class:busy={h} class:idle={lane.color && !h}
            style:fill={offLane(lane.key) ? null : hc || lane.color || null} style:color={offLane(lane.key) ? null : hc || lane.color || null}>{lane.label}<title>{h ? `${lane.label}: ${h.project}/${h.job}` : `${lane.label}: free`}</title></text>
    {/each}

    <rect x={LABEL} y={TOP} width={queueW} height={lanes.length * ROW} fill="url(#scan)" opacity="0.35" />
    <text x={LABEL + 6} y={TOP - 9} class="zone" class:hold={resources.queue && resources.queue !== "open"}>{resources.queue && resources.queue !== "open" ? `QUEUE · ${resources.queue.toUpperCase()}` : "QUEUE"}</text>
    {#if !queued.length}
      <text x={LABEL + queueW / 2} y={TOP + (lanes.length * ROW) / 2} class="empty">empty</text>
    {/if}

    {#each ticks as t}
      {#if t.x < width - PAD}
        <line x1={t.x} x2={t.x} y1={TOP} y2={height - PAD} class="tick" />
        <text x={t.x} y={TOP - 9} class="tick-label">{t.label}</text>
      {/if}
    {/each}

    {#each timeline.filter((b) => (b.run.exclusive || []).includes("machine")) as h (h.run.id)}
      <rect x={h.x} y={TOP} width={h.w} height={lanes.length * ROW} fill="url(#held)" pointer-events="none" />
    {/each}

    {#each queued as { q, x, w, bands: bs }}
      {#each bs as b}
        <rect {x} y={b.y} width={w} height={b.h} rx="4" fill={neon(q.project)} fill-opacity="0.08"
              stroke={neon(q.project)} stroke-dasharray="5 4" stroke-width="1.5" filter="url(#glow)"
              onmouseenter={() => (hover = { q })} onmouseleave={() => (hover = null)} role="presentation" />
      {/each}
      {#if bs[0] && w > 46 && bs[0].h >= 18}
        <text x={x + 6} y={bs[0].y + 9} class="block-project" fill={neon(q.project)}>{headline(q.project, q.job, bs[0].h, w)}</text>
        <text x={x + 6} y={bs[0].y + 19} class="block-label" fill={neon(q.project)}>{q.job}</text>
        {#if bs[0].h >= 34 && aboutOf(s, q.project, q.job)}
          <text x={x + 6} y={bs[0].y + 29} class="block-about" fill={neon(q.project)}>{clip(aboutOf(s, q.project, q.job), w)}</text>
        {/if}
      {/if}
    {/each}

    {#each timeline as { run, running, x, w, tone, bands: bs } (run.id)}
      {#each bs as b}
        <rect {x} y={b.y} width={w} height={b.h} rx="3" fill={isBuild(run) ? "url(#scan)" : tone}
              fill-opacity={running ? 0.42 : 0.2} stroke={tone} stroke-width={running ? 2 : 1.4}
              stroke-dasharray={isBuild(run) ? "3 3" : null}
              filter={running ? "url(#glow-strong)" : "url(#glow)"} class:pulse={running}
              onmouseenter={() => (hover = { run })} onmouseleave={() => (hover = null)}
              onclick={() => open(run)} role="button" tabindex="-1" onkeydown={() => {}} />
      {/each}
      {#if bs[0] && w > 46 && bs[0].h >= 18}
        <text x={x + 6} y={bs[0].y + 9} class="block-project" fill={tone}>{isBuild(run) ? run.project : headline(run.project, run.job, bs[0].h, w)}</text>
        <text x={x + 6} y={bs[0].y + 19} class="block-label" fill={tone}>
          {isBuild(run) ? "⚙ image build" : run.job}{running ? " ▶" : run.status === 0 ? "" : run.status === 75 ? " ⏸" : " ✗"}</text>
        {#if bs[0].h >= 34 && !isBuild(run) && aboutOf(s, run.project, run.job)}
          <text x={x + 6} y={bs[0].y + 29} class="block-about" fill={tone}>{clip(aboutOf(s, run.project, run.job), w)}</text>
        {/if}
      {/if}
    {/each}

    <line x1={nowX} x2={nowX} y1={TOP - 4} y2={height - PAD + 2} stroke={NOW} stroke-width="2" filter="url(#glow-strong)" />
    <text x={nowX} y={TOP - 9} class="now-label">NOW</text>
  </svg>

  {#each charts as c (c.key)}
    <svg {width} height={CH} role="img" aria-label="machine {c.label} over the same time span" class="machine">
      {#each [0, 50, 100] as p}
        <line x1={nowX} x2={width - PAD} y1={cy(p)} y2={cy(p)} class="tick" />
        <text x={nowX - 6} y={cy(p) + 4} class="lane-label">{p}{c.unit || "%"}</text>
      {/each}
      {#each ticks as t}
        {#if t.x < width - PAD}<line x1={t.x} x2={t.x} y1={cy(100)} y2={cy(0)} class="tick" />{/if}
      {/each}
      <path d={c.curve.area} fill={c.color} fill-opacity="0.12" />
      <path d={c.curve.line} fill="none" stroke={c.color} stroke-width="1.6" filter="url(#glow)" />
      {#if c.curve2}<path d={c.curve2.line} fill="none" stroke={c.color2} stroke-width="1.6" filter="url(#glow)" />{/if}
      {#each c.marks || [] as mk}
        {#if mk.v != null}
          <line x1={nowX} x2={width - PAD} y1={cy(mk.v)} y2={cy(mk.v)} stroke={c.color} stroke-width="1" stroke-dasharray="5 4" opacity={mk.dim ? 0.45 : 0.85} />
          <text x={width - PAD - 4} y={cy(mk.v) - 3} text-anchor="end" class="lane-label" fill={c.color}>{mk.label}</text>
        {/if}
      {/each}
      <text x={LABEL + 8} y={34} class="hud" fill={c.color}>{c.label} {last(c.pts) != null ? Math.round(last(c.pts)) + (c.unit || "%") : "-"}</text>
      <text x={LABEL + 8} y={54} class="hud-sub">{c.sub}</text>
      <line x1={nowX} x2={nowX} y1={cy(100) - 4} y2={cy(0) + 2} stroke={NOW} stroke-width="2" filter="url(#glow-strong)" />
    </svg>
  {/each}

  {#each detail ? strips : [] as st (st.key)}
    <svg {width} height={IH} role="img" aria-label="{st.key} throughput over the same time span, {st.a.name} above zero, {st.b.name} below" class="machine">
      {#each [MID - (MID - 8), MID + (MID - 8)] as y}
        <line x1={nowX} x2={width - PAD} y1={y} y2={y} class="tick" />
      {/each}
      <line x1={nowX} x2={width - PAD} y1={MID} y2={MID} class="zero" />
      <text x={nowX - 6} y={12} class="lane-label">{rate(st.max)}</text>
      <text x={nowX - 6} y={MID + 4} class="lane-label">0</text>
      <text x={nowX - 6} y={IH - 4} class="lane-label">{rate(st.max)}</text>
      {#each ticks as t}
        {#if t.x < width - PAD}<line x1={t.x} x2={t.x} y1={8} y2={IH - 8} class="tick" />{/if}
      {/each}
      <path d={st.ca.area} fill={st.a.color} fill-opacity="0.18" />
      <path d={st.ca.line} fill="none" stroke={st.a.color} stroke-width="1.5" filter="url(#glow)" />
      <path d={st.cb.area} fill={st.b.color} fill-opacity="0.18" />
      <path d={st.cb.line} fill="none" stroke={st.b.color} stroke-width="1.5" filter="url(#glow)" />
      <text x={LABEL + 8} y={28} class="hud small" fill="#ff2bd6">{st.label}</text>
      <text x={LABEL + 8} y={50} class="hud-sub" style:fill={st.a.color}>▲ {st.a.name} {rate(last(st.a.pts))}</text>
      <text x={LABEL + 8} y={68} class="hud-sub" style:fill={st.b.color}>▼ {st.b.name} {rate(last(st.b.pts))}</text>
      <line x1={nowX} x2={nowX} y1={4} y2={IH - 4} stroke={NOW} stroke-width="2" filter="url(#glow-strong)" />
    </svg>
  {/each}

  {#if hover}
    {@const j = hover.q ? fromQueue(hover.q)
      : hover.run.ended ? fromEnded(hover.run)
      : fromRunning((s?.running || []).find((x) => x.id === hover.run.id) || hover.run, hover.run)}
    <div class="tip">
      <b style:color={neon(j.project)}>{j.project}</b>/{j.job} <span class="dim">{j.params}</span><br />
      {#if aboutOf(s, j.project, j.job)}<span class="dim">{aboutOf(s, j.project, j.job)}</span><br />{/if}
      {j.threads ?? "?"} threads{#if j.memory} · {gb(j.memory)} reserved{/if}{#if j.used != null} · {gb(j.used)} used{/if}{#if j.peak} · peak {gb(j.peak)}{/if}{#if j.exclusive.length} · {j.exclusive.join(", ")}{/if}<br />
      {#if j.state === "queued"}<span class="amber">{j.note}</span>
      {:else if j.state === "running"}running for {dur(now - j.started)} on threads {(j.lanes || []).join(",")}
      {:else}
        {statusText(j)} · {dur(j.seconds)}{#if j.note && j.note !== j.state}<span class="amber"> · {j.note}</span>{/if}
        {#if j.cost}<br />{j.cost.cpu_seconds} s CPU ({j.cost.cpu_use} % of {j.cost.threads}){/if}
      {/if}
    </div>
  {/if}
</div>

<style>
  .bar { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
  .legend { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px 14px; padding: 4px 12px 2px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; font: 600 12px ui-monospace, monospace; color: var(--c); opacity: 0.7; }
  .chip::before { content: ""; width: 10px; height: 10px; border-radius: 3px; background: var(--c); box-shadow: 0 0 6px var(--c); }
  .chip.on { opacity: 1; text-shadow: 0 0 6px var(--c); }
  .chip.res::before { border-radius: 50%; }
  .views { display: flex; margin-left: auto; }
  .views button { border-radius: 0; min-width: 64px; }
  .views button:first-child { border-radius: 6px 0 0 6px; }
  .views button:last-child { border-radius: 0 6px 6px 0; }
  .views button.on { background: #00f5ff; color: #05060d; font-weight: 700; }
  .lane-label.small { font-size: 9px; }
  .zoom { display: flex; align-items: center; gap: 8px; }
  .zoom input { width: 220px; padding: 0; accent-color: #00f5ff; border: 0; background: none; }
  .zoom .span { font: 600 13px ui-monospace, monospace; min-width: 5em; }
  .arena { position: relative; background: #05060d; border: 1px solid #1b2140; border-radius: 10px;
           padding: 6px 0; overflow: hidden; box-shadow: inset 0 0 50px rgb(0 245 255 / 0.06); }
  .row { fill: #080a14; }
  .row.alt { fill: #0b0e1b; }
  .row.special { fill: #10091a; }
  .lane-label { font: 600 11px ui-monospace, monospace; fill: #5f6d96; text-anchor: end; }
  .lane-label.special { fill: #ff2bd6; filter: drop-shadow(0 0 3px #ff2bd6); }
  .lane-label.busy { filter: drop-shadow(0 0 4px currentColor); }
  .lane-label.idle { opacity: 0.45; filter: none; }
  .lane-label.dead { fill: #4a5068; opacity: 1; filter: none; text-decoration: line-through; }
  .off-note { font: 600 11px ui-monospace, monospace; fill: #6b7390; letter-spacing: 0.05em; }
  .zone { font: 700 10px ui-monospace, monospace; fill: #5f6d96; letter-spacing: .15em; }
  .zone.hold { fill: #ffb000; filter: drop-shadow(0 0 4px #ffb000); }
  .empty { font: 11px ui-monospace, monospace; fill: #2c3456; text-anchor: middle; }
  .tick { stroke: #1b2140; stroke-dasharray: 2 5; }
  .zero { stroke: #2c3456; }
  .tick-label { font: 10px ui-monospace, monospace; fill: #4b5880; text-anchor: middle; }
  .now-label { font: 700 10px ui-monospace, monospace; fill: #00f5ff; text-anchor: middle; letter-spacing: .15em;
               filter: drop-shadow(0 0 4px #00f5ff); }
  .block-label { font: 600 10.5px ui-monospace, monospace; pointer-events: none; }
  .block-about { font: 8.5px ui-monospace, monospace; opacity: .85; pointer-events: none; }
  .block-project { font: 8.5px ui-monospace, monospace; letter-spacing: .04em; opacity: .75; pointer-events: none; }
  .machine { display: block; border-top: 1px solid #1b2140; }
  .hud { font: 700 18px ui-monospace, monospace; letter-spacing: .05em; filter: drop-shadow(0 0 5px currentColor); }
  .hud-sub { font: 11px ui-monospace, monospace; fill: #5f6d96; }
  .hud.small { font-size: 13px; }
  rect[role="button"] { cursor: pointer; }
  .pulse { animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 50% { fill-opacity: 0.6; } }
  @media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }
  .tip { position: absolute; right: 12px; bottom: 12px; max-width: 440px; background: #0b0e1b; color: #d8e6ff;
         border: 1px solid #2a3566; border-radius: 8px; padding: 8px 10px; font: 12px/1.5 ui-monospace, monospace;
         box-shadow: 0 0 18px rgb(0 245 255 / 0.15); pointer-events: none; }
  .tip .dim { color: #6c7aa0; }
  .tip .amber { color: #ffb000; }
</style>
