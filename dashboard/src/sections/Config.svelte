<script>
  // the effective configuration: every setting in force, where it comes
  // from (default, computed from the machine, box.toml and its line), the
  // default when the file changes it, and the one place that changes it
  import { get } from "../lib/api.js";
  import { live } from "../lib/live.svelte.js";
  import { editHref } from "../lib/admin.svelte.js";
  let cfg = $state(null);
  let error = $state("");
  $effect(() => {
    void live.version;
    get("/api/config").then((c) => (cfg = c)).catch((e) => (error = e.message));
  });
  const groups = $derived(cfg ? [...new Set(cfg.settings.map((r) => r.group))] : []);
  // a table of the file ([resources], [power], [media]) apart from the envelope's key of the same name
  const TABLES = ["resources", "power", "media"];
  const name = (k) => (TABLES.includes(k) ? `[${k}]` : k.replace(/^envelope\./, ""));
  const show = (v) => (v == null ? "—" : typeof v === "string" ? v : Array.isArray(v) ? v.join(", ") || "none" : JSON.stringify(v));
</script>

{#if error}<p class="bad">{error}</p>
{:else if !cfg}<p class="mute">reading the configuration…</p>
{:else}
  <p class="mute">Every setting in force. The machine's are edited by hand in <span class="mono">{cfg.file}</span>;
    the limits here, or with <span class="mono">mcu-admin envelope set</span>, which rewrite their line in the same file.
    <span class="mono">mcu-admin config show --origin</span> prints the same.</p>
  {#each groups as g}
    <h2>{g}</h2>
    <div class="panel">
      <table>
        <thead><tr><th>setting</th><th>value</th><th>from</th><th>takes effect</th><th></th></tr></thead>
        <tbody>
          {#each cfg.settings.filter((r) => r.group === g) as r (r.key)}
            <tr>
              <td class="mono">{name(r.key)}</td>
              <td class="mono">{show(r.value)}</td>
              <td><span class="src {r.source}">{r.line ? `box.toml:${r.line}` : r.source}</span>
                {#if r.default != null}<span class="mute small"> (default {show(r.default)})</span>{/if}</td>
              <td class="mute small">{r.effect}</td>
              <td>{#if r.writer === "admin"}<a class="small" href={editHref(r.key.replace(/^envelope\./, ""))}>change</a>
                  {:else}<span class="mute small" title="edited by hand in box.toml">file</span>{/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/each}
{/if}

<style>
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  th:nth-child(1) { width: 22%; } th:nth-child(2) { width: 22%; } th:nth-child(3) { width: 26%; } th:nth-child(5) { width: 10%; }
  td { overflow-wrap: anywhere; }
  th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid var(--line); vertical-align: baseline; }
  th { font-weight: 600; font-size: 12px; color: var(--mute); }
  .small { font-size: 12px; }
  .src { font: 12px ui-monospace, monospace; padding: 0 6px; border-radius: 4px; border: 1px solid var(--line); }
  .src.box\.toml { color: var(--warn); border-color: var(--warn); }
  .src.computed { color: var(--accent, #3b6fd8); border-color: var(--accent, #3b6fd8); }
  .src.unset, .src.default { color: var(--mute); }
  @media (max-width: 720px) {
    th:nth-child(4), td:nth-child(4) { display: none; }
    th:nth-child(1) { width: 34%; } th:nth-child(2) { width: 24%; } th:nth-child(3) { width: 28%; } th:nth-child(5) { width: 14%; }
  }
</style>
