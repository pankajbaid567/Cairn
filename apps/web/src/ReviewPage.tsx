import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import {
  describeError,
  getReviews,
  resolveReview,
  type ReviewAction,
  type ReviewItem,
} from './api';

const LABEL: Record<ReviewAction, string> = {
  verified: 'Verify',
  edited: 'Edit and verify',
  disputed: 'Dispute',
  rejected: 'Reject',
};

function ReviewCard({ item }: { item: ReviewItem }) {
  const id = useId();
  const client = useQueryClient();
  const [action, setAction] = useState<ReviewAction>('verified');
  const [reason, setReason] = useState('');
  const [outcome, setOutcome] = useState('');
  const done = useMutation({
    mutationFn: () =>
      resolveReview(item.review_id, {
        state: action,
        reason: reason.trim(),
        ...(action === 'edited' && outcome.trim() ? { edit: { outcome: outcome.trim() } } : {}),
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['reviews'] }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (reason.trim().length >= 5) done.mutate();
  }

  return (
    <li>
      <h3>
        {item.kind === 'conflict' ? 'Conflict' : item.kind}: {item.detail.subject ?? item.decision}
        {item.decision ? ` (${item.decision})` : ''}
      </h3>
      {item.sides ? (
        <table>
          <caption>Evidence on each side. Nothing is resolved automatically.</caption>
          <thead>
            <tr>
              <th scope="col">Says</th>
              <th scope="col">Source tier</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {item.sides.map((s) => (
              <tr key={`${s.value}-${s.source}`}>
                <td>{s.value}</td>
                <td>{s.tier}</td>
                <td>{s.source ?? 'unknown'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {item.state === 'open' ? (
        <form onSubmit={submit} aria-label={`Resolve review ${item.review_id}`}>
          <label htmlFor={`${id}-a`}>Action</label>{' '}
          <select
            id={`${id}-a`}
            value={action}
            onChange={(e) => setAction(e.target.value as ReviewAction)}
          >
            {(Object.keys(LABEL) as ReviewAction[]).map((a) => (
              <option key={a} value={a}>
                {LABEL[a]}
              </option>
            ))}
          </select>
          {action === 'edited' ? (
            <p>
              <label htmlFor={`${id}-o`}>Corrected outcome</label>{' '}
              <input
                id={`${id}-o`}
                value={outcome}
                maxLength={1000}
                onChange={(e) => setOutcome(e.target.value)}
              />
            </p>
          ) : null}
          <p>
            <label htmlFor={`${id}-r`}>Reason (required, at least 5 characters)</label>{' '}
            <input
              id={`${id}-r`}
              value={reason}
              maxLength={500}
              onChange={(e) => setReason(e.target.value)}
            />
          </p>
          <button type="submit" disabled={done.isPending || reason.trim().length < 5}>
            Save decision
          </button>
          {done.isError ? <p role="alert">{describeError(done.error)}</p> : null}
        </form>
      ) : (
        <p>
          {item.state}: {item.reason}
        </p>
      )}
    </li>
  );
}

/** Open review items the signed-in persona can see: conflicts, candidates and disputes. */
export function ReviewPage() {
  const reviews = useQuery({ queryKey: ['reviews'], queryFn: () => getReviews('open') });
  return (
    <section aria-labelledby="review-h">
      <h2 id="review-h">Review queue</h2>
      {reviews.isPending ? <p>Loading…</p> : null}
      {reviews.isError ? <p role="alert">{describeError(reviews.error)}</p> : null}
      {reviews.data ? (
        reviews.data.reviews.length === 0 ? (
          <p>Nothing to review.</p>
        ) : (
          <ul aria-label="Open review items">
            {reviews.data.reviews.map((r) => (
              <ReviewCard key={r.review_id} item={r} />
            ))}
          </ul>
        )
      ) : null}
    </section>
  );
}
