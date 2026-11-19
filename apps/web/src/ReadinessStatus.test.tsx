import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AppProviders } from './providers';
import { createQueryClient } from './query-client';
import { ReadinessStatus } from './ReadinessStatus';

const SECRET_BODY = { status: 'ready', extensions: { btree_gist: '9.9.9' }, role: 'cairn_app' };

function renderWidget() {
  return render(
    <AppProviders client={createQueryClient()}>
      <ReadinessStatus />
    </AppProviders>,
  );
}

function stubFetch(impl: () => Promise<Response>) {
  const fetchMock = vi.fn<typeof fetch>(() => impl());
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ReadinessStatus', () => {
  it('shows "Checking" while the request is pending', () => {
    stubFetch(() => new Promise<Response>(() => undefined));
    renderWidget();
    expect(screen.getByRole('status')).toHaveTextContent('Checking');
  });

  it('shows "Ready" for a 200 response and requests /api/readyz', async () => {
    const fetchMock = stubFetch(() => Promise.resolve(Response.json(SECRET_BODY, { status: 200 })));
    renderWidget();
    expect(await screen.findByText('Ready')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/readyz');
  });

  it('shows "Not ready" for a 503 response without showing the body', async () => {
    stubFetch(() =>
      Promise.resolve(Response.json({ ...SECRET_BODY, status: 'not_ready' }, { status: 503 })),
    );
    renderWidget();
    expect(await screen.findByText('Not ready')).toBeInTheDocument();
    const text = screen.getByRole('status').textContent ?? '';
    expect(text).not.toMatch(/btree_gist|9\.9\.9|cairn_app/);
  });

  it('shows "Not ready" for any other non-200 status', async () => {
    stubFetch(() => Promise.resolve(new Response('bad gateway', { status: 502 })));
    renderWidget();
    expect(await screen.findByText('Not ready')).toBeInTheDocument();
    expect(screen.getByRole('status')).not.toHaveTextContent('bad gateway');
  });

  it('shows "Not ready" and "API unreachable" on a network error', async () => {
    stubFetch(() => Promise.reject(new TypeError('Failed to fetch')));
    renderWidget();
    expect(await screen.findByText('Not ready')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('API unreachable');
  });
});
