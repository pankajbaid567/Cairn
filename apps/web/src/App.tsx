import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';

import { getSession, signOut, switchOrg, type Session } from './api';
import { AskPage } from './AskPage';
import { AuthPages, AuthShell } from './AuthPages';
import { DecisionPage } from './DecisionPage';
import {
  AskIcon,
  AuditIcon,
  BrandMark,
  DecisionsIcon,
  PeopleIcon,
  ReviewIcon,
  SettingsIcon,
  SourcesIcon,
} from './icons';
import { InvitePage } from './InvitePage';
import { Onboarding } from './Onboarding';
import { OrgAuditPage } from './OrgAuditPage';
import { GovernancePage } from './GovernancePage';
import { PeoplePage } from './PeoplePage';
import { ReportsPage } from './ReportsPage';
import { PersonaSwitcher } from './PersonaSwitcher';
import { ReadinessStatus } from './ReadinessStatus';
import { ReviewPage } from './ReviewPage';
import { SettingsPage } from './SettingsPage';
import { SyncPage } from './SyncPage';
import { initials } from './personas';
import { Loading, RoleBadge } from './ui';

type View =
  | 'reports'
  | 'governance'
  | 'ask'
  | 'decisions'
  | 'review'
  | 'sources'
  | 'people'
  | 'audit'
  | 'settings';

interface NavItem {
  id: View;
  label: string;
  sub: string;
  icon: ReactNode;
  group: 'Work' | 'Knowledge' | 'Workspace';
}

const NAV: NavItem[] = [
  {
    id: 'ask',
    label: 'Ask',
    sub: 'Cited answers from your sources',
    icon: <AskIcon />,
    group: 'Knowledge',
  },
  {
    id: 'decisions',
    label: 'Decisions',
    sub: 'What was decided, and what we knew when',
    icon: <DecisionsIcon />,
    group: 'Knowledge',
  },
  {
    id: 'review',
    label: 'Review queue',
    sub: 'Conflicts and candidates to resolve',
    icon: <ReviewIcon />,
    group: 'Knowledge',
  },
  {
    id: 'sources',
    label: 'Sources',
    sub: 'Freshness and sync health',
    icon: <SourcesIcon />,
    group: 'Knowledge',
  },
  {
    id: 'people',
    label: 'People',
    sub: 'Members, teams and invitations',
    icon: <PeopleIcon />,
    group: 'Workspace',
  },
  {
    id: 'audit',
    label: 'Audit trail',
    sub: 'Who did what, tamper-evident',
    icon: <AuditIcon />,
    group: 'Workspace',
  },
  {
    id: 'settings',
    label: 'Settings',
    sub: 'Workspace and account',
    icon: <SettingsIcon />,
    group: 'Workspace',
  },
];

function readRoute(): { view: View; invite: string | null; auth: string } {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [head, ...rest] = raw.split('/');
  if (head === 'invite' && rest[0]) return { view: 'ask', invite: rest.join('/'), auth: '' };
  const view = NAV.some((n) => n.id === head) ? (head as View) : 'reports';
  return { view, invite: null, auth: head ?? '' };
}

function useRoute() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const on = () => setRoute(readRoute());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return {
    ...route,
    go: (view: View) => {
      window.location.hash = `/${view}`;
      setRoute(readRoute());
    },
  };
}

function UserMenu({ session }: { session: Session }) {
  const client = useQueryClient();
  const out = useMutation({
    mutationFn: signOut,
    onSuccess: async () => {
      client.clear();
      window.location.hash = '/login';
      await client.invalidateQueries({ queryKey: ['session'] });
    },
  });
  const swap = useMutation({
    mutationFn: switchOrg,
    onSuccess: async () => {
      client.removeQueries({ predicate: (q) => q.queryKey[0] !== 'session' });
      await client.invalidateQueries({ queryKey: ['session'] });
    },
  });
  const name = session.account?.name ?? session.persona ?? 'You';
  return (
    <div className="row" style={{ gap: 12 }}>
      {session.orgs.length > 1 ? (
        <select
          aria-label="Workspace"
          value={session.org?.org_id ?? ''}
          disabled={swap.isPending}
          onChange={(e) => swap.mutate(Number(e.target.value))}
        >
          {session.orgs.map((o) => (
            <option key={o.org_id} value={o.org_id}>
              {o.name}
            </option>
          ))}
        </select>
      ) : (
        <span className="org-name">{session.org?.name}</span>
      )}
      {session.role ? <RoleBadge role={session.role} /> : null}
      {session.demo ? <PersonaSwitcher /> : null}
      <details className="user-menu">
        <summary aria-label="Account menu">
          <span className="avatar" aria-hidden="true">
            {initials(name)}
          </span>
        </summary>
        <div className="card menu-pop">
          <div style={{ padding: '4px 8px 8px' }}>
            <strong>{name}</strong>
            <div className="muted" style={{ fontSize: 12.5 }}>
              {session.account?.email ?? 'Demo persona'}
            </div>
          </div>
          <button type="button" className="menu-item" onClick={() => out.mutate()}>
            Sign out
          </button>
        </div>
      </details>
    </div>
  );
}

function Shell({ session }: { session: Session }) {
  const { view, go } = useRoute();
  const current = NAV.find((n) => n.id === view) ?? NAV[0]!;
  const groups = ['Work', 'Knowledge', 'Workspace'] as const;
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <BrandMark />
          </span>
          <div>
            <h1>Cairn</h1>
            <p className="brand-tag">Permission-aware answers from your engineering history.</p>
          </div>
        </div>
        <nav className="nav" aria-label="Main">
          {groups.map((g) => (
            <div key={g} className="nav-group">
              <p className="nav-label">{g}</p>
              {NAV.filter((n) => n.group === g).map((n) => (
                <button
                  key={n.id}
                  type="button"
                  className="nav-item"
                  aria-current={n.id === view ? 'page' : undefined}
                  onClick={() => go(n.id)}
                >
                  {n.icon}
                  {n.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <ReadinessStatus />
          {session.org?.is_demo ? <span>Synthetic demo data</span> : null}
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div>
            <div className="topbar-title">{current.label}</div>
            <div className="topbar-sub">{current.sub}</div>
          </div>
          <UserMenu session={session} />
        </header>
        <main className="content">
          {view === 'reports' ? <ReportsPage session={session} /> : null}
          {view === 'governance' ? <GovernancePage session={session} /> : null}
          {view === 'ask' ? <AskPage withPersona={false} /> : null}
          {view === 'decisions' ? <DecisionPage /> : null}
          {view === 'review' ? <ReviewPage /> : null}
          {view === 'sources' ? <SyncPage /> : null}
          {view === 'people' ? <PeoplePage session={session} /> : null}
          {view === 'audit' ? <OrgAuditPage session={session} /> : null}
          {view === 'settings' ? <SettingsPage session={session} /> : null}
        </main>
      </div>
    </div>
  );
}

export function App() {
  const route = useRoute();
  const session = useQuery({ queryKey: ['session'], queryFn: getSession, retry: false });

  if (route.invite) {
    return <InvitePage token={route.invite} session={session.data ?? null} />;
  }
  if (session.isPending) {
    return (
      <AuthShell>
        <div style={{ width: 320 }}>
          <Loading height={120} />
        </div>
      </AuthShell>
    );
  }
  if (session.isError) {
    return (
      <AuthShell>
        <div className="stack" style={{ maxWidth: 400 }}>
          <h2>The service is waking up</h2>
          <p className="muted">
            It takes about ten seconds after a quiet period. Reload in a moment.
          </p>
          <button type="button" onClick={() => void session.refetch()}>
            Try again
          </button>
        </div>
      </AuthShell>
    );
  }
  if (session.data === null) return <AuthPages />;
  if (session.data.org === null) return <Onboarding session={session.data} />;
  return <Shell session={session.data} />;
}
