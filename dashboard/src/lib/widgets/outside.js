// use:outside={close}: a menu closes on a click elsewhere, or on Escape
export function outside(node, close) {
  const click = (e) => { if (!node.contains(e.target)) close(); };
  const key = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("pointerdown", click, true);
  document.addEventListener("keydown", key);
  return {
    update(fn) { close = fn; },
    destroy() {
      document.removeEventListener("pointerdown", click, true);
      document.removeEventListener("keydown", key);
    },
  };
}
