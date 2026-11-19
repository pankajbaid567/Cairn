import { useReadiness } from './use-readiness';

/** Shows API readiness as text. Status never depends on color. */
export function ReadinessStatus() {
  const { data, isPending, isError } = useReadiness();

  let label: string;
  let detail: string | null = null;
  let tone = '';
  if (isPending) {
    label = 'Checking';
  } else if (isError) {
    label = 'Not ready';
    detail = 'API unreachable';
    tone = 'dot-bad';
  } else if (data === 'ready') {
    label = 'Ready';
    tone = 'dot-ok';
  } else {
    label = 'Not ready';
    detail = 'API reports not ready';
    tone = 'dot-bad';
  }

  return (
    <p role="status" aria-live="polite" className="status-pill">
      <span className={`dot ${tone}`} aria-hidden="true" />
      <span>
        API status: <strong>{label}</strong>
        {detail ? <span> ({detail})</span> : null}
      </span>
    </p>
  );
}
