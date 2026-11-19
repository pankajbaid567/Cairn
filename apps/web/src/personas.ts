/** What each demo persona is, so a visitor sees why their answers differ. Display only. */
export const PERSONA_ROLES: Record<string, string> = {
  asha: 'Payments engineer. Reads engineering docs, payments chat and meeting notes.',
  ben: 'Software engineer. Reads shared engineering material, not private channels.',
  carla: 'HR partner. Reads HR-private conversations that engineers cannot see.',
  priya: 'Security admin. Reads security policies and incident channels.',
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
