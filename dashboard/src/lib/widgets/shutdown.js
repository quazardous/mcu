// a restart or power-off in progress: cancel it, or once its running jobs
// have ended (the countdown), run it now; shared by the banner and the menu
import { post } from "../api.js";
import { ask } from "./confirm.svelte.js";
import { say } from "./toast.svelte.js";

export async function cancelShutdown() {
  try { say((await post("/api/admin/shutdown", { action: "cancel" })).note); } catch (e) { say(e.message, true); }
}

export function shutdownNow(action) {
  const word = action === "reboot" ? "restart" : "power-off";
  ask(action === "reboot" ? "Restart the box now?" : "Power off the box now?",
    `The running jobs have ended; the ${word} starts at once instead of at the end of the countdown. It cannot be cancelled after this.`
    + (action === "poweroff" ? " Powering it on again needs someone at the machine." : ""),
    async () => { try { say((await post("/api/admin/shutdown", { action: "now" })).note); } catch (e) { say(e.message, true); } },
    action === "poweroff" ? "power off" : null);
}
