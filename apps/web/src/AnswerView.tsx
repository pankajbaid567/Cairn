import type { AnswerResponse } from './api';

const SEARCHED_LABELS: Record<string, string> = {
  chat_export: 'chat export',
  meeting_notes: 'meeting notes',
  docs_folder: 'docs',
};

const DEGRADED_TEXT: Record<string, string> = {
  dense: 'Semantic search was skipped',
  rerank: 'Reranking was skipped',
};

/** The answer with numbered citation markers, then the citations. Status is always text. */
export function AnswerView({ answer }: { answer: AnswerResponse }) {
  const searched = answer.searched.map((s) => SEARCHED_LABELS[s] ?? s).join(', ');
  return (
    <section aria-label="Answer">
      {answer.degraded.map((d) => (
        <p key={d} role="note">
          {DEGRADED_TEXT[d] ?? `Stage skipped: ${d}`}
        </p>
      ))}
      <p>{answer.answer}</p>
      {answer.abstained ? (
        <p>Searched: {searched}</p>
      ) : (
        <>
          <h2>Citations</h2>
          <ol>
            {answer.citations.map((c, index) => (
              <li key={c.id} id={`citation-${index + 1}`}>
                <strong>{c.title}</strong> ({c.source_kind.replace('_', ' ')}) at {c.location}
                <blockquote>{c.quote}</blockquote>
              </li>
            ))}
          </ol>
          <p>Freshness: {answer.freshness.weakest_badge}</p>
        </>
      )}
    </section>
  );
}
