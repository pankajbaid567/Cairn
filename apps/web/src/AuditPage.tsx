import { useQuery } from '@tanstack/react-query';

import { describeError, getMyAudit } from './api';

/** The signed-in persona's own recent searches, answers and decision views (ids and counts only). */
export function AuditPage() {
  const audit = useQuery({ queryKey: ['my-audit'], queryFn: getMyAudit });
  return (
    <section aria-labelledby="audit-h" className="stack">
      <div className="page-head">
        <h2 id="audit-h">Access and audit</h2>
        <p>Your own recent requests. Only you can see this list; it records counts, not content.</p>
      </div>
      {audit.isPending ? <div className="skeleton" style={{ height: 80 }} /> : null}
      {audit.isError ? (
        <p role="alert" className="notice notice-bad">
          {describeError(audit.error)}
        </p>
      ) : null}
      {audit.data ? (
        audit.data.entries.length === 0 ? (
          <p className="card empty">Nothing yet.</p>
        ) : (
          <div className="card table-wrap">
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
                    <td className="mono">{e.at.slice(0, 19).replace('T', ' ')}</td>
                    <td className="mono">{e.endpoint}</td>
                    <td>
                      <span className="badge badge-plain">{e.outcome}</span>
                    </td>
                    <td>{e.returned}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : null}
    </section>
  );
}
