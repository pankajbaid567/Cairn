/** Plain helpers and labels shared by the screens. */

export const ROLE_LABEL: Record<string, string> = {
  owner: 'Owner',
  admin: 'Admin',
  manager: 'Manager',
  member: 'Member',
  auditor: 'Auditor',
  guest: 'Guest',
};

export const ROLE_HELP: Record<string, string> = {
  owner: 'Full control, including billing and other admins.',
  admin: 'Manages people, teams and settings.',
  manager: 'Sees the reports of the people they manage; approves decisions.',
  member: 'Works in the workspace: asks, reports, proposes decisions.',
  auditor: 'Read-only access to the audit trail and evidence. No content.',
  guest: 'Limited: sees only what is shared with them directly.',
};

export function when(iso: string): string {
  return iso.slice(0, 16).replace('T', ' ');
}

/** An ISO calendar day (UTC) ``offset`` days from today. */
export function isoDay(offset: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + offset);
  return d.toISOString().slice(0, 10);
}

/** "Wed 19 Nov" for an ISO day. */
export function dayLabel(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return d.toLocaleDateString('en-GB', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  });
}
