import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import {
  commentOnReport,
  getMyReport,
  getReport,
  getReportFeed,
  getRollup,
  makeDigest,
  putReport,
  reportsExportUrl,
  type Digest,
  type Report,
  type ReportLink,
  type Session,
} from './api';
import { Empty, ErrorNote, Loading, PageHead, Tabs } from './ui';
import { dayLabel, isoDay } from './ui-helpers';

type Tab = 'mine' | 'feed' | 'rollup';

const LINK_KINDS: ReportLink['kind'][] = ['decision', 'pr', 'doc', 'url'];

function LinkChips({ links }: { links: ReportLink[] }) {
  if (links.length === 0) return null;
  return (
    <ul className="chips" aria-label="Links">
      {links.map((l, i) => (
        <li key={`${l.kind}-${l.ref}-${i}`} className="badge badge-plain">
          {l.kind}: {l.title ?? l.ref}
        </li>
      ))}
    </ul>
  );
}

function ReportForm({ date }: { date: string }) {
  const client = useQueryClient();
  const ids = [useId(), useId(), useId(), useId()];
  const mine = useQuery({ queryKey: ['report-mine', date], queryFn: () => getMyReport(date) });
  const [draft, setDraft] = useState<{
    done: string;
    next: string;
    blockers: string;
    links: ReportLink[];
  } | null>(null);
  const [kind, setKind] = useState<ReportLink['kind']>('decision');
  const [ref, setRef] = useState('');
  const saved = mine.data?.report;
  const form = draft ?? {
    done: saved?.done ?? '',
    next: saved?.next ?? '',
    blockers: saved?.blockers ?? '',
    links: saved?.links ?? [],
  };
  const save = useMutation({
    mutationFn: () => putReport(date, form),
    onSuccess: async () => {
      setDraft(null);
      await client.invalidateQueries({ queryKey: ['report-mine'] });
      await client.invalidateQueries({ queryKey: ['report-feed'] });
    },
  });
  const edit = (patch: Partial<typeof form>) => setDraft({ ...form, ...patch });

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }
  if (mine.isPending) return <Loading height={160} />;
  if (mine.isError) return <ErrorNote error={mine.error} />;
  const editable = mine.data.editable;
  return (
    <form onSubmit={submit} className="card card-pad stack" aria-label="Daily report">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <strong>{dayLabel(date)}</strong>
        {saved ? (
          <span className="badge badge-ok">
            Submitted · edited {saved.updated_at.slice(11, 16)} UTC
          </span>
        ) : (
          <span className="badge badge-warn">Not submitted</span>
        )}
      </div>
      <div className="field">
        <label htmlFor={ids[0]}>What did you get done?</label>
        <textarea
          id={ids[0]}
          rows={4}
          value={form.done}
          maxLength={4000}
          disabled={!editable}
          placeholder="Shipped the refund-event change, reviewed two PRs…"
          onChange={(e) => edit({ done: e.target.value })}
        />
      </div>
      <div className="field">
        <label htmlFor={ids[1]}>What is next?</label>
        <textarea
          id={ids[1]}
          rows={2}
          value={form.next}
          maxLength={2000}
          disabled={!editable}
          onChange={(e) => edit({ next: e.target.value })}
        />
      </div>
      <div className="field">
        <label htmlFor={ids[2]}>Anything blocking you?</label>
        <textarea
          id={ids[2]}
          rows={2}
          value={form.blockers}
          maxLength={2000}
          disabled={!editable}
          placeholder="Leave empty if nothing."
          onChange={(e) => edit({ blockers: e.target.value })}
        />
      </div>
      <div className="field">
        <label htmlFor={ids[3]}>Link a decision, PR or document</label>
        <div className="row">
          <select
            aria-label="Link type"
            value={kind}
            disabled={!editable}
            onChange={(e) => setKind(e.target.value as ReportLink['kind'])}
          >
            {LINK_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          <input
            id={ids[3]}
            value={ref}
            disabled={!editable}
            placeholder="ADR-007, #123, https://…"
            style={{ flex: 1, minWidth: 180 }}
            onChange={(e) => setRef(e.target.value)}
          />
          <button
            type="button"
            className="btn-ghost"
            disabled={!editable || ref.trim() === '' || form.links.length >= 20}
            onClick={() => {
              edit({ links: [...form.links, { kind, ref: ref.trim() }] });
              setRef('');
            }}
          >
            Add link
          </button>
        </div>
        <LinkChips links={form.links} />
      </div>
      {save.isError ? <ErrorNote error={save.error} /> : null}
      {save.isSuccess && draft === null ? (
        <p className="notice" role="status">
          Saved.
        </p>
      ) : null}
      {!editable ? (
        <p className="muted">This day is outside the 14-day window and cannot be edited.</p>
      ) : null}
      <div className="row">
        <button
          type="submit"
          disabled={
            !editable || save.isPending || (form.done.trim() === '' && form.blockers.trim() === '')
          }
        >
          {save.isPending ? 'Saving…' : saved ? 'Update report' : 'Submit report'}
        </button>
      </div>
    </form>
  );
}

function ReportCard({ r }: { r: Report }) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const detail = useQuery({
    queryKey: ['report', r.report_id],
    queryFn: () => getReport(r.report_id),
    enabled: open,
  });
  const add = useMutation({
    mutationFn: () => commentOnReport(r.report_id, text.trim()),
    onSuccess: async () => {
      setText('');
      await client.invalidateQueries({ queryKey: ['report', r.report_id] });
      await client.invalidateQueries({ queryKey: ['report-feed'] });
    },
  });
  return (
    <article className="card card-pad stack" aria-label={`Report by ${r.author} on ${r.date}`}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="row">
          <span className="avatar" aria-hidden="true">
            {r.author.slice(0, 1).toUpperCase()}
          </span>
          <strong>{r.author}</strong>
          <span className="muted">{dayLabel(r.date)}</span>
        </div>
        {r.blockers ? <span className="badge badge-bad">Blocked</span> : null}
      </div>
      <p style={{ whiteSpace: 'pre-wrap' }}>{r.done}</p>
      {r.next ? (
        <p className="muted" style={{ whiteSpace: 'pre-wrap' }}>
          <strong>Next:</strong> {r.next}
        </p>
      ) : null}
      {r.blockers ? (
        <p className="notice notice-warn" style={{ whiteSpace: 'pre-wrap' }}>
          <strong>Blockers:</strong> {r.blockers}
        </p>
      ) : null}
      <LinkChips links={r.links} />
      <div>
        <button type="button" className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
          {r.comments > 0 ? `${r.comments} comment${r.comments === 1 ? '' : 's'}` : 'Comment'}
        </button>
      </div>
      {open ? (
        <div className="stack" style={{ gap: 8 }}>
          {detail.data?.comments.map((c) => (
            <p key={c.comment_id} className="claim">
              <strong>{c.author}</strong>{' '}
              <span className="muted">{c.at.slice(0, 16).replace('T', ' ')}</span>
              <br />
              {c.body}
            </p>
          ))}
          <form
            className="row"
            aria-label={`Comment on ${r.author}'s report`}
            onSubmit={(e) => {
              e.preventDefault();
              if (text.trim()) add.mutate();
            }}
          >
            <input
              value={text}
              maxLength={1000}
              placeholder="Write a comment"
              aria-label="Comment"
              style={{ flex: 1 }}
              onChange={(e) => setText(e.target.value)}
            />
            <button type="submit" disabled={add.isPending || text.trim() === ''}>
              Post
            </button>
          </form>
          {add.isError ? <ErrorNote error={add.error} /> : null}
        </div>
      ) : null}
    </article>
  );
}

function Feed({ days }: { days: number }) {
  const end = isoDay(0);
  const start = isoDay(-(days - 1));
  const feed = useQuery({
    queryKey: ['report-feed', start, end],
    queryFn: () => getReportFeed(start, end),
  });
  if (feed.isPending) return <Loading height={160} />;
  if (feed.isError) return <ErrorNote error={feed.error} />;
  if (feed.data.reports.length === 0) return <Empty>No reports in this period yet.</Empty>;
  return (
    <div className="stack">
      <p className="muted">Showing {feed.data.scope}.</p>
      {feed.data.reports.map((r) => (
        <ReportCard key={r.report_id} r={r} />
      ))}
    </div>
  );
}

function DigestView({ d }: { d: Digest }) {
  const label = new Map(d.reports.map((r) => [r.report_id, `${r.author}, ${r.date.slice(5)}`]));
  const items = (title: string, list: Digest['highlights']) =>
    list.length > 0 ? (
      <div className="stack" style={{ gap: 8 }}>
        <p className="section-title">{title}</p>
        <ul className="claims">
          {list.map((h, i) => (
            <li key={i} className="claim">
              {h.text}
              <ul aria-label="Sources">
                {h.reports.map((id) => (
                  <li key={id}>{label.get(id) ?? `report ${id}`}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </div>
    ) : null;
  return (
    <div className="card card-pad stack" aria-label="Digest">
      {d.empty ? (
        <p className="muted">No reports in that period.</p>
      ) : (
        <p style={{ fontSize: 15.5 }}>{d.summary}</p>
      )}
      {items('Highlights', d.highlights)}
      {items('Blockers', d.blockers)}
      {d.model ? (
        <p className="muted mono">
          {d.model}
          {d.cached ? ' · cached' : ''} · every item cites the reports it comes from
        </p>
      ) : null}
    </div>
  );
}

function Rollup({ admin }: { admin: boolean }) {
  const [days, setDays] = useState(7);
  const [scope, setScope] = useState('team');
  const end = isoDay(0);
  const start = isoDay(-(days - 1));
  const roll = useQuery({
    queryKey: ['report-rollup', start, end],
    queryFn: () => getRollup(start, end),
  });
  const digest = useMutation({ mutationFn: () => makeDigest({ start, end, scope }) });
  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="row">
          <label htmlFor="rollup-days">Period</label>
          <select id="rollup-days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
          </select>
        </div>
        <a
          className="btn-ghost"
          href={reportsExportUrl(start, end)}
          download
          style={{ textDecoration: 'none' }}
        >
          Export CSV
        </a>
      </div>
      {roll.isPending ? <Loading /> : null}
      {roll.isError ? <ErrorNote error={roll.error} /> : null}
      {roll.data ? (
        <>
          <div className="tiles">
            <div className="card tile">
              <div className="tile-num">
                {roll.data.coverage === null ? '—' : `${Math.round(roll.data.coverage * 100)}%`}
              </div>
              <div className="tile-label">of expected reports submitted</div>
            </div>
            <div className="card tile">
              <div className="tile-num">{roll.data.reports}</div>
              <div className="tile-label">reports in the period</div>
            </div>
            <div className="card tile">
              <div className="tile-num">{roll.data.with_blockers}</div>
              <div className="tile-label">mention a blocker</div>
            </div>
            <div className="card tile">
              <div className="tile-num">{roll.data.people}</div>
              <div className="tile-label">people covered</div>
            </div>
          </div>
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Submitted</th>
                  <th scope="col">Missing</th>
                </tr>
              </thead>
              <tbody>
                {[...roll.data.days].reverse().map((d) => (
                  <tr key={d.date}>
                    <td>{dayLabel(d.date)}</td>
                    <td>
                      <span
                        className={`badge ${d.submitted === d.expected ? 'badge-ok' : 'badge-warn'}`}
                      >
                        {d.submitted} of {d.expected}
                      </span>
                    </td>
                    <td>
                      {d.missing.length ? (
                        d.missing.join(', ')
                      ) : (
                        <span className="muted">{d.date === end ? 'day in progress' : 'none'}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
      <div className="card card-pad stack">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div>
            <strong>AI digest</strong>
            <p className="muted" style={{ fontSize: 12.5 }}>
              A short summary of the period, written only from reports you may read. Each point
              cites its reports.
            </p>
          </div>
          <div className="row">
            {admin ? (
              <select
                aria-label="Digest scope"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
              >
                <option value="team">Reports I can read</option>
                <option value="org">Whole workspace</option>
              </select>
            ) : null}
            <button type="button" disabled={digest.isPending} onClick={() => digest.mutate()}>
              {digest.isPending ? 'Writing…' : 'Generate digest'}
            </button>
          </div>
        </div>
        {digest.isError ? <ErrorNote error={digest.error} /> : null}
        {digest.data ? <DigestView d={digest.data} /> : null}
      </div>
    </div>
  );
}

/** Daily work reports: write yours, read the team's, and roll it up for managers. */
export function ReportsPage({ session }: { session: Session }) {
  const role = session.role ?? 'guest';
  const writer = ['owner', 'admin', 'manager', 'member'].includes(role);
  const lead = ['owner', 'admin', 'manager'].includes(role);
  const [tab, setTab] = useState<Tab>(writer ? 'mine' : 'feed');
  const [date, setDate] = useState(isoDay(0));
  const dateId = useId();
  return (
    <section aria-labelledby="reports-h" className="stack">
      <PageHead title="Daily reports" id="reports-h">
        What each person got done, in one place, so nobody has to ask.
      </PageHead>
      <Tabs
        label="Report sections"
        value={tab}
        onChange={setTab}
        tabs={[
          ...(writer ? [{ id: 'mine' as const, label: 'My report' }] : []),
          { id: 'feed', label: role === 'owner' || role === 'admin' ? 'Everyone' : 'Team feed' },
          ...(lead ? [{ id: 'rollup' as const, label: 'Roll-up' }] : []),
        ]}
      />
      {tab === 'mine' ? (
        <div className="stack">
          <div className="row">
            <label htmlFor={dateId}>Day</label>
            <input
              id={dateId}
              type="date"
              value={date}
              min={isoDay(-14)}
              max={isoDay(0)}
              onChange={(e) => e.target.value && setDate(e.target.value)}
            />
          </div>
          <ReportForm date={date} />
        </div>
      ) : null}
      {tab === 'feed' ? <Feed days={7} /> : null}
      {tab === 'rollup' && lead ? <Rollup admin={role === 'owner' || role === 'admin'} /> : null}
    </section>
  );
}
