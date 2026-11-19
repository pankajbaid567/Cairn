import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuditPage } from './AuditPage';
import { SyncPage } from './SyncPage';

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

function wrap(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe('SyncPage and AuditPage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('shows sources with their badge and a notice while permission events are pending', async () => {
    fetchMock.mockReturnValue(
      json({
        sources: [
          {
            source: 'github:o/payment-service',
            badge: 'Delayed',
            last_synced_at: '2026-11-04T10:00:00+00:00',
            oldest_pending_s: 400,
          },
          {
            source: 'import:docs_folder',
            badge: 'Imported',
            last_synced_at: null,
            oldest_pending_s: null,
          },
        ],
        pending_acl: 2,
        pending_content: 1,
        oldest_pending_s: 400,
        last_applied_at: null,
      }),
    );
    wrap(<SyncPage />);
    expect(await screen.findByText('github:o/payment-service')).toBeInTheDocument();
    expect(screen.getByText('Delayed')).toBeInTheDocument();
    expect(screen.getByText('not live')).toBeInTheDocument();
    expect(
      screen.getByText(/Answers pause until the permission events are applied/),
    ).toBeInTheDocument();
  });

  it('says so when there are no readable sources, and when the user is not signed in', async () => {
    fetchMock.mockReturnValue(
      json({
        sources: [],
        pending_acl: 0,
        pending_content: 0,
        oldest_pending_s: null,
        last_applied_at: null,
      }),
    );
    wrap(<SyncPage />);
    expect(await screen.findByText('No sources you can read.')).toBeInTheDocument();
    fetchMock.mockReturnValue(json({ error: 'unauthenticated' }, 401));
    wrap(<AuditPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a persona first.');
  });

  it('lists the audit entries as counts, not content', async () => {
    fetchMock.mockReturnValue(
      json({
        entries: [
          {
            audit_id: 'a1',
            at: '2026-11-04T10:00:00.123+00:00',
            endpoint: 'answer',
            outcome: 'answered',
            returned: 3,
            acl_version: 5,
            model: null,
          },
        ],
      }),
    );
    wrap(<AuditPage />);
    expect(await screen.findByText('answered')).toBeInTheDocument();
    expect(screen.getByText('2026-11-04 10:00:00')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
  });
});
