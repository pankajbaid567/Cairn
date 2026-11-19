import { useQuery } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import { describeError, getDecision, searchDecisions, type AsOf } from './api';

const DAY = /^\d{4}-\d{2}-\d{2}$/;

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
    <section aria-labelledby={`${searchId}-h`}>
      <h2 id={`${searchId}-h`}>Decisions</h2>
      <form onSubmit={search}>
        <label htmlFor={searchId}>Find a decision</label>
        <br />
        <input
          id={searchId}
          value={text}
          maxLength={100}
          onChange={(e) => setText(e.target.value)}
        />{' '}
        <button type="submit" disabled={text.trim() === ''}>
          Search
        </button>
      </form>
      {hits.isError ? <p role="alert">{describeError(hits.error)}</p> : null}
      {hits.data ? (
        hits.data.decisions.length === 0 ? (
          <p>No decisions found that you can see.</p>
        ) : (
          <ul aria-label="Decision results">
            {hits.data.decisions.map((hit) => (
              <li key={hit.key}>
                <button type="button" onClick={() => setKey(hit.key)}>
                  {hit.key}
                </button>{' '}
                {hit.title}
              </li>
            ))}
          </ul>
        )
      ) : null}

      {key !== null ? (
        <div>
          <fieldset>
            <legend>As of</legend>
            <label htmlFor={validId}>It held on (YYYY-MM-DD)</label>{' '}
            <input
              id={validId}
              value={valid}
              placeholder="today"
              onChange={(e) => setValid(e.target.value)}
            />
            <br />
            <label htmlFor={recordedId}>As we knew it on (YYYY-MM-DD)</label>{' '}
            <input
              id={recordedId}
              value={recorded}
              placeholder="today"
              onChange={(e) => setRecorded(e.target.value)}
            />
          </fieldset>
          <div aria-live="polite">
            {page.isPending ? <p>Loading…</p> : null}
            {page.isError ? <p role="alert">{describeError(page.error)}</p> : null}
          </div>
        </div>
      ) : null}

      {data ? (
        <article>
          <h3>
            {data.decision.key}: {data.decision.title}
          </h3>
          <p>
            <strong>Status: {data.status}</strong> (as of {data.as_of.valid}, known{' '}
            {data.as_of.recorded}) <span>Verification: {data.decision.verification}</span>
          </p>
          {data.decision.question ? <p>Question: {data.decision.question}</p> : null}
          {data.summary ? <p>{data.summary}</p> : null}
          <h4>Claims</h4>
          {data.claims.length === 0 ? (
            <p>No claims you can see.</p>
          ) : (
            <ul>
              {data.claims.map((claim) => (
                <li key={claim.key}>
                  {claim.text} <em>(tier {claim.tier})</em>
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
          <h4>Timeline</h4>
          <ol aria-label="Timeline">
            {data.timeline.map((e, i) => (
              <li key={`${e.event}-${i}`}>
                {e.valid_at?.slice(0, 10)} {e.event}
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
