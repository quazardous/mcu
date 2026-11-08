// the "invite a project" dialog, one for the whole app: openInvite() shows it
export const inviting = $state({ open: false });

export function openInvite() {
  inviting.open = true;
}
