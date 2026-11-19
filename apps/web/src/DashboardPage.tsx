import { useQuery } from '@tanstack/react-query';
import { useState, type FormEvent, type ReactNode } from 'react';

import { getDashboard, type Dashboard, type Session } from './api';
import { ErrorNote, Loading, PageHead } from './ui';
import { when } from './ui-helpers';

function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

function Card({
  title,
  value,
  hint,
  href,
  action,
  tone = 'neutral',
}: {
  title: string;
  value: ReactNode;
  hint: string;
  href: string;
  action: string;
  tone?: 'neutral' | 'warn' | 'ok' | 'bad';
}) {
  const color =
    tone === 'warn'
      ? 'var(--warn)'
      : tone === 'ok'
        ? 'var(--ok)'
        : tone === 'bad'
          ? 'var(--bad)'
          : undefined;
  return (
    <div className="card tile stack" style={{ gap: 6 }}>
      <div className="tile-label">{title}</div>
      <div className="tile-num" style={color ? { color } : undefined}>
        {value}
      </div>
      <div className="muted" style={{ fontSize: 12.5 }}>
        {hint}
      </div>
      <a href={href} className="link" style={{ fontSize: 13 }}>
        {action}
      </a>
    </div>
  );
}

function Attention({ d }: { d: Dashboard }) {
  const cards: ReactNode[] = [];
  if (d.my_report_today !== undefined) {
    cards.push(
      <Card
        key="report"
        title="Your daily report"
        value={d.my_report_today ? 'Done' : d.weekend ? 'Weekend' : 'Not yet'}
        tone={d.my_report_today ? 'ok' : d.weekend ? 'neutral' : 'warn'}
        hint={
          d.my_report_today
            ? 'Submitted for today.'
            : d.weekend
              ? 'No report needed today.'
              : 'Takes two minutes.'
        }
        href="#/reports"
        action={d.my_report_today ? 'Edit it' : 'Write it'}
      />,
    );
  }
  if (d.approvals_waiting !== undefined) {
    cards.push(
      <Card
        key="appr"
        title="Waiting for your decision"
        value={d.approvals_waiting}
        tone={d.approvals_waiting > 0 ? 'warn' : 'ok'}
        hint={d.approvals_waiting > 0 ? 'Decisions, policies and requests.' : 'All clear.'}
        href="#/governance"
        action="Open approvals"
      />,
    );
  }
  if (d.policies_to_attest !== undefined) {
    cards.push(
      <Card
        key="pol"
        title="Policies to attest"
        value={d.policies_to_attest}
        tone={d.policies_to_attest > 0 ? 'warn' : 'ok'}
        hint={d.policies_to_attest > 0 ? 'Read and accept them.' : 'You are up to date.'}
        href="#/governance"
        action="Open policies"
      />,
    );
  }
  if (d.review_items_mine !== undefined) {
    cards.push(
      <Card
        key="rev"
        title="Access to review"
        value={d.review_items_mine}
        tone={d.review_items_mine > 0 ? 'warn' : 'ok'}
        hint={d.review_items_mine > 0 ? 'People assigned to you.' : 'Nothing assigned.'}
        href="#/compliance"
        action="Open reviews"
      />,
    );
  }
  cards.push(
    <Card
      key="mine"
      title="Your open requests"
      value={d.my_requests_pending}
      hint="Waiting for someone else to decide."
      href="#/governance"
      action="See requests"
    />,
  );
  return (
    <div className="tiles" aria-label="Needs your attention">
      {cards}
    </div>
  );
}

function Snapshot({ d }: { d: Dashboard }) {
  const parts: ReactNode[] = [];
  if (d.team_today) {
    const t = d.team_today;
    const pct = t.expected ? Math.round((t.submitted / t.expected) * 100) : 0;
    parts.push(
      <div key="team" className="card card-pad stack">
        <p className="section-title">Team today</p>
        <p style={{ fontSize: 22, fontWeight: 700 }}>
          {t.submitted} of {t.expected}{' '}
          <span className="muted" style={{ fontSize: 14, fontWeight: 400 }}>
            reports in
          </span>
        </p>
        <div className="bar" role="img" aria-label={`${pct}% of expected reports submitted`}>
          <span style={{ width: `${pct}%` }} />
        </div>
        <p className="muted">
          {t.blockers_recent} {t.blockers_recent === 1 ? 'report mentions' : 'reports mention'} a
          blocker in the last 3 days.
        </p>
        <a href="#/reports" className="link">
          Open the roll-up
        </a>
      </div>,
    );
  }
  if (d.audit && d.controls) {
    parts.push(
      <div key="comp" className="card card-pad stack">
        <p className="section-title">Audit and compliance</p>
        <div className="row">
          <span className={`badge ${d.audit.chain_ok ? 'badge-ok' : 'badge-bad'}`}>
            {d.audit.chain_ok ? 'Audit chain intact' : 'Audit chain broken'}
          </span>
          <span className="badge badge-plain">{d.audit.events} events</span>
          {d.audit.denied_7d > 0 ? (
            <span className="badge badge-warn">{d.audit.denied_7d} refused this week</span>
          ) : null}
        </div>
        <p>
          Controls: <strong>{d.controls.pass}</strong> passing, <strong>{d.controls.fail}</strong>{' '}
          failing, {d.controls.no_evidence} without evidence yet.
        </p>
        <a href="#/compliance" className="link">
          Open compliance
        </a>
      </div>,
    );
  }
  if (d.risks) {
    parts.push(
      <div key="risk" className="card card-pad stack">
        <p className="section-title">Risks</p>
        <p style={{ fontSize: 22, fontWeight: 700 }}>
          {d.risks.open}{' '}
          <span className="muted" style={{ fontSize: 14, fontWeight: 400 }}>
            open
          </span>
          {d.risks.high > 0 ? (
            <span className="badge badge-bad" style={{ marginLeft: 10 }}>
              {d.risks.high} high
            </span>
          ) : null}
        </p>
        <a href="#/governance" className="link">
          Open the register
        </a>
      </div>,
    );
  }
  if (parts.length === 0) return null;
  return <div className="snap-grid">{parts}</div>;
}

/** The home screen: what needs this person today, and a way straight into a question. */
export function DashboardPage({ session }: { session: Session }) {
  const dash = useQuery({
    queryKey: ['dashboard'],
    queryFn: getDashboard,
    refetchInterval: 60_000,
  });
  const [q, setQ] = useState('');
  const first = (session.account?.name ?? session.persona ?? '').split(' ')[0] ?? '';

  function ask(event: FormEvent) {
    event.preventDefault();
    if (!q.trim()) return;
    try {
      sessionStorage.setItem('cairn-prefill', q.trim());
    } catch {
      // storage unavailable: the question is simply not carried over
    }
    window.location.hash = '/ask';
  }

  return (
    <section aria-labelledby="home-h" className="stack">
      <PageHead title={`${greeting()}${first ? `, ${first}` : ''}`} id="home-h">
        {session.org?.name} ·{' '}
        {dash.data
          ? new Date(`${dash.data.date}T00:00:00Z`).toLocaleDateString('en-GB', {
              weekday: 'long',
              day: 'numeric',
              month: 'long',
              timeZone: 'UTC',
            })
          : ''}
      </PageHead>
      <form onSubmit={ask} className="card composer" aria-label="Ask a question">
        <label htmlFor="home-q" className="sr-only">
          Ask your engineering history
        </label>
        <div className="row" style={{ gap: 10 }}>
          <input
            id="home-q"
            value={q}
            maxLength={500}
            placeholder="Ask why something was decided, who approved it, what changed…"
            style={{ flex: 1, minWidth: 220, border: 0, boxShadow: 'none', fontSize: 15 }}
            onChange={(e) => setQ(e.target.value)}
          />
          <button type="submit" disabled={!q.trim()}>
            Ask
          </button>
        </div>
      </form>
      {dash.isPending ? <Loading height={140} /> : null}
      {dash.isError ? <ErrorNote error={dash.error} /> : null}
      {dash.data ? (
        <>
          <Attention d={dash.data} />
          <Snapshot d={dash.data} />
          <div className="card card-pad stack">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <p className="section-title">Recent activity</p>
              <a href="#/audit" className="link">
                Audit trail
              </a>
            </div>
            {dash.data.recent.length === 0 ? <p className="muted">Nothing yet.</p> : null}
            <ul className="activity">
              {dash.data.recent.map((e) => (
                <li key={e.seq}>
                  <span className="mono muted">{when(e.at).slice(5)}</span>
                  <span>
                    <strong>{e.actor ?? 'System'}</strong> <span className="mono">{e.action}</span>
                  </span>
                  {e.outcome !== 'ok' ? <span className="badge badge-bad">{e.outcome}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        </>
      ) : null}
    </section>
  );
}
