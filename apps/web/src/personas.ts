/** What each demo persona is, so a visitor sees why their answers differ. Display only. */
export const PERSONA_ROLES: Record<string, string> = {
  asha: 'Payments engineer (member). Reads engineering docs, payments chat and meeting notes.',
  ben: 'Software engineer (member). Reads shared engineering material, not private channels.',
  carla: 'HR partner (manager). Reads HR-private conversations that engineers cannot see.',
  dev: 'Engineering manager and workspace owner. Sees everything, including the audit trail.',
  iris: 'Security engineer with the auditor role. Reads the audit trail, controls and evidence, but no content.',
  priya: 'Security admin (admin). Manages people; reads security policies and incident channels.',
};

export const EXAMPLE_QUESTIONS = [
  'Why did we move payment-service from MongoDB to PostgreSQL?',
  'Who approved the PostgreSQL migration?',
  'Is Kafka still used for ledger events?',
  'What happens to the refund event shape after the move?',
];

export function initials(name: string): string {
  return name.trim().slice(0, 1).toUpperCase() || '?';
}
