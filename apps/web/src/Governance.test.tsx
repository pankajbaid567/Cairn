import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Approval, Session } from './api';
import { GovernancePage } from './GovernancePage';

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

const base: Approval = {
  approval_id: 1,
  kind: 'decision',
  title: 'Adopt PostgreSQL',
  detail: {},
  requested_by: 'Mem',
  mine: false,
  required: 1,
  state: 'pending',
  approvals: 0,
  created_at: '2026-11-19T10:00:00+00:00',
  decided_at: null,
  can_vote: true,
  voted: false,
};

function wrap(role: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GovernancePage session={session(role)} />
    </QueryClientProvider>,
  );
}

describe('GovernancePage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('lets a manager approve someone else’s request and sends the comment', async () => {
    let sent: RequestInit | undefined;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes('/governance/approvals/1/vote')) {
        sent = init;
        return json({ state: 'approved' });
      }
      if (url.includes('/governance/approvals')) return json({ approvals: [base] });
      return json({}, 404);
    });
    wrap('manager');
    fireEvent.change(await screen.findByLabelText('Comment'), { target: { value: 'agreed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(sent).toBeDefined());
    expect(JSON.parse(String(sent?.body))).toEqual({ vote: 'approve', comment: 'agreed' });
  });

  it('shows no vote buttons on your own request and says why', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes('/governance/approvals')
        ? json({ approvals: [{ ...base, mine: true, can_vote: false, requested_by: 'Ana' }] })
        : json({}, 404),
    );
    wrap('manager');
    expect(
      await screen.findByText(/You made this request, so someone else has to decide it/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Withdraw request' })).toBeInTheDocument();
  });

  it('shows the reason the database gave when a vote is refused', async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === 'POST')
        return json(
          {
            error: 'forbidden',
            reason: 'segregation of duties: nobody votes on their own request',
          },
          403,
        );
      return url.includes('/governance/approvals') ? json({ approvals: [base] }) : json({}, 404);
    });
    wrap('manager');
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('segregation of duties');
  });

  it('lets a member propose a decision but not see the risk register tab', async () => {
    let sent: RequestInit | undefined;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith('/governance/decisions') && init?.method === 'POST') {
        sent = init;
        return json({ decision_id: 1, key: 'DR-001', approval_id: 3 }, 201);
      }
      if (url.endsWith('/governance/decisions')) return json({ decisions: [] });
      return json({ approvals: [] });
    });
    wrap('member');
    expect(screen.queryByRole('tab', { name: 'Risks' })).toBeNull();
    fireEvent.click(await screen.findByRole('tab', { name: 'Decisions' }));
    fireEvent.change(await screen.findByLabelText('Title'), {
      target: { value: 'Adopt PostgreSQL 18' },
    });
    fireEvent.change(screen.getByLabelText('Decision and reasons'), {
      target: { value: 'Transactions and pgvector' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Submit for approval' }));
    await waitFor(() => expect(sent).toBeDefined());
    expect(JSON.parse(String(sent?.body))).toMatchObject({
      title: 'Adopt PostgreSQL 18',
      required: 1,
    });
  });

  it('keeps Attest disabled until the policy has been read', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith('/governance/policies')) {
        return json({
          policies: [
            {
              policy_id: 4,
              slug: 'access',
              title: 'Access Control',
              state: 'active',
              version: 1,
              published_at: '2026-11-01T00:00:00+00:00',
              attest_by: '2026-12-01T00:00:00+00:00',
              review_due: '2027-11-01',
              review_overdue: false,
              attested: null,
              expected: 5,
              mine: false,
              needs_my_attestation: true,
              overdue: false,
              latest_version_state: 'published',
            },
          ],
        });
      }
      if (url.endsWith('/governance/policies/4')) {
        return json({
          policy: {
            policy_id: 4,
            slug: 'access',
            title: 'Access Control',
            state: 'active',
            version: 1,
          },
          can_edit: false,
          versions: [
            {
              version: 1,
              title: 'Access Control',
              body: 'Access is least-privilege.',
              summary: null,
              state: 'published',
              published_at: '2026-11-01T00:00:00+00:00',
              attest_by: '2026-12-01T00:00:00+00:00',
              author: 'Adm',
              approval_id: 2,
            },
          ],
          attested: [],
          missing: [],
        });
      }
      return json({ approvals: [] });
    });
    wrap('member');
    fireEvent.click(await screen.findByRole('tab', { name: 'Policies' }));
    expect(await screen.findByText('Needs your attestation')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open' }));
    const attest = await screen.findByRole('button', { name: 'Attest' });
    expect(attest).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/I have read and accept this policy/));
    expect(attest).toBeEnabled();
  });
});
