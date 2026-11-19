import { useQuery } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';

import { ApiError, getMe } from './api';
import { AskPage } from './AskPage';
import { AuditIcon, AskIcon, BrandMark, DecisionsIcon, ReviewIcon, SourcesIcon } from './icons';
import { AuditPage } from './AuditPage';
import { DecisionPage } from './DecisionPage';
import { PersonaSwitcher } from './PersonaSwitcher';
import { ReadinessStatus } from './ReadinessStatus';
import { ReviewPage } from './ReviewPage';
import { SyncPage } from './SyncPage';
import { Welcome } from './Welcome';

type View = 'ask' | 'decisions' | 'review' | 'sources' | 'audit';

const NAV: { id: View; label: string; sub: string; icon: ReactNode }[] = [
  { id: 'ask', label: 'Ask', sub: 'Cited answers from your sources', icon: <AskIcon /> },
  {
    id: 'decisions',
    label: 'Decisions',
    sub: 'What was decided, and what we knew when',
    icon: <DecisionsIcon />,
  },
  {
    id: 'review',
    label: 'Review queue',
    sub: 'Conflicts and candidates to resolve',
    icon: <ReviewIcon />,
  },
  { id: 'sources', label: 'Sources', sub: 'Freshness and sync health', icon: <SourcesIcon /> },
  { id: 'audit', label: 'Access and audit', sub: 'Your own recent requests', icon: <AuditIcon /> },
];

function readHash(): View {
  const id = window.location.hash.replace(/^#\/?/, '');
  return NAV.some((n) => n.id === id) ? (id as View) : 'ask';
}

function useView(): [View, (view: View) => void] {
  const [view, setView] = useState<View>(readHash);
  useEffect(() => {
    const onChange = () => setView(readHash());
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return [
    view,
    (next) => {
      window.location.hash = `/${next}`;
      setView(next);
    },
  ];
}

export function App() {
  const [view, setView] = useView();
  const me = useQuery({ queryKey: ['me'], queryFn: getMe, retry: false });
  const signedOut = me.error instanceof ApiError && me.error.status === 401;
  const current = NAV.find((n) => n.id === view) ?? NAV[0]!;

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
          <p className="nav-label">Workspace</p>
          {NAV.map((n) => (
            <button
              key={n.id}
              type="button"
              className="nav-item"
              aria-current={n.id === view ? 'page' : undefined}
              onClick={() => setView(n.id)}
            >
              {n.icon}
              {n.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <ReadinessStatus />
          <span>Synthetic demo data</span>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div>
            <div className="topbar-title">{signedOut ? 'Welcome' : current.label}</div>
            <div className="topbar-sub">
              {signedOut ? 'Choose who you are to begin' : current.sub}
            </div>
          </div>
          {signedOut ? null : <PersonaSwitcher />}
        </header>
        <main className="content">
          {signedOut ? (
            <Welcome />
          ) : (
            <>
              {view === 'ask' ? <AskPage withPersona={false} /> : null}
              {view === 'decisions' ? <DecisionPage /> : null}
              {view === 'review' ? <ReviewPage /> : null}
              {view === 'sources' ? <SyncPage /> : null}
              {view === 'audit' ? <AuditPage /> : null}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
