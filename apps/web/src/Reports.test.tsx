import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from './api';
import { ReportsPage } from './ReportsPage';
import { isoDay } from './ui-helpers';

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

const report = {
  report_id: 9,
  author_principal: 6,
  author: 'Max',
  date: isoDay(0),
  done: 'Fixed the refund event bug',
  next: 'Write tests',
  blockers: 'Waiting on ledger',
  links: [{ kind: 'decision', ref: 'ADR-007' }],
  updated_at: `${isoDay(0)}T10:00:00+00:00`,
  comments: 0,
};

function wrap(role: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ReportsPage session={session(role)} />
    </QueryClientProvider>,
  );
}

describe('ReportsPage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('submits a report with a link and says it was saved', async () => {
    let put: RequestInit | undefined;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes('/reports/mine'))
        return json({ date: isoDay(0), report: null, editable: true, window_days: 14 });
      if (init?.method === 'PUT') {
        put = init;
        return json({ report_id: 1, created: true }, 201);
      }
      return json({}, 404);
    });
    wrap('member');
    const save = await screen.findByRole('button', { name: 'Submit report' });
    expect(save).toBeDisabled(); // nothing written yet
    fireEvent.change(screen.getByLabelText('What did you get done?'), {
      target: { value: 'Shipped the fix' },
    });
    fireEvent.change(screen.getByLabelText('Link a decision, PR or document'), {
      target: { value: 'ADR-007' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add link' }));
    fireEvent.click(screen.getByRole('button', { name: 'Submit report' }));
    await waitFor(() => expect(put).toBeDefined());
    expect(JSON.parse(String(put?.body))).toMatchObject({
      done: 'Shipped the fix',
      links: [{ kind: 'decision', ref: 'ADR-007' }],
    });
    expect(await screen.findByText('Saved.')).toBeInTheDocument();
  });

  it('refuses an empty report and explains the closed window', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes('/reports/mine')
        ? json({ date: '2020-01-01', report: null, editable: false, window_days: 14 })
        : json({}, 404),
    );
    wrap('member');
    expect(await screen.findByText(/outside the 14-day window/)).toBeInTheDocument();
    expect(screen.getByLabelText('What did you get done?')).toBeDisabled();
  });

  it('shows the team feed with blockers and who wrote it', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.includes('/reports?')
        ? json({
            start: isoDay(-6),
            end: isoDay(0),
            scope: 'yourself and your teammates',
            reports: [report],
          })
        : json({}, 404),
    );
    wrap('auditor'); // an auditor has no "My report" tab, so the feed is the landing tab
    expect(await screen.findByLabelText(`Report by Max on ${isoDay(0)}`)).toBeInTheDocument();
    expect(screen.getByText('Blocked')).toBeInTheDocument();
    expect(screen.getByText(/yourself and your teammates/)).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'My report' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Roll-up' })).toBeNull();
  });

  it('gives a manager the roll-up with who is missing, and a digest that cites its sources', async () => {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes('/reports/mine'))
        return json({ date: isoDay(0), report: null, editable: true, window_days: 14 });
      if (url.includes('/reports/rollup')) {
        return json({
          start: isoDay(-6),
          end: isoDay(0),
          people: 3,
          reports: 2,
          with_blockers: 1,
          coverage: 0.5,
          days: [{ date: isoDay(-1), expected: 3, submitted: 1, missing: ['Mia', 'Zed'] }],
        });
      }
      if (url.endsWith('/reports/digest') && init?.method === 'POST') {
        return json({
          summary: 'The team fixed a refund bug.',
          highlights: [{ text: 'Refund bug fixed', reports: [9] }],
          blockers: [],
          model: 'stub:stub-1',
          cached: false,
          reports: [{ report_id: 9, author: 'Max', date: isoDay(0) }],
        });
      }
      return json({}, 404);
    });
    wrap('manager');
    fireEvent.click(await screen.findByRole('tab', { name: 'Roll-up' }));
    expect(await screen.findByText('50%')).toBeInTheDocument();
    expect(screen.getByText('Mia, Zed')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Generate digest' }));
    expect(await screen.findByText('The team fixed a refund bug.')).toBeInTheDocument();
    expect(screen.getByText(`Max, ${isoDay(0).slice(5)}`)).toBeInTheDocument();
  });
});
