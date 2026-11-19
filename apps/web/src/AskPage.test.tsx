import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AnswerView } from './AnswerView';
import { ApiError, describeError, type AnswerResponse } from './api';
import { AskPage } from './AskPage';

const base: AnswerResponse = {
  answer: 'PostgreSQL was chosen for transactions [1].',
  claims: [{ text: 'c', citations: ['chunk:x'] }],
  citations: [
    {
      id: 'chunk:x',
      title: 'ADR-007',
      source_kind: 'adr_file',
      location: 'eng-docs/adr/ADR-007.md',
      quote: 'Decision: move to PostgreSQL.',
    },
  ],
  abstained: false,
  searched: ['chat_export', 'meeting_notes', 'docs_folder'],
  degraded: [],
  freshness: { weakest_badge: 'Imported' },
  model: {},
  audit_id: 'a',
};

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json', ...headers },
    }),
  );
}

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AskPage />
    </QueryClientProvider>,
  );
}

describe('AnswerView', () => {
  it('shows the answer, numbered citations and freshness', () => {
    render(<AnswerView answer={base} />);
    expect(screen.getByText(/PostgreSQL was chosen/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Citations' })).toBeInTheDocument();
    expect(screen.getByText('ADR-007')).toBeInTheDocument();
    expect(screen.getByText('Decision: move to PostgreSQL.')).toBeInTheDocument();
    expect(screen.getByText('Freshness: Imported')).toBeInTheDocument();
  });

  it('shows the fixed not-found text and what was searched', () => {
    render(
      <AnswerView
        answer={{
          ...base,
          answer: 'Not found in sources you can access.',
          abstained: true,
          citations: [],
          claims: [],
        }}
      />,
    );
    expect(screen.getByText('Not found in sources you can access.')).toBeInTheDocument();
    expect(screen.getByText('Searched: chat export, meeting notes, docs')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Citations' })).toBeNull();
  });

  it('says in words when a stage was skipped', () => {
    render(<AnswerView answer={{ ...base, degraded: ['dense', 'rerank'] }} />);
    expect(screen.getByText('Semantic search was skipped')).toBeInTheDocument();
    expect(screen.getByText('Reranking was skipped')).toBeInTheDocument();
  });
});

describe('describeError', () => {
  it('maps API failures to fixed text', () => {
    expect(describeError(new ApiError(429, 'rate_limited', 12))).toBe(
      'Too many requests, try again in 12 seconds.',
    );
    expect(describeError(new ApiError(503, 'model_budget_exhausted', 3600))).toContain('00:00 UTC');
    expect(describeError(new ApiError(0, 'unreachable', null))).toContain('waking up');
    expect(describeError(new ApiError(401, 'unauthenticated', null))).toBe(
      'Choose a persona first.',
    );
    expect(describeError(new Error('x'))).toBe('Something went wrong. Try again.');
  });
});

describe('AskPage', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        if (url === '/api/auth/personas')
          return json({
            demo: true,
            personas: [
              { persona: 'asha', display_name: 'Asha' },
              { persona: 'ben', display_name: 'Ben' },
            ],
          });
        if (url === '/api/me') return json({ persona: 'asha', display_name: 'Asha' });
        if (url === '/api/answer') {
          expect(JSON.parse(String(init?.body))).toEqual({ question: 'Why PostgreSQL?' });
          return json(base);
        }
        return json({}, 404);
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it('offers the personas, labels the demo, and shows an answer for a question', async () => {
    wrap();
    expect(
      await screen.findByText('Demo: the personas and all data are synthetic.'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Ask as')).toBeInTheDocument();
    const button = screen.getByRole('button', { name: 'Ask' });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Your question'), {
      target: { value: 'Why PostgreSQL?' },
    });
    fireEvent.click(button);
    expect(await screen.findByText(/PostgreSQL was chosen/)).toBeInTheDocument();
  });

  it('says persona login is not enabled when the API answers 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => json({ error: 'not_found' }, 404)),
    );
    wrap();
    expect(
      await screen.findByText('Persona login is not enabled on this deployment.'),
    ).toBeInTheDocument();
  });

  it('shows rate limits as text', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/auth/personas')
          return json({ demo: false, personas: [{ persona: 'asha', display_name: 'Asha' }] });
        if (url === '/api/me') return json({ persona: 'asha', display_name: 'Asha' });
        return json({ error: 'rate_limited' }, 429, { 'retry-after': '30' });
      }),
    );
    wrap();
    fireEvent.change(await screen.findByLabelText('Your question'), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Too many requests, try again in 30 seconds.',
      ),
    );
  });
});
