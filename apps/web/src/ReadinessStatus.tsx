import { useReadiness } from './use-readiness';

/** Shows API readiness as text. Status never depends on color. */
export function ReadinessStatus() {
  const { data, isPending, isError } = useReadiness();

  let label: string;
  let detail: string | null = null;
  if (isPending) {
    label = 'Checking';
  } else if (isError) {
    label = 'Not ready';
    detail = 'API unreachable';
  } else if (data === 'ready') {
    label = 'Ready';
  } else {
    label = 'Not ready';
    detail = 'API reports not ready';
  }

  return (
    <p role="status" aria-live="polite">
      API status: <strong>{label}</strong>
      {detail ? <span> ({detail})</span> : null}
    </p>
  );
}
