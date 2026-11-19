import { useQuery } from '@tanstack/react-query';

import { describeError, getSyncSources } from './api';

/** Sources the signed-in persona can read, with freshness and what is waiting to be applied. */
export function SyncPage() {
  const sync = useQuery({
    queryKey: ['sync-sources'],
    queryFn: getSyncSources,
    refetchInterval: 15_000,
  });
  return (
    <section aria-labelledby="sync-h">
      <h2 id="sync-h">Sources and sync health</h2>
      {sync.isPending ? <p>Loading…</p> : null}
      {sync.isError ? <p role="alert">{describeError(sync.error)}</p> : null}
      {sync.data ? (
        <div>
          <p>
            Waiting to apply: {sync.data.pending_acl} permission events, {sync.data.pending_content}{' '}
            content events.
            {sync.data.pending_acl > 0
              ? ' Answers pause until the permission events are applied.'
              : ''}
          </p>
          {sync.data.sources.length === 0 ? (
            <p>No sources you can read.</p>
          ) : (
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
                    <td>{s.source}</td>
                    <td>{s.badge}</td>
                    <td>{s.last_synced_at ?? 'not live'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
    </section>
  );
}
