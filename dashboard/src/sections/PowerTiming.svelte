<script>
  // the CPU's power profile, and the profile timing jobs get (timing = true)
  import { live } from "../lib/live.svelte.js";
  import { postNote } from "../lib/admin.svelte.js";
  import Setting from "../lib/widgets/Setting.svelte";
  const PROFILES = ["quiet", "balanced", "fast"];
  const PROFILE_ABOUT = {
    fast: "the firmware's own limit: shortest runs, the CPU at its thermal ceiling, the fan loud",
    balanced: "about 25 % slower than fast on a long full-load build, far fewer thermal throttles",
    quiet: "about 45 % slower than fast, no throttling, the coolest and quietest",
  };
  const PW = $derived(live.state?.power);
  const T = $derived(live.state?.timing || {});
  const targetNow = $derived(T.target && T.target !== "auto" ? Math.round(T.target) : Math.round((T.idle_temp ?? 50) + 10));
  const setTiming = (body) => postNote("/api/admin/timing", body);
</script>

<h2>CPU power</h2>
{#if PW}
<div class="panel">
  <Setting label="CPU power" about="the CPU's long-term power limit (RAPL PL1); a timing job always runs at fast"
           kind="range" value={Math.max(0, PROFILES.indexOf(PW.profile))} min={0} max={2} step={1} bigStep={1}
           format={(i) => `${PROFILES[i]} · ${PW.profiles[PROFILES[i]]} W`}
           onset={(i) => postNote("/api/admin/power", { profile: PROFILES[i] })} />
  <p class="mute small">{PROFILE_ABOUT[PW.profile] ?? ""} · limit now {PW.limit_w} W{PW.limit_w !== PW.profiles[PW.profile] ? " (a timing job holds the machine)" : ""}</p>
</div>
{:else}
  <p class="mute">This machine exposes no CPU power limit (RAPL): the profile cannot be set.</p>
{/if}

<h2>Timing jobs</h2>
<div class="panel">
  <p class="mute">Jobs with <span class="mono">timing = true</span> hold the whole machine and get this profile; other jobs never do.</p>
  <div class="row">
    <label class="row"><input type="checkbox" checked={!!T.cooldown} onchange={(e) => setTiming({ cooldown: e.currentTarget.checked })} />
      cool down before each timing job</label>
    <span class="mute">target:
      {T.target && T.target !== "auto" ? `${T.target} °C (manual)` : `auto, idle + 10 °C = ${T.idle_temp != null ? Math.round(T.idle_temp + 10) + " °C" : "not measured yet"}`}</span>
  </div>
  <Setting label="cool-down target" about="auto: idle + 10 °C (the idle temperature is measured while nothing runs)" kind="range" value={targetNow} min={35} max={95} unit=" °C"
           overridden={!!(T.target && T.target !== "auto")} resetLabel="back to auto"
           onset={(v) => setTiming({ target: v })} onreset={() => setTiming({ target: "auto" })} />
  <Setting label="frequency cap" about="applied only while a timing job runs" kind="range" value={T.cap_pct || 100} min={30} max={100} step={5}
           format={(v) => (v === 100 ? "none" : `${v} %`)} overridden={!!T.cap_pct} resetLabel="no cap"
           onset={(v) => setTiming({ cap_pct: v === 100 ? null : v })} onreset={() => setTiming({ cap_pct: null })} />
</div>

<style>
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .small { font-size: 12px; }
</style>
