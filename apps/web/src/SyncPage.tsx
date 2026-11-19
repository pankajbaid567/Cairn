import { useQuery } from '@tanstack/react-query';

import { describeError, getSyncSources } from './api';

const TONE: Record<string, string> = {
  Live: 'badge-ok',
  Imported: 'badge-info',
  Delayed: 'badge-warn',
  Stale: 'badge-bad',
};

/** Sources the signed-in persona can read, with freshness and what is waiting to be applied. */
export function SyncPage() {
  const sync = useQuery({
    queryKey: ['sync-sources'],
    queryFn: getSyncSources,
    refetchInterval: 15_000,
  });
  return (
    <section aria-labelledby="sync-h" className="stack">
      <div className="page-head">
        <h2 id="sync-h">Sources and sync health</h2>
        <p>Where answers come from, how fresh each source is, and what is still being applied.</p>
      </div>
      {sync.isPending ? <div className="skeleton" style={{ height: 80 }} /> : null}
      {sync.isError ? (
        <p role="alert" className="notice notice-bad">
          {describeError(sync.error)}
        </p>
      ) : null}
      {sync.data ? (
        <div className="stack">
          <div className="tiles">
            <div className="card tile">
              <div className="tile-num">{sync.data.sources.length}</div>
              <div className="tile-label">sources you can read</div>
            </div>
            <div className="card tile">
              <div className="tile-num">{sync.data.pending_acl + sync.data.pending_content}</div>
              <div className="tile-label">events waiting to apply</div>
            </div>
          </div>
          <p className={sync.data.pending_acl > 0 ? 'notice notice-warn' : 'muted'}>
            Waiting to apply: {sync.data.pending_acl} permission events, {sync.data.pending_content}{' '}
            content events.
            {sync.data.pending_acl > 0
              ? ' Answers pause until the permission events are applied.'
              : ''}
          </p>
          {sync.data.sources.length === 0 ? (
            <p className="card empty">No sources you can read.</p>
          ) : (
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col">Freshness</th>
                    <th scope="col">Last synced</th>
                  </tr>
                </thead>
                <tbody>
                  {sync.data.sources.map((s) => (
                    <tr key={s.source}>
                      <td className="mono">{s.source}</td>
                      <td>
                        <span className={`badge ${TONE[s.badge] ?? ''}`}>{s.badge}</span>
                      </td>
                      <td>{s.last_synced_at ?? 'not live'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ) : null}
    </section>
  );
}
