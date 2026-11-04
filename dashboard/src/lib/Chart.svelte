<script>
  // a uPlot chart; `data` is uPlot's [xs, ...series] and `series` its options
  import uPlot from "uplot";
  let { data, series, height = 220, scales = {}, axes = null, onpoint = null } = $props();
  let el = $state();
  let width = $state(600);
  let plot;

  // the canvas does not understand CSS variables: read the theme's colours
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  function latest(u) {
    const idxs = u.data.map((col) => {
      for (let i = (col?.length ?? 0) - 1; i >= 0; i--) if (col[i] != null) return i;
      return null;
    });
    u.setLegend({ idxs }, false);
  }

  $effect(() => {
    const mute = css("--mute"), line = css("--line");
    const opts = {
      width, height, series: [{}, ...series], scales,
      axes: axes || [{ stroke: mute, grid: { stroke: line }, ticks: { stroke: line } },
                     { stroke: mute, grid: { stroke: line }, ticks: { stroke: line } }],
      cursor: { drag: { x: true, y: false } },
      // off the chart, the legend shows each series' latest value, not "--"
      hooks: { setLegend: [(u) => { if (u.legend.idx == null) latest(u); }], ready: [latest] },
    };
    plot?.destroy();
    plot = new uPlot(opts, data, el);
    if (onpoint) {
      el.onclick = () => {
        const i = plot.cursor.idx;
        if (i != null) onpoint(i);
      };
    }
    return () => plot?.destroy();
  });
</script>

<div bind:clientWidth={width}><div bind:this={el}></div></div>
