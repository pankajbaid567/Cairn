import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from './api';
import { CompliancePage } from './CompliancePage';

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

function session(role: string): Session {
  return {
    authenticated: true,
    account: { id: 'a1', email: 'ana@example.org', name: 'Ana' },
    persona: null,
    org: { org_id: 2, slug: 'acme', name: 'Acme', role, is_demo: false },
    role,
    orgs: [],
    demo: false,
    principal_id: 5,
  };
}

function wrap(role: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CompliancePage session={session(role)} />
    </QueryClientProvider>,
  );
}

const review = {
  review_id: 3,
  title: 'Q4 access review',
  state: 'open',
  started_at: '2026-11-19T10:00:00+00:00',
  due_at: '2026-12-03T10:00:00+00:00',
  closed_at: null,
  started_by: 'Owen',
  total: 2,
  decided: 0,
  overdue: false,
  items: [
    {
      item_id: 11,
      member: 'Max',
      role: 'member',
      teams: ['Payments'],
      reviewer: 'Ana',
      decision: 'pending',
      new_role: null,
      comment: null,
      decided_at: null,
      mine: true,
    },
    {
      item_id: 12,
      member: 'Ana',
      role: 'admin',
      teams: [],
      reviewer: 'Owen',
      decision: 'pending',
      new_role: null,
      comment: null,
      decided_at: null,
      mine: false,
    },
  ],
};

describe('CompliancePage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('shows controls with their status in words and the not-a-certification note', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.endsWith('/compliance/controls')
        ? json({
            controls: [
              {
                id: 'POL-1',
                category: 'Policies',
                title: 'Policies are approved and published',
                status: 'pass',
                detail: '2 active policies.',
                route: 'governance',
              },
              {
                id: 'ACC-1',
                category: 'Access',
                title: 'Access is reviewed at least quarterly',
                status: 'no_evidence',
                detail: 'No access review has been completed yet.',
                route: 'compliance',
              },
              {
                id: 'LOG-1',
                category: 'Logging',
                title: 'The audit trail is tamper-evident',
                status: 'fail',
                detail: 'The chain breaks at event 4.',
                route: 'audit',
              },
            ],
            counts: { pass: 1, fail: 1, no_evidence: 1 },
            note: 'These checks are not an audit opinion or a certification.',
          })
        : json({}, 404),
    );
    wrap('auditor');
    expect(await screen.findByText('Passing')).toBeInTheDocument();
    expect(screen.getByText('Failing')).toBeInTheDocument();
    expect(screen.getByText('No evidence yet')).toBeInTheDocument();
    expect(screen.getByText(/not an audit opinion or a certification/)).toBeInTheDocument();
  });

  it('lets only the assigned reviewer decide and sends the decision', async () => {
    let sent: RequestInit | undefined;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes('/decide')) {
        sent = init;
        return json({ status: 'decided' });
      }
      if (url.endsWith('/compliance/reviews')) return json({ reviews: [review] });
      return json({}, 404);
    });
    wrap('manager'); // not an admin: no controls tab, lands on the reviews
    expect(screen.queryByRole('tab', { name: 'Controls' })).toBeNull();
    fireEvent.change(await screen.findByLabelText('Decision for Max'), {
      target: { value: 'remove' },
    });
    fireEvent.change(screen.getByLabelText('Comment for Max'), {
      target: { value: 'left the team' },
    });
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0]!);
    await waitFor(() => expect(sent).toBeDefined());
    expect(JSON.parse(String(sent?.body))).toEqual({
      decision: 'remove',
      comment: 'left the team',
    });
    expect(screen.queryByLabelText('Decision for Ana')).toBeNull();
    expect(screen.getByText('Waiting for Owen')).toBeInTheDocument();
  });

  it('keeps Close review disabled until every item is decided', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.endsWith('/compliance/reviews') ? json({ reviews: [review] }) : json({}, 404),
    );
    wrap('admin');
    fireEvent.click(await screen.findByRole('tab', { name: 'Access reviews' }));
    expect(await screen.findByRole('button', { name: 'Close review' })).toBeDisabled();
    expect(screen.getByText('0 of 2 decided')).toBeInTheDocument();
  });

  it('shows an anchor that no longer matches as a warning', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith('/org/audit/anchors')) {
        return json({
          anchors: [
            {
              seq: 7,
              head_hash: 'a'.repeat(64),
              at: '2026-11-19T10:00:00+00:00',
              status: 'mismatch',
            },
          ],
          ok: false,
          latest_seq: 9,
        });
      }
      if (url.endsWith('/compliance/controls'))
        return json({ controls: [], counts: { pass: 0, fail: 0, no_evidence: 0 }, note: 'n' });
      return json({}, 404);
    });
    wrap('auditor');
    fireEvent.click(await screen.findByRole('tab', { name: 'Evidence and integrity' }));
    expect(await screen.findByText(/An anchor no longer matches the database/)).toBeInTheDocument();
    expect(screen.getByText('mismatch')).toBeInTheDocument();
  });
});
