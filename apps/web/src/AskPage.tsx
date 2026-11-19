import { useMutation } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import { AnswerView } from './AnswerView';
import { describeError, postAnswer } from './api';
import { PersonaSwitcher } from './PersonaSwitcher';

const MAX_QUESTION = 500;

/** Ask a question as the chosen persona and show the cited answer. */
export function AskPage() {
  const id = useId();
  const [question, setQuestion] = useState('');
  const ask = useMutation({ mutationFn: postAnswer, mutationKey: ['answer'] });

  function submit(event: FormEvent) {
    event.preventDefault();
    const trimmed = question.trim();
    if (trimmed) ask.mutate(trimmed);
  }

  return (
    <div>
      <PersonaSwitcher />
      <form onSubmit={submit}>
        <label htmlFor={id}>Your question</label>
        <br />
        <textarea
          id={id}
          value={question}
          maxLength={MAX_QUESTION}
          rows={3}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <br />
        <button type="submit" disabled={ask.isPending || question.trim() === ''}>
          {ask.isPending ? 'Asking…' : 'Ask'}
        </button>
      </form>
      <div aria-live="polite">
        {ask.isPending ? <p>Searching your sources…</p> : null}
        {ask.isError ? <p role="alert">{describeError(ask.error)}</p> : null}
        {ask.isSuccess ? <AnswerView answer={ask.data} /> : null}
      </div>
    </div>
  );
}
