import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

describe('navigation of a signed-in workspace', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    window.location.hash = '';
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('offers every area a user needs, grouped, with Home first', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith('/session')) {
        return json({
          authenticated: true,
          account: { id: 'a1', email: 'ana@example.org', name: 'Ana Owner' },
          persona: null,
          org: { org_id: 2, slug: 'acme', name: 'Acme', role: 'owner', is_demo: false },
          role: 'owner',
          orgs: [{ org_id: 2, slug: 'acme', name: 'Acme', role: 'owner', is_demo: false }],
          demo: false,
          principal_id: 5,
        });
      }
      if (url.endsWith('/dashboard')) {
        return json({
          role: 'owner',
          date: '2026-11-19',
          weekend: false,
          my_requests_pending: 0,
          members: 1,
          recent: [],
        });
      }
      return json({}, 404);
    });
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <App />
      </QueryClientProvider>,
    );
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    const labels = Array.from(nav.querySelectorAll('button')).map((b) => b.textContent);
    expect(labels).toEqual([
      'Home',
      'Daily reports',
      'Governance',
      'Ask',
      'Decisions',
      'Review queue',
      'Sources',
      'Compliance',
      'People',
      'Audit trail',
      'Settings',
    ]);
    expect(nav.querySelector('[aria-current="page"]')?.textContent).toBe('Home');
    expect(await screen.findByLabelText('Ask a question')).toBeInTheDocument();
  });
});
