import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import {
  addRisk,
  attestPolicy,
  createPolicy,
  getApprovals,
  getManualDecisions,
  getPolicies,
  getPolicy,
  getRisks,
  markImplemented,
  patchRisk,
  proposeDecision,
  requestApproval,
  savePolicyDraft,
  submitPolicy,
  voteOnApproval,
  withdrawApproval,
  type Approval,
  type PolicyRow,
  type Risk,
  type Session,
} from './api';
import { Empty, ErrorNote, Loading, PageHead, Tabs } from './ui';
import { when } from './ui-helpers';

type Tab = 'approvals' | 'decisions' | 'policies' | 'risks';

const STATUS_TONE: Record<string, string> = {
  accepted: 'badge-ok',
  implemented: 'badge-ok',
  proposed: 'badge-info',
  rejected: 'badge-bad',
  withdrawn: 'badge-plain',
  approved: 'badge-ok',
  pending: 'badge-warn',
  active: 'badge-ok',
  draft: 'badge-plain',
  published: 'badge-ok',
  superseded: 'badge-plain',
};

function Status({ s }: { s: string }) {
  return <span className={`badge ${STATUS_TONE[s] ?? ''}`}>{s}</span>;
}

function useRefresh() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      [
        'approvals',
        'manual-decisions',
        'policies',
        'policy',
        'risks',
        'gov-summary',
        'members',
      ].map((k) => client.invalidateQueries({ queryKey: [k] })),
    );
}

// ---------------------------------------------------------------- approvals

function ApprovalCard({ a }: { a: Approval }) {
  const refresh = useRefresh();
  const [comment, setComment] = useState('');
  const vote = useMutation({
    mutationFn: (v: 'approve' | 'reject') => voteOnApproval(a.approval_id, v, comment.trim()),
    onSuccess: refresh,
  });
  const withdraw = useMutation({
    mutationFn: () => withdrawApproval(a.approval_id),
    onSuccess: refresh,
  });
  return (
    <article className="card card-pad stack" aria-label={`Approval: ${a.title}`}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <strong>{a.title}</strong>
          <div className="muted" style={{ fontSize: 12.5 }}>
            {a.kind} · requested by {a.mine ? 'you' : a.requested_by} · {when(a.created_at)} UTC
          </div>
        </div>
        <div className="row">
          <span className="badge badge-plain">
            {a.approvals} of {a.required} approvals
          </span>
          <Status s={a.state} />
        </div>
      </div>
      {typeof a.detail.reason === 'string' && a.detail.reason ? (
        <p className="muted">{a.detail.reason}</p>
      ) : null}
      {typeof a.detail.role === 'string' ? (
        <p>
          Requested role: <strong>{a.detail.role}</strong>
        </p>
      ) : null}
      {a.state === 'pending' && a.mine ? (
        <p className="notice" role="status">
          You made this request, so someone else has to decide it.
        </p>
      ) : null}
      {a.can_vote ? (
        <div className="row">
          <input
            aria-label="Comment"
            value={comment}
            maxLength={500}
            placeholder="Comment (optional)"
            style={{ flex: 1, minWidth: 180 }}
            onChange={(e) => setComment(e.target.value)}
          />
          <button type="button" disabled={vote.isPending} onClick={() => vote.mutate('approve')}>
            Approve
          </button>
          <button
            type="button"
            className="btn-ghost"
            disabled={vote.isPending}
            onClick={() => vote.mutate('reject')}
          >
            Reject
          </button>
        </div>
      ) : null}
      {a.state === 'pending' && a.mine ? (
        <div className="row">
          <button
            type="button"
            className="btn-ghost"
            disabled={withdraw.isPending}
            onClick={() => withdraw.mutate()}
          >
            Withdraw request
          </button>
        </div>
      ) : null}
      {vote.isError ? <ErrorNote error={vote.error} /> : null}
      {withdraw.isError ? <ErrorNote error={withdraw.error} /> : null}
    </article>
  );
}

function RequestForm({ role }: { role: string }) {
  const refresh = useRefresh();
  const ids = [useId(), useId(), useId()];
  const [kind, setKind] = useState<'access' | 'exception'>('access');
  const [title, setTitle] = useState('');
  const [reason, setReason] = useState('');
  const [want, setWant] = useState('manager');
  const [expires, setExpires] = useState('');
  const lead = ['owner', 'admin', 'manager'].includes(role);
  const create = useMutation({
    mutationFn: () =>
      requestApproval({
        kind,
        title: title.trim(),
        reason: reason.trim(),
        ...(kind === 'access' ? { role: want } : {}),
        ...(kind === 'exception' && expires ? { expires_at: expires } : {}),
      }),
    onSuccess: async () => {
      setTitle('');
      setReason('');
      await refresh();
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (title.trim().length >= 3) create.mutate();
  }
  if (role === 'guest') return null;
  return (
    <form onSubmit={submit} className="card card-pad stack" aria-label="Make a request">
      <p className="section-title">Make a request</p>
      <div className="field-row">
        <div className="field">
          <label htmlFor={ids[0]}>Type</label>
          <select
            id={ids[0]}
            value={kind}
            onChange={(e) => setKind(e.target.value as 'access' | 'exception')}
          >
            <option value="access">Access: a different role</option>
            {lead ? <option value="exception">Exception: a time-limited waiver</option> : null}
          </select>
        </div>
        {kind === 'access' ? (
          <div className="field">
            <label htmlFor={ids[1]}>Role you need</label>
            <select id={ids[1]} value={want} onChange={(e) => setWant(e.target.value)}>
              <option value="manager">Manager</option>
              <option value="member">Member</option>
              <option value="auditor">Auditor</option>
            </select>
          </div>
        ) : (
          <div className="field">
            <label htmlFor={ids[1]}>Expires on</label>
            <input
              id={ids[1]}
              type="date"
              value={expires}
              onChange={(e) => setExpires(e.target.value)}
            />
          </div>
        )}
      </div>
      <div className="field">
        <label htmlFor={ids[2]}>Title</label>
        <input
          id={ids[2]}
          value={title}
          maxLength={160}
          placeholder={
            kind === 'access'
              ? 'Need manager access to lead Payments'
              : 'Waive the 90-day password rotation for the build bot'
          }
          onChange={(e) => setTitle(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor={`${ids[2]}-r`}>Why</label>
        <textarea
          id={`${ids[2]}-r`}
          rows={2}
          value={reason}
          maxLength={1000}
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      {create.isError ? <ErrorNote error={create.error} /> : null}
      <div className="row">
        <button type="submit" disabled={create.isPending || title.trim().length < 3}>
          Send request
        </button>
      </div>
    </form>
  );
}

function ApprovalsTab({ session }: { session: Session }) {
  const [filter, setFilter] = useState<'pending' | 'all'>('pending');
  const list = useQuery({ queryKey: ['approvals', filter], queryFn: () => getApprovals(filter) });
  return (
    <div className="stack">
      <div className="row">
        <label htmlFor="appr-filter">Show</label>
        <select
          id="appr-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as 'pending' | 'all')}
        >
          <option value="pending">Waiting for a decision</option>
          <option value="all">Everything</option>
        </select>
      </div>
      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorNote error={list.error} /> : null}
      {list.data && list.data.approvals.length === 0 ? (
        <Empty>Nothing {filter === 'pending' ? 'waiting' : 'yet'}.</Empty>
      ) : null}
      {list.data?.approvals.map((a) => (
        <ApprovalCard key={a.approval_id} a={a} />
      ))}
      <RequestForm role={session.role ?? 'guest'} />
    </div>
  );
}

// ---------------------------------------------------------------- decisions

function DecisionsTab({ session }: { session: Session }) {
  const refresh = useRefresh();
  const ids = [useId(), useId(), useId(), useId()];
  const list = useQuery({ queryKey: ['manual-decisions'], queryFn: getManualDecisions });
  const [title, setTitle] = useState('');
  const [question, setQuestion] = useState('');
  const [outcome, setOutcome] = useState('');
  const [required, setRequired] = useState(1);
  const role = session.role ?? 'guest';
  const lead = ['owner', 'admin', 'manager'].includes(role);
  const propose = useMutation({
    mutationFn: () =>
      proposeDecision({
        title: title.trim(),
        question: question.trim(),
        outcome: outcome.trim(),
        required,
      }),
    onSuccess: async () => {
      setTitle('');
      setQuestion('');
      setOutcome('');
      await refresh();
    },
  });
  const done = useMutation({
    mutationFn: (id: number) => markImplemented(id, ''),
    onSuccess: refresh,
  });
  const canPropose = ['owner', 'admin', 'manager', 'member'].includes(role);
  return (
    <div className="stack">
      {canPropose ? (
        <form
          className="card card-pad stack"
          aria-label="Propose a decision"
          onSubmit={(e) => {
            e.preventDefault();
            if (title.trim().length >= 3 && outcome.trim().length >= 3) propose.mutate();
          }}
        >
          <p className="section-title">Propose a decision</p>
          <div className="field">
            <label htmlFor={ids[0]}>Title</label>
            <input
              id={ids[0]}
              value={title}
              maxLength={160}
              placeholder="Adopt PostgreSQL 18 for the payments service"
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor={ids[1]}>Question this answers</label>
            <input
              id={ids[1]}
              value={question}
              maxLength={500}
              placeholder="Which database should payments use?"
              onChange={(e) => setQuestion(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor={ids[2]}>Decision and reasons</label>
            <textarea
              id={ids[2]}
              rows={3}
              value={outcome}
              maxLength={1000}
              onChange={(e) => setOutcome(e.target.value)}
            />
          </div>
          <div className="field" style={{ maxWidth: 260 }}>
            <label htmlFor={ids[3]}>Approvals needed</label>
            <select
              id={ids[3]}
              value={required}
              onChange={(e) => setRequired(Number(e.target.value))}
            >
              <option value={1}>1 approver</option>
              <option value={2}>2 approvers</option>
              <option value={3}>3 approvers</option>
            </select>
          </div>
          {propose.isError ? <ErrorNote error={propose.error} /> : null}
          <p className="muted" style={{ fontSize: 12.5 }}>
            Approvers are managers, admins or owners. You cannot approve your own proposal.
          </p>
          <div className="row">
            <button
              type="submit"
              disabled={propose.isPending || title.trim().length < 3 || outcome.trim().length < 3}
            >
              Submit for approval
            </button>
          </div>
        </form>
      ) : null}
      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorNote error={list.error} /> : null}
      {list.data && list.data.decisions.length === 0 ? (
        <Empty>No decisions proposed in Cairn yet.</Empty>
      ) : null}
      {list.data?.decisions.map((d) => (
        <article
          key={d.decision_id}
          className="card card-pad stack"
          aria-label={`Decision ${d.key}`}
        >
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <span className="mono">{d.key}</span> <strong>{d.title}</strong>
              <div className="muted" style={{ fontSize: 12.5 }}>
                Proposed by {d.proposed_by ?? 'unknown'} · {when(d.created_at)} UTC
              </div>
            </div>
            <div className="row">
              {d.approval_state === 'pending' ? (
                <span className="badge badge-plain">
                  {d.approvals ?? 0} of {d.required ?? 1} approvals
                </span>
              ) : null}
              <Status s={d.status} />
            </div>
          </div>
          {d.question ? <p className="muted">{d.question}</p> : null}
          <p style={{ whiteSpace: 'pre-wrap' }}>{d.outcome}</p>
          {d.status === 'accepted' && lead ? (
            <div className="row">
              <button
                type="button"
                className="btn-ghost"
                disabled={done.isPending}
                onClick={() => done.mutate(d.decision_id)}
              >
                Mark implemented
              </button>
            </div>
          ) : null}
        </article>
      ))}
      {done.isError ? <ErrorNote error={done.error} /> : null}
    </div>
  );
}

// ---------------------------------------------------------------- policies

function PolicyDetailView({
  id,
  onClose,
  canWrite,
}: {
  id: number;
  onClose: () => void;
  canWrite: boolean;
}) {
  const refresh = useRefresh();
  const detail = useQuery({ queryKey: ['policy', id], queryFn: () => getPolicy(id) });
  const [read, setRead] = useState(false);
  const [edit, setEdit] = useState<{ title: string; body: string; summary: string } | null>(null);
  const attest = useMutation({ mutationFn: () => attestPolicy(id), onSuccess: refresh });
  const save = useMutation({
    mutationFn: () => savePolicyDraft(id, edit as { title: string; body: string; summary: string }),
    onSuccess: async () => {
      setEdit(null);
      await refresh();
    },
  });
  const submit = useMutation({ mutationFn: () => submitPolicy(id), onSuccess: refresh });
  if (detail.isPending) return <Loading height={160} />;
  if (detail.isError) return <ErrorNote error={detail.error} />;
  const d = detail.data;
  const published = d.versions.find((v) => v.state === 'published');
  const latest = d.versions[0];
  const draft = latest?.state === 'draft' ? latest : undefined;
  return (
    <div className="card card-pad stack" aria-label={`Policy ${d.policy.title}`}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h3>{d.policy.title}</h3>
        <button type="button" className="btn-ghost" onClick={onClose}>
          Close
        </button>
      </div>
      {published ? (
        <>
          <p className="muted">
            Version {published.version}, published{' '}
            {published.published_at ? when(published.published_at) : ''} UTC
            {published.attest_by ? `, attest by ${published.attest_by.slice(0, 10)}` : ''}.
          </p>
          <pre className="policy-body">{published.body}</pre>
          <label>
            <input type="checkbox" checked={read} onChange={(e) => setRead(e.target.checked)} /> I
            have read and accept this policy
          </label>
          <div className="row">
            <button
              type="button"
              disabled={!read || attest.isPending}
              onClick={() => attest.mutate()}
            >
              Attest
            </button>
            {attest.isSuccess ? <span className="badge badge-ok">Recorded</span> : null}
          </div>
          {attest.isError ? <ErrorNote error={attest.error} /> : null}
        </>
      ) : (
        <p className="muted">No version is published yet.</p>
      )}
      {d.attested.length > 0 || d.missing.length > 0 ? (
        <div className="field-row">
          <div>
            <p className="section-title">Attested ({d.attested.length})</p>
            <ul>
              {d.attested.map((a) => (
                <li key={a.name}>
                  {a.name} <span className="muted">{a.at.slice(0, 10)}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <p className="section-title">Still to attest ({d.missing.length})</p>
            <ul>
              {d.missing.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
      {canWrite && d.can_edit ? (
        <div className="stack">
          <p className="section-title">
            {draft ? `Draft of version ${draft.version}` : 'Propose a change'}
          </p>
          {edit ? (
            <div className="stack">
              <input
                aria-label="Policy title"
                value={edit.title}
                onChange={(e) => setEdit({ ...edit, title: e.target.value })}
              />
              <textarea
                aria-label="Policy text"
                rows={8}
                value={edit.body}
                onChange={(e) => setEdit({ ...edit, body: e.target.value })}
              />
              <input
                aria-label="What changed"
                value={edit.summary}
                placeholder="What changed (one line)"
                onChange={(e) => setEdit({ ...edit, summary: e.target.value })}
              />
              {save.isError ? <ErrorNote error={save.error} /> : null}
              <div className="row">
                <button
                  type="button"
                  disabled={save.isPending || edit.body.trim().length < 10}
                  onClick={() => save.mutate()}
                >
                  Save draft
                </button>
                <button type="button" className="btn-ghost" onClick={() => setEdit(null)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : latest?.state === 'pending' ? (
            <p className="notice" role="status">
              Version {latest.version} is waiting for approval by someone other than its author.
            </p>
          ) : (
            <div className="row">
              <button
                type="button"
                className="btn-ghost"
                onClick={() => {
                  const base = draft ?? published ?? latest;
                  setEdit({
                    title: base?.title ?? d.policy.title,
                    body: base?.body ?? '',
                    summary: '',
                  });
                }}
              >
                {draft ? 'Edit draft' : 'Write a new version'}
              </button>
              {draft ? (
                <button type="button" disabled={submit.isPending} onClick={() => submit.mutate()}>
                  Submit for approval
                </button>
              ) : null}
            </div>
          )}
          {submit.isError ? <ErrorNote error={submit.error} /> : null}
        </div>
      ) : null}
      <div>
        <p className="section-title">History</p>
        <ul className="timeline">
          {d.versions.map((v) => (
            <li key={v.version}>
              v{v.version} <Status s={v.state} /> by {v.author}
              {v.summary ? <span className="muted"> · {v.summary}</span> : null}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function NewPolicy() {
  const refresh = useRefresh();
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const create = useMutation({
    mutationFn: () =>
      createPolicy({ title: title.trim(), body: body.trim(), review_days: 365, attest_days: 30 }),
    onSuccess: async () => {
      setTitle('');
      setBody('');
      await refresh();
    },
  });
  return (
    <form
      className="card card-pad stack"
      aria-label="New policy"
      onSubmit={(e) => {
        e.preventDefault();
        if (title.trim().length >= 3 && body.trim().length >= 10) create.mutate();
      }}
    >
      <p className="section-title">New policy</p>
      <input
        aria-label="Title of the new policy"
        value={title}
        maxLength={160}
        placeholder="Acceptable Use Policy"
        onChange={(e) => setTitle(e.target.value)}
      />
      <textarea
        aria-label="Text of the new policy"
        rows={4}
        value={body}
        maxLength={30000}
        placeholder="What the policy requires of people…"
        onChange={(e) => setBody(e.target.value)}
      />
      {create.isError ? <ErrorNote error={create.error} /> : null}
      <div className="row">
        <button
          type="submit"
          disabled={create.isPending || title.trim().length < 3 || body.trim().length < 10}
        >
          Create draft
        </button>
      </div>
    </form>
  );
}

function PoliciesTab({ session }: { session: Session }) {
  const list = useQuery({ queryKey: ['policies'], queryFn: getPolicies });
  const [open, setOpen] = useState<number | null>(null);
  const role = session.role ?? 'guest';
  const writer = role === 'owner' || role === 'admin';
  const pct = (p: PolicyRow) =>
    p.attested === null || p.expected === 0 ? null : Math.round((p.attested / p.expected) * 100);
  return (
    <div className="stack">
      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorNote error={list.error} /> : null}
      {list.data && list.data.policies.length === 0 ? (
        <Empty>No policies yet.{writer ? ' Create the first one below.' : ''}</Empty>
      ) : null}
      {list.data && list.data.policies.length > 0 ? (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Policy</th>
                <th scope="col">Version</th>
                <th scope="col">Status</th>
                <th scope="col">Attested</th>
                <th scope="col">Review due</th>
                <th scope="col">
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {list.data.policies.map((p) => (
                <tr key={p.policy_id}>
                  <td>
                    <strong>{p.title}</strong>
                    {p.needs_my_attestation ? (
                      <div>
                        <span className="badge badge-warn">Needs your attestation</span>
                      </div>
                    ) : null}
                  </td>
                  <td>{p.version ? `v${p.version}` : '—'}</td>
                  <td>
                    <Status s={p.state} />
                    {p.latest_version_state === 'pending' ? (
                      <span className="badge badge-info">change pending</span>
                    ) : null}
                  </td>
                  <td>
                    {pct(p) === null ? (
                      <span className="muted">{p.mine ? 'you attested' : '—'}</span>
                    ) : (
                      <span
                        className={`badge ${p.overdue ? 'badge-bad' : pct(p) === 100 ? 'badge-ok' : 'badge-warn'}`}
                      >
                        {p.attested} of {p.expected} ({pct(p)}%)
                      </span>
                    )}
                  </td>
                  <td>
                    {p.review_overdue ? (
                      <span className="badge badge-bad">overdue</span>
                    ) : (
                      (p.review_due ?? '—')
                    )}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={() => setOpen(p.policy_id)}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {open !== null ? (
        <PolicyDetailView id={open} canWrite={writer} onClose={() => setOpen(null)} />
      ) : null}
      {writer ? <NewPolicy /> : null}
    </div>
  );
}

// ---------------------------------------------------------------- risks

function heat(score: number): string {
  return score >= 15 ? 'badge-bad' : score >= 8 ? 'badge-warn' : 'badge-ok';
}

function RiskRow({ r }: { r: Risk }) {
  const refresh = useRefresh();
  const move = useMutation({
    mutationFn: (state: string) => patchRisk(r.risk_id, { state }),
    onSuccess: refresh,
  });
  return (
    <tr>
      <td>
        <strong>{r.title}</strong>
        <div className="muted" style={{ fontSize: 12.5 }}>
          {r.category} · owner {r.owner}
          {r.review_overdue ? ' · review overdue' : ''}
        </div>
        {r.mitigation ? <div style={{ fontSize: 12.5 }}>{r.mitigation}</div> : null}
      </td>
      <td>
        <span className={`badge ${heat(r.score)}`}>
          {r.likelihood} × {r.impact} = {r.score}
        </span>
      </td>
      <td>
        {r.can_edit ? (
          <select
            aria-label={`State of ${r.title}`}
            value={r.state}
            disabled={move.isPending}
            onChange={(e) => move.mutate(e.target.value)}
          >
            {['open', 'mitigating', 'accepted', 'closed'].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        ) : (
          <Status s={r.state} />
        )}
      </td>
    </tr>
  );
}

function RisksTab({ session }: { session: Session }) {
  const refresh = useRefresh();
  const list = useQuery({ queryKey: ['risks'], queryFn: getRisks });
  const [title, setTitle] = useState('');
  const [category, setCategory] = useState('operational');
  const [likelihood, setLikelihood] = useState(3);
  const [impact, setImpact] = useState(3);
  const [mitigation, setMitigation] = useState('');
  const lead = ['owner', 'admin', 'manager'].includes(session.role ?? '');
  const create = useMutation({
    mutationFn: () =>
      addRisk({
        title: title.trim(),
        description: '',
        category,
        likelihood,
        impact,
        mitigation: mitigation.trim(),
      }),
    onSuccess: async () => {
      setTitle('');
      setMitigation('');
      await refresh();
    },
  });
  return (
    <div className="stack">
      {list.isPending ? <Loading /> : null}
      {list.isError ? <ErrorNote error={list.error} /> : null}
      {list.data && list.data.risks.length === 0 ? <Empty>No risks recorded.</Empty> : null}
      {list.data && list.data.risks.length > 0 ? (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Risk</th>
                <th scope="col">Likelihood × impact</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {list.data.risks.map((r) => (
                <RiskRow key={r.risk_id} r={r} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {lead ? (
        <form
          className="card card-pad stack"
          aria-label="Add a risk"
          onSubmit={(e) => {
            e.preventDefault();
            if (title.trim().length >= 3) create.mutate();
          }}
        >
          <p className="section-title">Add a risk</p>
          <input
            aria-label="Risk title"
            value={title}
            maxLength={160}
            placeholder="Single cloud region"
            onChange={(e) => setTitle(e.target.value)}
          />
          <div className="field-row">
            <select
              aria-label="Category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              {['security', 'operational', 'compliance', 'financial', 'people', 'vendor'].map(
                (c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ),
              )}
            </select>
            <select
              aria-label="Likelihood"
              value={likelihood}
              onChange={(e) => setLikelihood(Number(e.target.value))}
            >
              {[1, 2, 3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  Likelihood {n}
                </option>
              ))}
            </select>
            <select
              aria-label="Impact"
              value={impact}
              onChange={(e) => setImpact(Number(e.target.value))}
            >
              {[1, 2, 3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  Impact {n}
                </option>
              ))}
            </select>
          </div>
          <input
            aria-label="Mitigation"
            value={mitigation}
            maxLength={2000}
            placeholder="What reduces it"
            onChange={(e) => setMitigation(e.target.value)}
          />
          {create.isError ? <ErrorNote error={create.error} /> : null}
          <div className="row">
            <button type="submit" disabled={create.isPending || title.trim().length < 3}>
              Add risk
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

/** Approvals, decisions proposed in Cairn, policies with attestations, and the risk register. */
export function GovernancePage({ session }: { session: Session }) {
  const role = session.role ?? 'guest';
  const [tab, setTab] = useState<Tab>('approvals');
  const risksVisible = ['owner', 'admin', 'manager', 'auditor'].includes(role);
  return (
    <section aria-labelledby="gov-h" className="stack">
      <PageHead title="Governance" id="gov-h">
        Who approved what, which policies people have accepted, and the risks being tracked. Nobody
        approves their own request.
      </PageHead>
      <Tabs
        label="Governance sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: 'approvals', label: 'Approvals' },
          { id: 'decisions', label: 'Decisions' },
          { id: 'policies', label: 'Policies' },
          ...(risksVisible ? [{ id: 'risks' as const, label: 'Risks' }] : []),
        ]}
      />
      {tab === 'approvals' ? <ApprovalsTab session={session} /> : null}
      {tab === 'decisions' ? <DecisionsTab session={session} /> : null}
      {tab === 'policies' ? <PoliciesTab session={session} /> : null}
      {tab === 'risks' && risksVisible ? <RisksTab session={session} /> : null}
    </section>
  );
}
