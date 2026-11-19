import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { AUDIT_EXPORT_URL, getAuditEvents, verifyAudit, type Session } from './api';
import { AuditPage } from './AuditPage';
import { Empty, ErrorNote, Loading, PageHead, Tabs } from './ui';
import { when } from './ui-helpers';

type Tab = 'trail' | 'mine';

const TONE: Record<string, string> = { ok: 'badge-ok', denied: 'badge-bad', failed: 'badge-warn' };

function Verify() {
  const check = useMutation({ mutationFn: verifyAudit });
  return (
    <div className="card card-pad stack">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <strong>Integrity check</strong>
          <p className="muted" style={{ fontSize: 12.5 }}>
            Every event is chained to the one before it with a SHA-256 hash. Editing or deleting an
            event breaks the chain from that point.
          </p>
        </div>
        <button type="button" disabled={check.isPending} onClick={() => check.mutate()}>
          {check.isPending ? 'Checking…' : 'Verify the chain'}
        </button>
      </div>
      {check.isError ? <ErrorNote error={check.error} /> : null}
      {check.data ? (
        check.data.ok ? (
          <p
            className="notice"
            role="status"
            style={{ background: 'var(--ok-soft)', color: 'var(--ok)' }}
          >
            Chain intact: {check.data.checked} events checked, head #{check.data.head_seq}{' '}
            <span className="mono">{check.data.head_hash?.slice(0, 16)}…</span>
          </p>
        ) : (
          <p className="notice notice-bad" role="alert">
            Chain broken at event #{check.data.first_bad_seq}: an event was changed or removed.
          </p>
        )
      ) : null}
    </div>
  );
}

/** The workspace's audit trail, an integrity check, an export, and the viewer's own requests. */
export function OrgAuditPage({ session }: { session: Session }) {
  const [tab, setTab] = useState<Tab>('trail');
  const [action, setAction] = useState('');
  const reader = session.role === 'owner' || session.role === 'admin' || session.role === 'auditor';
  const events = useQuery({
    queryKey: ['org-audit', action],
    queryFn: () => getAuditEvents(action || undefined),
  });
  return (
    <section aria-labelledby="audit-trail-h" className="stack">
      <PageHead
        title="Audit trail"
        id="audit-trail-h"
        actions={
          reader ? (
            <a
              className="btn-ghost"
              href={AUDIT_EXPORT_URL}
              download
              style={{ textDecoration: 'none', display: 'inline-flex', alignItems: 'center' }}
            >
              Export CSV
            </a>
          ) : null
        }
      >
        {reader
          ? 'Everything people did in this workspace: sign-ins, role changes, approvals, exports. It records ids and counts, never content.'
          : 'Your own activity in this workspace. Owners, admins and auditors see everyone.'}
      </PageHead>
      <Tabs
        label="Audit sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: 'trail', label: reader ? 'Workspace trail' : 'My activity' },
          { id: 'mine', label: 'My answers and searches' },
        ]}
      />
      {tab === 'mine' ? <AuditPage /> : null}
      {tab === 'trail' ? (
        <div className="stack">
          {reader ? <Verify /> : null}
          <div className="row">
            <label htmlFor="audit-filter">Filter by action</label>
            <input
              id="audit-filter"
              value={action}
              placeholder="member.invited"
              onChange={(e) => setAction(e.target.value.trim())}
            />
          </div>
          {events.isPending ? <Loading /> : null}
          {events.isError ? <ErrorNote error={events.error} /> : null}
          {events.data && events.data.events.length === 0 ? <Empty>No events yet.</Empty> : null}
          {events.data && events.data.events.length > 0 ? (
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">#</th>
                    <th scope="col">When (UTC)</th>
                    <th scope="col">Who</th>
                    <th scope="col">Action</th>
                    <th scope="col">Target</th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {events.data.events.map((e) => (
                    <tr key={e.seq}>
                      <td className="mono">{e.seq}</td>
                      <td className="mono">{when(e.at)}</td>
                      <td>
                        {e.actor ?? <span className="muted">system</span>}
                        {e.actor_role ? <span className="muted"> · {e.actor_role}</span> : null}
                      </td>
                      <td className="mono">{e.action}</td>
                      <td className="mono">
                        {e.target_type
                          ? `${e.target_type}${e.target_id ? ` ${e.target_id.slice(0, 12)}` : ''}`
                          : ''}
                      </td>
                      <td>
                        <span className={`badge ${TONE[e.outcome] ?? ''}`}>{e.outcome}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
