import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';
import { PeoplePage } from './PeoplePage';
import type { Session } from './api';

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
}

function session(role: string, org = true): Session {
  return {
    authenticated: true,
    account: { id: 'a1', email: 'ana@example.org', name: 'Ana' },
    persona: null,
    org: org ? { org_id: 2, slug: 'acme', name: 'Acme', role, is_demo: false } : null,
    role: org ? role : null,
    orgs: org ? [{ org_id: 2, slug: 'acme', name: 'Acme', role, is_demo: false }] : [],
    demo: false,
    principal_id: 5,
  };
}

const members = {
  members: [
    {
      account_id: 'a1',
      principal_id: 5,
      email: 'ana@example.org',
      name: 'Ana',
      role: 'owner',
      state: 'active',
      title: null,
      joined_at: '2026-11-01T10:00:00+00:00',
      team_ids: [],
      you: true,
    },
    {
      account_id: 'a2',
      principal_id: 6,
      email: 'bo@example.org',
      name: 'Bo',
      role: 'member',
      state: 'active',
      title: null,
      joined_at: '2026-11-02T10:00:00+00:00',
      team_ids: [],
      you: false,
    },
    {
      account_id: 'a3',
      principal_id: 7,
      email: 'cy@example.org',
      name: 'Cy',
      role: 'admin',
      state: 'active',
      title: null,
      joined_at: '2026-11-02T11:00:00+00:00',
      team_ids: [],
      you: false,
    },
  ],
};

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe('signed-out app', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    window.location.hash = '';
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it('shows the sign-in form when nobody is signed in and keeps the brand heading', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith('/session')) return json({ error: 'unauthenticated' }, 401);
      if (url.endsWith('/auth/config'))
        return json({ password_login: true, demo_login: false, auto_confirm: false });
      return json({ error: 'not_found' }, 404);
    });
    wrap(<App />);
    expect(await screen.findByRole('form', { name: 'Sign in to Cairn' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Cairn' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('explains a wrong password in words and does not reveal whether the address exists', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith('/session')) return json({ error: 'unauthenticated' }, 401);
      if (url.endsWith('/auth/config'))
        return json({ password_login: true, demo_login: false, auto_confirm: false });
      if (url.endsWith('/auth/login')) return json({ error: 'invalid_credentials' }, 401);
      return json({}, 404);
    });
    wrap(<App />);
    fireEvent.change(await screen.findByLabelText('Work email'), {
      target: { value: 'who@example.org' },
    });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'wrong-password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The email or password is not right.',
    );
  });

  it('moves to the code step after sign-up when the address must be confirmed', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith('/session')) return json({ error: 'unauthenticated' }, 401);
      if (url.endsWith('/auth/config'))
        return json({ password_login: true, demo_login: false, auto_confirm: false });
      if (url.endsWith('/auth/signup')) return json({ status: 'confirm_needed' }, 202);
      return json({}, 404);
    });
    window.location.hash = '/signup';
    wrap(<App />);
    fireEvent.change(await screen.findByLabelText('Your name'), { target: { value: 'Ana' } });
    fireEvent.change(screen.getByLabelText('Work email'), { target: { value: 'ana@example.org' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'correct-horse-9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));
    expect(
      await screen.findByText(/We sent a 6-digit code to ana@example.org/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Verification code')).toBeInTheDocument();
  });

  it('asks a signed-in account with no workspace to create one', async () => {
    fetchMock.mockImplementation((url: string) =>
      url.endsWith('/session') ? json(session('owner', false)) : json({}, 404),
    );
    wrap(<App />);
    expect(await screen.findByRole('form', { name: 'Create a workspace' })).toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'Join with an invitation' })).toBeInTheDocument();
  });

  it('says an invitation is not valid when the preview is refused', async () => {
    window.location.hash = '/invite/' + 'x'.repeat(30);
    fetchMock.mockImplementation((url: string) =>
      url.includes('/invites/preview')
        ? json({ error: 'not_found' }, 404)
        : json({ error: 'unauthenticated' }, 401),
    );
    wrap(<App />);
    expect(await screen.findByText('This invitation is not valid')).toBeInTheDocument();
  });
});

describe('PeoplePage', () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal('fetch', fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function route() {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith('/org/members')) return json(members);
      if (url.endsWith('/org/teams')) return json({ teams: [] });
      if (url.endsWith('/org/invites') && init?.method === 'POST') {
        return json(
          {
            invite_id: 'i1',
            email: 'new@example.org',
            role: 'member',
            token: 't'.repeat(40),
            path: '/#/invite/' + 't'.repeat(40),
            expires_in_hours: 168,
          },
          201,
        );
      }
      if (url.endsWith('/org/invites')) return json({ invites: [] });
      if (url.includes('/org/members/') && init?.method === 'PATCH')
        return json({ status: 'updated' });
      return json({}, 404);
    });
  }

  it('lets an owner change anyone else but not themselves', async () => {
    route();
    wrap(<PeoplePage session={session('owner')} />);
    expect(await screen.findByLabelText('Role of Bo')).toBeInTheDocument();
    expect(screen.getByLabelText('Role of Cy')).toBeInTheDocument();
    expect(screen.queryByLabelText('Role of Ana')).toBeNull();
    fireEvent.change(screen.getByLabelText('Role of Bo'), { target: { value: 'manager' } });
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH');
      expect(call?.[1]?.body).toBe(JSON.stringify({ role: 'manager' }));
    });
  });

  it('keeps an admin away from owners and other admins', async () => {
    route();
    wrap(<PeoplePage session={session('admin')} />);
    expect(await screen.findByLabelText('Role of Bo')).toBeInTheDocument();
    expect(screen.queryByLabelText('Role of Cy')).toBeNull(); // another admin
    expect(screen.queryByLabelText('Role of Ana')).toBeNull(); // the owner
  });

  it('shows a member the directory without any controls or the invitations tab', async () => {
    route();
    wrap(<PeoplePage session={session('member')} />);
    expect(await screen.findByText('Bo')).toBeInTheDocument();
    expect(screen.queryByLabelText(/Role of/)).toBeNull();
    expect(screen.queryByRole('tab', { name: /Invitations/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Deactivate' })).toBeNull();
  });

  it('creates an invitation link and shows it once', async () => {
    route();
    wrap(<PeoplePage session={session('owner')} />);
    fireEvent.click(await screen.findByRole('tab', { name: /Invitations/ }));
    fireEvent.change(await screen.findByLabelText('Email address'), {
      target: { value: 'new@example.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create invitation link' }));
    const link = (await screen.findByLabelText('Invitation link')) as HTMLInputElement;
    expect(link.value).toContain('/#/invite/');
    expect(screen.getByText(/It works once, only for that address/)).toBeInTheDocument();
  });
});
