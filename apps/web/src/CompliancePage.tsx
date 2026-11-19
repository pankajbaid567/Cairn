import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';

import {
  anchorNow,
  closeAccessReview,
  decideReviewItem,
  EVIDENCE_PACK_URL,
  getAccessReviews,
  getAnchors,
  getControls,
  startAccessReview,
  verifyAudit,
  type AccessReview,
  type Control,
  type ReviewItem2,
  type Session,
} from './api';
import { Empty, ErrorNote, Loading, PageHead, RoleBadge, Tabs } from './ui';
import { when } from './ui-helpers';

type Tab = 'controls' | 'reviews' | 'evidence';

const TONE: Record<Control['status'], string> = {
  pass: 'badge-ok',
  fail: 'badge-bad',
  no_evidence: 'badge-plain',
};
const LABEL: Record<Control['status'], string> = {
  pass: 'Passing',
  fail: 'Failing',
  no_evidence: 'No evidence yet',
};
const ROUTE_TEXT: Record<string, string> = {
  governance: 'Governance',
  people: 'People',
  audit: 'Audit trail',
  reports: 'Daily reports',
  compliance: 'Access reviews',
};

function Controls() {
  const controls = useQuery({ queryKey: ['controls'], queryFn: getControls });
  if (controls.isPending) return <Loading height={200} />;
  if (controls.isError) return <ErrorNote error={controls.error} />;
  const { counts, note } = controls.data;
  const groups = [...new Set(controls.data.controls.map((c) => c.category))];
  return (
    <div className="stack">
      <div className="tiles">
        <div className="card tile">
          <div className="tile-num" style={{ color: 'var(--ok)' }}>
            {counts.pass}
          </div>
          <div className="tile-label">passing</div>
        </div>
        <div className="card tile">
          <div className="tile-num" style={{ color: counts.fail ? 'var(--bad)' : undefined }}>
            {counts.fail}
          </div>
          <div className="tile-label">failing</div>
        </div>
        <div className="card tile">
          <div className="tile-num">{counts.no_evidence}</div>
          <div className="tile-label">no evidence yet</div>
        </div>
      </div>
      <p className="notice" role="note">
        {note}
      </p>
      {groups.map((g) => (
        <div key={g} className="stack" style={{ gap: 8 }}>
          <p className="section-title">{g}</p>
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Control</th>
                  <th scope="col">Status</th>
                  <th scope="col">What the records show</th>
                </tr>
              </thead>
              <tbody>
                {controls.data.controls
                  .filter((c) => c.category === g)
                  .map((c) => (
                    <tr key={c.id}>
                      <td>
                        <span className="mono">{c.id}</span> <strong>{c.title}</strong>
                      </td>
                      <td>
                        <span className={`badge ${TONE[c.status]}`}>{LABEL[c.status]}</span>
                      </td>
                      <td>
                        {c.detail} <span className="muted">({ROUTE_TEXT[c.route] ?? c.route})</span>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

function ItemRow({ item, review }: { item: ReviewItem2; review: AccessReview }) {
  const client = useQueryClient();
  const [decision, setDecision] = useState('keep');
  const [role, setRole] = useState('member');
  const [comment, setComment] = useState('');
  const decide = useMutation({
    mutationFn: () =>
      decideReviewItem(item.item_id, {
        decision,
        comment: comment.trim(),
        ...(decision === 'change' ? { new_role: role } : {}),
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['access-reviews'] });
      await client.invalidateQueries({ queryKey: ['members'] });
      await client.invalidateQueries({ queryKey: ['controls'] });
    },
  });
  const open = review.state === 'open' && item.decision === 'pending';
  return (
    <tr>
      <td>
        <strong>{item.member}</strong>
        <div className="muted" style={{ fontSize: 12.5 }}>
          {item.teams.length ? item.teams.join(', ') : 'no team'}
        </div>
      </td>
      <td>
        <RoleBadge role={item.role} />
      </td>
      <td>{item.reviewer}</td>
      <td>
        {item.decision === 'pending' ? (
          item.mine && open ? (
            <div className="row">
              <select
                aria-label={`Decision for ${item.member}`}
                value={decision}
                onChange={(e) => setDecision(e.target.value)}
              >
                <option value="keep">Keep access</option>
                <option value="change">Change role</option>
                <option value="remove">Remove</option>
              </select>
              {decision === 'change' ? (
                <select
                  aria-label={`New role for ${item.member}`}
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                >
                  {['admin', 'manager', 'member', 'auditor', 'guest']
                    .filter((r) => r !== item.role)
                    .map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                </select>
              ) : null}
              <input
                aria-label={`Comment for ${item.member}`}
                value={comment}
                maxLength={500}
                placeholder="Comment"
                onChange={(e) => setComment(e.target.value)}
              />
              <button type="button" disabled={decide.isPending} onClick={() => decide.mutate()}>
                Save
              </button>
              {decide.isError ? <ErrorNote error={decide.error} /> : null}
            </div>
          ) : (
            <span className="badge badge-warn">Waiting for {item.reviewer}</span>
          )
        ) : (
          <span
            className={`badge ${item.decision === 'remove' ? 'badge-bad' : item.decision === 'change' ? 'badge-warn' : 'badge-ok'}`}
          >
            {item.decision}
            {item.new_role ? ` → ${item.new_role}` : ''}
          </span>
        )}
      </td>
    </tr>
  );
}

function Reviews({ session }: { session: Session }) {
  const client = useQueryClient();
  const titleId = useId();
  const list = useQuery({ queryKey: ['access-reviews'], queryFn: getAccessReviews });
  const [title, setTitle] = useState('');
  const admin = session.role === 'owner' || session.role === 'admin';
  const refresh = async () => {
    await client.invalidateQueries({ queryKey: ['access-reviews'] });
    await client.invalidateQueries({ queryKey: ['controls'] });
  };
  const start = useMutation({
    mutationFn: () => startAccessReview(title.trim(), 14),
    onSuccess: async () => {
      setTitle('');
      await refresh();
    },
  });
  const close = useMutation({ mutationFn: closeAccessReview, onSuccess: refresh });
  if (list.isPending) return <Loading height={160} />;
  if (list.isError) return <ErrorNote error={list.error} />;
  const open = list.data.reviews.find((r) => r.state === 'open');
  const past = list.data.reviews.filter((r) => r.state === 'closed');
  return (
    <div className="stack">
      {open ? (
        <section className="card card-pad stack" aria-label={`Access review ${open.title}`}>
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <strong>{open.title}</strong>
              <div className="muted" style={{ fontSize: 12.5 }}>
                Started by {open.started_by} · due {when(open.due_at).slice(0, 10)}
                {open.overdue ? ' · overdue' : ''}
              </div>
            </div>
            <span className={`badge ${open.decided === open.total ? 'badge-ok' : 'badge-warn'}`}>
              {open.decided} of {open.total} decided
            </span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Person</th>
                  <th scope="col">Role</th>
                  <th scope="col">Reviewer</th>
                  <th scope="col">Decision</th>
                </tr>
              </thead>
              <tbody>
                {open.items.map((i) => (
                  <ItemRow key={i.item_id} item={i} review={open} />
                ))}
              </tbody>
            </table>
          </div>
          {admin ? (
            <div className="row">
              <button
                type="button"
                disabled={close.isPending || open.decided < open.total}
                onClick={() => close.mutate(open.review_id)}
              >
                Close review
              </button>
              {open.decided < open.total ? (
                <span className="muted">Every item must be decided first.</span>
              ) : null}
            </div>
          ) : null}
          {close.isError ? <ErrorNote error={close.error} /> : null}
        </section>
      ) : admin ? (
        <form
          className="card card-pad stack"
          aria-label="Start an access review"
          onSubmit={(e) => {
            e.preventDefault();
            if (title.trim().length >= 3) start.mutate();
          }}
        >
          <p className="section-title">Start an access review</p>
          <p className="muted">
            Every active member is assigned a reviewer (an owner or admin, never themselves) who
            keeps, changes or removes their access.
          </p>
          <div className="row">
            <label htmlFor={titleId}>Title</label>
            <input
              id={titleId}
              value={title}
              maxLength={120}
              placeholder="Q4 2026 access review"
              style={{ flex: 1, minWidth: 220 }}
              onChange={(e) => setTitle(e.target.value)}
            />
            <button type="submit" disabled={start.isPending || title.trim().length < 3}>
              Start review
            </button>
          </div>
          {start.isError ? <ErrorNote error={start.error} /> : null}
        </form>
      ) : (
        <Empty>No access review is open.</Empty>
      )}
      {past.length > 0 ? (
        <div className="stack" style={{ gap: 8 }}>
          <p className="section-title">Completed reviews</p>
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Review</th>
                  <th scope="col">Closed</th>
                  <th scope="col">Kept</th>
                  <th scope="col">Changed</th>
                  <th scope="col">Removed</th>
                </tr>
              </thead>
              <tbody>
                {past.map((r) => (
                  <tr key={r.review_id}>
                    <td>{r.title}</td>
                    <td className="mono">{r.closed_at ? when(r.closed_at).slice(0, 10) : ''}</td>
                    <td>{r.items.filter((i) => i.decision === 'keep').length}</td>
                    <td>{r.items.filter((i) => i.decision === 'change').length}</td>
                    <td>{r.items.filter((i) => i.decision === 'remove').length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Evidence() {
  const client = useQueryClient();
  const anchors = useQuery({ queryKey: ['anchors'], queryFn: getAnchors, retry: false });
  const verify = useMutation({ mutationFn: verifyAudit });
  const anchor = useMutation({
    mutationFn: anchorNow,
    onSuccess: () => client.invalidateQueries({ queryKey: ['anchors'] }),
  });
  return (
    <div className="stack">
      <div className="card card-pad stack">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div>
            <strong>Evidence pack</strong>
            <p className="muted" style={{ fontSize: 12.5 }}>
              A zip with the audit trail, approvals, attestations, access reviews, risks and members
              as CSV, the control results, and a manifest with a SHA-256 for every file and a
              signature over it.
            </p>
          </div>
          <a
            className="btn-ghost"
            href={EVIDENCE_PACK_URL}
            download
            style={{ textDecoration: 'none', display: 'inline-flex', alignItems: 'center' }}
          >
            Download evidence pack
          </a>
        </div>
      </div>
      <div className="card card-pad stack">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div>
            <strong>Is the audit trail intact?</strong>
            <p className="muted" style={{ fontSize: 12.5 }}>
              The chain check finds edited or deleted events. An anchor stores the chain's head
              outside the database (S3 Object Lock) so even a fully rewritten chain is caught.
            </p>
          </div>
          <div className="row">
            <button
              type="button"
              className="btn-ghost"
              disabled={verify.isPending}
              onClick={() => verify.mutate()}
            >
              Verify chain
            </button>
            {anchors.data ? (
              <button type="button" disabled={anchor.isPending} onClick={() => anchor.mutate()}>
                Anchor now
              </button>
            ) : null}
          </div>
        </div>
        {verify.data ? (
          verify.data.ok ? (
            <p
              className="notice"
              role="status"
              style={{ background: 'var(--ok-soft)', color: 'var(--ok)' }}
            >
              Chain intact: {verify.data.checked} events checked.
            </p>
          ) : (
            <p className="notice notice-bad" role="alert">
              Chain broken at event #{verify.data.first_bad_seq}.
            </p>
          )
        ) : null}
        {anchor.isError ? <ErrorNote error={anchor.error} /> : null}
        {anchors.data ? (
          <>
            <p className={anchors.data.ok ? 'notice' : 'notice notice-bad'} role="status">
              {anchors.data.anchors.length === 0
                ? 'No anchors yet. Anchor now to record the head of the chain outside the database.'
                : anchors.data.ok
                  ? `Every anchor still matches the database (${anchors.data.anchors.length} stored).`
                  : 'An anchor no longer matches the database: events were rewritten after they were anchored.'}
            </p>
            {anchors.data.anchors.length > 0 ? (
              <div className="card table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Event #</th>
                      <th scope="col">Anchored (UTC)</th>
                      <th scope="col">Hash</th>
                      <th scope="col">Check</th>
                    </tr>
                  </thead>
                  <tbody>
                    {anchors.data.anchors.slice(0, 10).map((a) => (
                      <tr key={a.seq}>
                        <td className="mono">{a.seq}</td>
                        <td className="mono">{when(a.at)}</td>
                        <td className="mono">{a.head_hash.slice(0, 16)}…</td>
                        <td>
                          <span
                            className={`badge ${a.status === 'match' ? 'badge-ok' : 'badge-bad'}`}
                          >
                            {a.status === 'match' ? 'matches' : a.status.replace('_', ' ')}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </>
        ) : (
          <p className="muted">External anchors are not configured on this deployment.</p>
        )}
      </div>
    </div>
  );
}

/** Controls, access reviews and evidence: what an auditor asks for, in one place. */
export function CompliancePage({ session }: { session: Session }) {
  const role = session.role ?? 'guest';
  const reader = role === 'owner' || role === 'admin' || role === 'auditor';
  const [tab, setTab] = useState<Tab>(reader ? 'controls' : 'reviews');
  return (
    <section aria-labelledby="compliance-h" className="stack">
      <PageHead title="Compliance" id="compliance-h">
        The records an auditor asks for, ready: control checks, access reviews, and a signed
        evidence pack. This is evidence, not a certification.
      </PageHead>
      <Tabs
        label="Compliance sections"
        value={tab}
        onChange={setTab}
        tabs={[
          ...(reader ? [{ id: 'controls' as const, label: 'Controls' }] : []),
          { id: 'reviews', label: 'Access reviews' },
          ...(reader ? [{ id: 'evidence' as const, label: 'Evidence and integrity' }] : []),
        ]}
      />
      {tab === 'controls' && reader ? <Controls /> : null}
      {tab === 'reviews' ? <Reviews session={session} /> : null}
      {tab === 'evidence' && reader ? <Evidence /> : null}
    </section>
  );
}
