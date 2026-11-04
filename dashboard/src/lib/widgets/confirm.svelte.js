// one confirmation dialog for the whole app: ask(title, detail, go, word)
// shows it; `word` (optional) must be typed back to enable "confirm"
export const asking = $state({ current: null });

export function ask(title, detail, go, word = null) {
  asking.current = { title, detail, go, word, typed: "" };
}
