import { useQuery } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import { describeError, getDecision, searchDecisions, type AsOf } from './api';

const DAY = /^\d{4}-\d{2}-\d{2}$/;

const STATUS_TONE: Record<string, string> = {
  implemented: 'badge-ok',
  accepted: 'badge-info',
  proposed: 'badge-plain',
  superseded: 'badge-warn',
  rejected: 'badge-bad',
};

/** Find a decision, then read its status, claims with sources and timeline, optionally as of a date. */
export function DecisionPage() {
  const searchId = useId();
  const validId = useId();
  const recordedId = useId();
  const [text, setText] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [key, setKey] = useState<string | null>(null);
  const [valid, setValid] = useState('');
  const [recorded, setRecorded] = useState('');

  const asOf: AsOf = {
    valid: DAY.test(valid) ? valid : undefined,
    recorded: DAY.test(recorded) ? recorded : undefined,
  };
  const hits = useQuery({
    queryKey: ['decision-search', submitted],
    queryFn: () => searchDecisions(submitted),
    enabled: submitted !== '',
  });
  const page = useQuery({
    queryKey: ['decision', key, asOf.valid ?? 'now', asOf.recorded ?? 'now'],
    queryFn: () => getDecision(key as string, asOf),
    enabled: key !== null,
  });

  function search(event: FormEvent) {
    event.preventDefault();
    setKey(null);
    setSubmitted(text.trim());
  }

  const data = page.data;
  return (
    <section aria-labelledby={`${searchId}-h`} className="stack">
      <div className="page-head">
        <h2 id={`${searchId}-h`}>Decisions</h2>
        <p>
          Find a decision, read the claims behind it with their sources, and see what we knew on any
          date.
        </p>
      </div>
      <form onSubmit={search} className="card card-pad stack">
        <div className="field">
          <label htmlFor={searchId}>Find a decision</label>
          <div className="row">
            <input
              id={searchId}
              value={text}
              maxLength={100}
              placeholder="postgres, kafka, ADR-007…"
              style={{ flex: 1, minWidth: 200 }}
              onChange={(e) => setText(e.target.value)}
            />
            <button type="submit" disabled={text.trim() === ''}>
              Search
            </button>
          </div>
        </div>
      </form>
      {hits.isError ? (
        <p role="alert" className="notice notice-bad">
          {describeError(hits.error)}
        </p>
      ) : null}
      {hits.data ? (
        hits.data.decisions.length === 0 ? (
          <p className="card empty">No decisions found that you can see.</p>
        ) : (
          <ul aria-label="Decision results" className="results">
            {hits.data.decisions.map((hit) => (
              <li key={hit.key} className="result">
                <button type="button" onClick={() => setKey(hit.key)}>
                  {hit.key}
                </button>
                <span>{hit.title}</span>
              </li>
            ))}
          </ul>
        )
      ) : null}

      {key !== null ? (
        <div className="stack">
          <fieldset>
            <legend>As of</legend>
            <div className="field-row">
              <div className="field">
                <label htmlFor={validId}>It held on (YYYY-MM-DD)</label>
                <input
                  id={validId}
                  value={valid}
                  placeholder="today"
                  onChange={(e) => setValid(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor={recordedId}>As we knew it on (YYYY-MM-DD)</label>
                <input
                  id={recordedId}
                  value={recorded}
                  placeholder="today"
                  onChange={(e) => setRecorded(e.target.value)}
                />
              </div>
            </div>
          </fieldset>
          <div aria-live="polite">
            {page.isPending ? <div className="skeleton" style={{ height: 80 }} /> : null}
            {page.isError ? (
              <p role="alert" className="notice notice-bad">
                {describeError(page.error)}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}

      {data ? (
        <article className="card card-pad stack">
          <div className="decision-head">
            <h3>
              {data.decision.key}: {data.decision.title}
            </h3>
            <strong className={`badge ${STATUS_TONE[data.status] ?? ''}`}>
              Status: {data.status}
            </strong>
          </div>
          <p className="muted">
            As of {data.as_of.valid}, known {data.as_of.recorded} ·{' '}
            <span>Verification: {data.decision.verification}</span>
          </p>
          {data.decision.question ? <p>Question: {data.decision.question}</p> : null}
          {data.summary ? <p>{data.summary}</p> : null}
          <h4 className="section-title">Claims</h4>
          {data.claims.length === 0 ? (
            <p className="muted">No claims you can see.</p>
          ) : (
            <ul className="claims">
              {data.claims.map((claim) => (
                <li key={claim.key} className="claim">
                  {claim.text} <em className="badge badge-plain">(tier {claim.tier})</em>
                  {claim.citations.length > 0 ? (
                    <ul aria-label={`Sources for ${claim.key}`}>
                      {claim.citations.map((c) => (
                        <li key={`${claim.key}-${c.support_set}-${c.external_id}`}>{c.title}</li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          <h4 className="section-title">Timeline</h4>
          <ol aria-label="Timeline" className="timeline">
            {data.timeline.map((e, i) => (
              <li key={`${e.event}-${i}`}>
                <span className="mono">{e.valid_at?.slice(0, 10)}</span> {e.event}
                {e.by ? ` by ${e.by}` : ''}
                {e.note ? `: ${e.note}` : ''}
              </li>
            ))}
          </ol>
        </article>
      ) : null}
    </section>
  );
}
