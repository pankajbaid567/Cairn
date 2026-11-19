import { useQuery } from '@tanstack/react-query';

import { describeError, getMyAudit } from './api';

/** The signed-in persona's own recent searches, answers and decision views (ids and counts only). */
export function AuditPage() {
  const audit = useQuery({ queryKey: ['my-audit'], queryFn: getMyAudit });
  return (
    <section aria-labelledby="audit-h">
      <h2 id="audit-h">Access and audit</h2>
      <p>Your own recent requests. Only you can see this list; it records counts, not content.</p>
      {audit.isPending ? <p>Loading…</p> : null}
      {audit.isError ? <p role="alert">{describeError(audit.error)}</p> : null}
      {audit.data ? (
        audit.data.entries.length === 0 ? (
          <p>Nothing yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">What</th>
                <th scope="col">Outcome</th>
                <th scope="col">Sources returned</th>
              </tr>
            </thead>
            <tbody>
              {audit.data.entries.map((e) => (
                <tr key={e.audit_id}>
                  <td>{e.at.slice(0, 19).replace('T', ' ')}</td>
                  <td>{e.endpoint}</td>
                  <td>{e.outcome}</td>
                  <td>{e.returned}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : null}
    </section>
  );
}
