// one toast for the whole app: say(text, bad) shows the outcome of an action
// at the bottom of the screen, wherever the page is scrolled; it goes after 10 s
export const toast = $state({ text: "", bad: false });
let timer = null;

export function say(text, bad = false) {
  toast.text = String(text || "");
  toast.bad = bad;
  clearTimeout(timer);
  if (toast.text) timer = setTimeout(() => (toast.text = ""), 10000);
}
