import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ReviewPage } from './ReviewPage';

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

const conflict = {
  review_id: 7,
  kind: 'conflict',
  decision: 'ADR-015',
  state: 'open',
  reason: null,
  created_at: '2026-11-04T10:00:00+00:00',
  resolved_at: null,
  detail: { subject: 'API v1 deprecation date' },
  sides: [
    { value: '2026-09-30', tier: 'T3', source: 'doc-api-v1-deprecation' },
    { value: '2026-10-31', tier: 'T5', source: 'thread-mobile-eng-2026-07-14' },
  ],
};

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReviewPage />
    </QueryClientProvider>,
  );
}

describe('ReviewPage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('shows both sides of a conflict with tiers and sources and resolves nothing by itself', async () => {
    fetchMock.mockReturnValue(json({ reviews: [conflict] }));
    wrap();
    expect(await screen.findByText('2026-09-30')).toBeInTheDocument();
    expect(screen.getByText('2026-10-31')).toBeInTheDocument();
    expect(screen.getByText('T3')).toBeInTheDocument();
    expect(screen.getByText('T5')).toBeInTheDocument();
    expect(screen.getByText('thread-mobile-eng-2026-07-14')).toBeInTheDocument();
    expect(screen.getByText(/Nothing is resolved automatically/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the list was fetched
  });

  it('needs a reason of at least five characters before it can be saved', async () => {
    fetchMock.mockReturnValue(json({ reviews: [conflict] }));
    wrap();
    const save = await screen.findByRole('button', { name: 'Save decision' });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'ok' } });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'Both dates are used' } });
    expect(save).toBeEnabled();
  });

  it('sends the chosen action and the reason, then reloads the list', async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? json({ review_id: 7, state: 'disputed' })
        : json({ reviews: [conflict] }),
    );
    wrap();
    await screen.findByText('2026-09-30');
    fireEvent.change(screen.getByLabelText('Action'), { target: { value: 'disputed' } });
    fireEvent.change(screen.getByLabelText(/Reason/), {
      target: { value: 'Both dates are in use' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save decision' }));
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(post?.[0]).toBe('/api/reviews/7/resolve');
      expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({
        state: 'disputed',
        reason: 'Both dates are in use',
      });
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter((c) => !(c[1] as RequestInit | undefined)?.method).length,
      ).toBeGreaterThan(1),
    );
  });

  it('says so when there is nothing to review', async () => {
    fetchMock.mockReturnValue(json({ reviews: [] }));
    wrap();
    expect(await screen.findByText('Nothing to review.')).toBeInTheDocument();
  });
});
