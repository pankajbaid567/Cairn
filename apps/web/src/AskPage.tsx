import { useMutation } from '@tanstack/react-query';
import { useId, useState, type FormEvent, type KeyboardEvent } from 'react';

import { AnswerView } from './AnswerView';
import { describeError, postAnswer } from './api';
import { PersonaSwitcher } from './PersonaSwitcher';
import { EXAMPLE_QUESTIONS } from './personas';

const MAX_QUESTION = 500;

/** Ask a question as the chosen persona and show the cited answer. */
export function AskPage({ withPersona = true }: { withPersona?: boolean }) {
  const id = useId();
  const [question, setQuestion] = useState(() => {
    try {
      const carried = sessionStorage.getItem('cairn-prefill') ?? '';
      if (carried) sessionStorage.removeItem('cairn-prefill');
      return carried;
    } catch {
      return '';
    }
  });
  const ask = useMutation({ mutationFn: postAnswer, mutationKey: ['answer'] });

  function run(text: string) {
    const trimmed = text.trim();
    if (trimmed) ask.mutate(trimmed);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    run(question);
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !ask.isPending) {
      event.preventDefault();
      run(question);
    }
  }

  return (
    <div className="stack">
      {withPersona ? <PersonaSwitcher /> : null}
      <form onSubmit={submit} className="card composer">
        <label htmlFor={id} className="muted" style={{ paddingLeft: 8, fontSize: 12.5 }}>
          Your question
        </label>
        <textarea
          id={id}
          value={question}
          maxLength={MAX_QUESTION}
          rows={3}
          placeholder="Why did we choose PostgreSQL for payments? Who approved it?"
          onKeyDown={onKey}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <div className="composer-foot">
          <span className="muted" style={{ fontSize: 12.5 }}>
            {question.length}/{MAX_QUESTION} · Ctrl or ⌘ + Enter to send
          </span>
          <button type="submit" disabled={ask.isPending || question.trim() === ''}>
            {ask.isPending ? 'Asking…' : 'Ask'}
          </button>
        </div>
      </form>

      {!ask.isSuccess && !ask.isPending ? (
        <div className="stack" style={{ gap: 8 }}>
          <p className="section-title">Try one of these</p>
          <div className="chips">
            {EXAMPLE_QUESTIONS.map((q) => (
              <button
                key={q}
                type="button"
                className="chip"
                onClick={() => {
                  setQuestion(q);
                  run(q);
                }}
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <div aria-live="polite" className="stack">
        {ask.isPending ? (
          <div className="card answer stack" aria-busy="true">
            <p className="muted">Searching your sources…</p>
            <div className="skeleton" style={{ width: '92%' }} />
            <div className="skeleton" style={{ width: '78%' }} />
            <div className="skeleton" style={{ width: '60%' }} />
          </div>
        ) : null}
        {ask.isError ? (
          <p role="alert" className="notice notice-bad">
            {describeError(ask.error)}
          </p>
        ) : null}
        {ask.isSuccess ? <AnswerView answer={ask.data} /> : null}
      </div>
    </div>
  );
}
