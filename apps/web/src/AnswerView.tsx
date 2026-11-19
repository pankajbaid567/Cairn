import { useState, type ReactNode } from 'react';

import type { AnswerResponse } from './api';
import { CopyIcon } from './icons';

const SEARCHED_LABELS: Record<string, string> = {
  chat_export: 'chat export',
  meeting_notes: 'meeting notes',
  docs_folder: 'docs',
};

const DEGRADED_TEXT: Record<string, string> = {
  dense: 'Semantic search was skipped',
  rerank: 'Reranking was skipped',
};

const FRESHNESS_TONE: Record<string, string> = {
  Live: 'badge-ok',
  Imported: 'badge-info',
  Delayed: 'badge-warn',
  Stale: 'badge-bad',
};

/** Splits "text [1][2] more" into text and numbered citation links. */
function withMarkers(text: string, onJump: (n: number) => void): ReactNode[] {
  return text.split(/(\[\d+\])/g).map((part, i) => {
    const m = /^\[(\d+)\]$/.exec(part);
    if (!m) return part;
    const n = Number(m[1]);
    return (
      <a
        key={`${i}-${n}`}
        className="cite-mark"
        href={`#citation-${n}`}
        aria-label={`Source ${n}`}
        onClick={(event) => {
          event.preventDefault();
          onJump(n);
        }}
      >
        {n}
      </a>
    );
  });
}

/** The answer with numbered citation markers, then the citations. Status is always text. */
export function AnswerView({ answer }: { answer: AnswerResponse }) {
  const [active, setActive] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const searched = answer.searched.map((s) => SEARCHED_LABELS[s] ?? s).join(', ');

  function jump(n: number) {
    setActive(n);
    document
      .getElementById(`citation-${n}`)
      ?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(answer.answer);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard not available: nothing to do
    }
  }

  return (
    <section aria-label="Answer" className="stack">
      {answer.degraded.map((d) => (
        <p key={d} role="note" className="notice notice-warn">
          {DEGRADED_TEXT[d] ?? `Stage skipped: ${d}`}
        </p>
      ))}
      <div className="card answer">
        <p className="answer-text">{withMarkers(answer.answer, jump)}</p>
        <div className="answer-meta">
          {answer.abstained ? (
            <>
              <span className="badge badge-warn">No answer</span>
              <span>Searched: {searched}</span>
            </>
          ) : (
            <>
              <span className={`badge ${FRESHNESS_TONE[answer.freshness.weakest_badge] ?? ''}`}>
                Freshness: {answer.freshness.weakest_badge}
              </span>
              <span className="badge badge-plain">
                {answer.citations.length} {answer.citations.length === 1 ? 'source' : 'sources'}
              </span>
              {answer.model.model ? (
                <span className="mono">
                  {answer.model.model}
                  {answer.model.provider ? ` · ${answer.model.provider}` : ''}
                </span>
              ) : null}
              <button
                type="button"
                className="btn-ghost"
                style={{ marginLeft: 'auto' }}
                onClick={() => void copy()}
              >
                <CopyIcon /> {copied ? 'Copied' : 'Copy answer'}
              </button>
            </>
          )}
        </div>
      </div>
      {answer.abstained ? null : (
        <>
          <h2 className="section-title">Citations</h2>
          <ol className="cites">
            {answer.citations.map((c, index) => (
              <li
                key={c.id}
                id={`citation-${index + 1}`}
                className="card cite"
                style={active === index + 1 ? { borderColor: 'var(--accent)' } : undefined}
              >
                <div className="cite-head">
                  <span className="cite-num">{index + 1}</span>
                  <strong>{c.title}</strong>
                  <span className="badge badge-plain">{c.source_kind.replace('_', ' ')}</span>
                  <span className="cite-loc">{c.location}</span>
                </div>
                <blockquote>{c.quote}</blockquote>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
