<script>
  // one editable setting: a control holding a draft, "set" once the draft
  // differs, "cancel", and a "back to …" reset when the value is an override.
  // A change applies at once and offers "undo" for a few seconds; only a
  // caller passing `confirm` (a destructive change) asks first through the
  // app's dialog. kind: range | select | checks.
  // A range snaps to bigStep while Ctrl is held (dragging, or Ctrl+arrows);
  // bigStep defaults to 10 for a wide range, 5 for a narrower one
  import { ask } from "./confirm.svelte.js";
  let {
    label, about = "", kind = "range", value, min = 0, max = 100, step = 1, unit = "",
    options = [], format = null, confirm = null, overridden = false, resetLabel = "reset",
    bare = false,   // inside a table cell: no line nor padding of its own
    bigStep = null,
    typed = true,   // a range's value is a number field (false: a label, for a range over choices)
    onset, onreset = null,
  } = $props();
  let draft = $state(null);
  // after "set", the new value stays shown until the box's state says it
  // holds it (or 5 s pass): no flicker back to the old value meanwhile
  let pending = $state(null);
  let pendingTimer = null;
  $effect(() => {
    if (pending != null && JSON.stringify(value) === JSON.stringify(pending)) pending = null;
  });
  // a range input keeps where it was dragged when the value it shows does
  // not change (cancel, or set back to the same): put it back by hand
  let rangeEl = $state(null);
  $effect(() => {
    if (rangeEl && draft == null) rangeEl.value = String(pending ?? value);
  });
  const big = $derived(bigStep ?? (max - min >= 40 ? 10 : max - min >= 10 ? 5 : step));
  const snap = (v) => Math.min(max, Math.max(min, min + Math.round((v - min) / big) * big));
  // Ctrl held anywhere: the range snaps (a pointer drag carries no key state)
  let ctrl = $state(false);
  $effect(() => {
    if (kind !== "range") return;
    const k = (e) => (ctrl = e.ctrlKey);
    const off = () => (ctrl = false);
    window.addEventListener("keydown", k); window.addEventListener("keyup", k); window.addEventListener("blur", off);
    return () => { window.removeEventListener("keydown", k); window.removeEventListener("keyup", k); window.removeEventListener("blur", off); };
  });
  function bigKey(e) {
    if (!e.ctrlKey || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "PageUp", "PageDown"].includes(e.key)) return;
    e.preventDefault();
    const dir = ["ArrowRight", "ArrowUp", "PageUp"].includes(e.key) ? 1 : -1;
    const cur = Number(draft ?? pending ?? value);
    const at = snap(cur);
    draft = at !== cur && Math.sign(at - cur) === dir ? at : snap(cur + dir * big);
  }
  const shown = $derived(draft ?? pending ?? value);
  const changed = $derived(draft != null && JSON.stringify(draft) !== JSON.stringify(value));
  const show = (v) => (format ? format(v) : kind === "checks" ? (v || []).join(", ") || "none" : `${v}${unit}`);
  // a range's value label sized for its longest value (in ch): its width
  // never changes while dragging, so the slider never moves under the pointer
  const widest = $derived.by(() => {
    if (kind !== "range" || (max - min) / step > 500) return 6;
    let w = 0;
    for (let v = min; v <= max; v += step) w = Math.max(w, String(show(v)).length);
    return Math.max(w, 6);
  });
  // the value before the last change, offered back for a few seconds
  let undo = $state(null);
  let undoTimer = null;
  function offerUndo(prev) {
    undo = { prev };
    clearTimeout(undoTimer);
    undoTimer = setTimeout(() => (undo = null), 8000);
  }
  async function apply(v) {
    const prev = value;
    pending = v;
    draft = null;
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => (pending = null), 5000);
    await onset(v);
    offerUndo(prev);
  }
  function set() {
    const v = draft;
    if (confirm) ask(confirm.title ?? `Set ${label}?`, confirm.detail ?? `${show(value)} → ${show(v)}.`, () => apply(v), confirm.word ?? null);
    else apply(v);
  }
  async function reset() {
    const prev = value;
    await onreset();
    draft = null;
    offerUndo(prev);
  }
  function revert() {
    const p = undo.prev;
    undo = null;
    apply(p).then(() => (undo = null));
  }
  // a typed value: clamped to the range and its step, a draft like a drag
  let clamped = $state("");
  function typedValue(e) {
    const raw = Number(e.currentTarget.value);
    if (e.currentTarget.value.trim() === "" || Number.isNaN(raw)) {
      e.currentTarget.value = String(shown);
      return;
    }
    if (raw === Number(draft ?? value)) return;   // Enter, then the change event: already taken
    const v = Math.min(max, Math.max(min, min + Math.round((raw - min) / step) * step));
    clamped = v !== raw ? (raw > max ? `max ${max}` : raw < min ? `min ${min}` : `steps of ${step}`) : "";
    e.currentTarget.value = String(v);
    draft = v === value ? null : v;
  }
  const note = $derived(format && kind === "range" && String(format(shown)) !== `${shown}${unit}` ? format(shown) : "");
  function toggle(o) {
    const cur = [...(shown || [])];
    draft = cur.includes(o) ? cur.filter((x) => x !== o) : [...cur, o];
  }
</script>

<div class="setting" class:bare>
  <span class="lbl"><b>{label}</b>{#if about}<span class="mute small"> · {about}</span>{/if}</span>
  <span class="ctl">
    {#if kind === "range"}
      <input type="range" bind:this={rangeEl} {min} {max} {step} value={shown} aria-label={label}
             title="hold Ctrl for steps of {big}{unit}"
             onpointerdown={(e) => (ctrl = e.ctrlKey)} onpointermove={(e) => (ctrl = e.ctrlKey)} onkeydown={bigKey}
             oninput={(e) => { clamped = ""; const v = Number(e.currentTarget.value); draft = ctrl ? snap(v) : v; if (ctrl) e.currentTarget.value = String(draft); }} />
      {#if typed}
        <span class="typed">
          <input type="number" class="mono num" {min} {max} {step} value={shown} aria-label="{label} value"
                 style:width="{String(max).length + 4.5}ch"
                 onchange={typedValue}
                 onkeydown={(e) => { if (e.key === "Enter") typedValue(e); if (e.key === "Escape") { draft = null; clamped = ""; e.currentTarget.value = String(value); } }} />
          {#if unit.trim()}<span class="mute">{unit.trim()}</span>{/if}
          {#if clamped}<span class="warn small">{clamped}</span>{:else if note}<span class="mute small">{note}</span>{/if}
        </span>
      {:else}
        <span class="mono val" style:min-width="{widest + 1}ch">{show(shown)}</span>
      {/if}
    {:else if kind === "select"}
      <select value={shown} aria-label={label} onchange={(e) => (draft = e.currentTarget.value)}>
        {#each options as o}<option value={o}>{o}</option>{/each}
      </select>
    {:else}
      {#each options as o}
        <label class="small"><input type="checkbox" checked={(shown || []).includes(o)} onchange={() => toggle(o)} /> {o}</label>
      {/each}
    {/if}
    <!-- always there, only shown once changed: dragging never moves the layout -->
    <span class="act" class:on={changed} aria-hidden={!changed} style:min-width={kind === "range" ? `calc(${widest + 4}ch + 70px)` : null}>
      <button class="small" onclick={set} tabindex={changed ? 0 : -1}>set {changed ? show(draft) : ""}</button>
      <button class="link small" onclick={() => (draft = null)} tabindex={changed ? 0 : -1}>cancel</button>
    </span>
    {#if undo}<button class="link small" onclick={revert}>undo ({show(undo.prev)})</button>{/if}
    {#if overridden && onreset}
      <span class="pin small" title="an override">📌</span>
      <button class="link small" onclick={reset}>{resetLabel}</button>
    {/if}
  </span>
</div>

<style>
  .setting { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; padding: 6px 0; border-bottom: 1px solid var(--line); }
  .setting.bare { border-bottom: 0; padding: 2px 0 0; }
  .lbl { flex: 1; min-width: 260px; }
  .bare .lbl { flex: none; min-width: 0; }
  .ctl { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  input[type=range] { width: 200px; padding: 0; border: 0; background: none; }
  .val { white-space: nowrap; }
  .typed { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
  .num { padding: 2px 4px; text-align: right; }
  .act { display: inline-flex; gap: 6px; min-width: 150px; white-space: nowrap; visibility: hidden; }
  .act.on { visibility: visible; }
  .pin { color: var(--warn); }
  .small { font-size: 12px; }
</style>
