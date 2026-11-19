import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DecisionPage } from './DecisionPage';

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

const page = {
  decision: {
    key: 'ADR-007',
    title: 'Migrate to PostgreSQL',
    question: null,
    outcome: null,
    verification: 'verified',
    origin: 'scenario',
  },
  as_of: { valid: 'now', recorded: 'now' },
  status: 'implemented',
  claims: [
    {
      key: 'C1',
      text: 'Transactions are required.',
      tier: 'T3',
      citations: [{ external_id: 'doc:a', title: 'Design doc', support_set: 0 }],
    },
  ],
  summary: 'Migrate to PostgreSQL. Status: implemented.',
  timeline: [
    {
      event: 'proposed',
      valid_at: '2026-03-03T00:00:00+00:00',
      recorded_at: null,
      by: null,
      note: null,
      source: 's',
    },
  ],
  links: [],
  audit_id: 'x',
};

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DecisionPage />
    </QueryClientProvider>,
  );
}

describe('DecisionPage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('finds a decision and shows status, claims with sources and the timeline', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.startsWith('/api/decisions?q=')
        ? json({
            decisions: [
              {
                key: 'ADR-007',
                title: 'Migrate to PostgreSQL',
                outcome: null,
                verification: 'verified',
              },
            ],
          })
        : json(page),
    );
    wrap();
    fireEvent.change(screen.getByLabelText('Find a decision'), { target: { value: 'postgres' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    fireEvent.click(await screen.findByRole('button', { name: 'ADR-007' }));
    expect(await screen.findByText('Status: implemented')).toBeInTheDocument();
    expect(screen.getByText(/Verification: verified/)).toBeInTheDocument();
    expect(screen.getByText('Design doc')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Timeline' })).toHaveTextContent('proposed');
  });

  it('passes a valid as-of date to the API and ignores a malformed one', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.startsWith('/api/decisions?q=')
        ? json({ decisions: [{ key: 'ADR-007', title: 't', outcome: null, verification: null }] })
        : json(page),
    );
    wrap();
    fireEvent.change(screen.getByLabelText('Find a decision'), { target: { value: 'adr' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    fireEvent.click(await screen.findByRole('button', { name: 'ADR-007' }));
    await screen.findByText('Status: implemented');
    fireEvent.change(screen.getByLabelText(/It held on/), { target: { value: '2026-03-04' } });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/decisions/ADR-007?valid=2026-03-04',
        expect.anything(),
      ),
    );
    fireEvent.change(screen.getByLabelText(/It held on/), { target: { value: 'tomorrow' } });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenLastCalledWith('/api/decisions/ADR-007', expect.anything()),
    );
  });

  it('says so when nothing is visible, and when the decision is hidden', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.startsWith('/api/decisions?q=')
        ? json({ decisions: [] })
        : json({ error: 'not_found' }, 404),
    );
    wrap();
    fireEvent.change(screen.getByLabelText('Find a decision'), { target: { value: 'nothing' } });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('No decisions found that you can see.')).toBeInTheDocument();
  });
});
