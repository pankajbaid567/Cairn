import type { ReactNode } from 'react';

import { describeError } from './api';

/** Small shared pieces, so every screen looks and behaves the same. */

export function PageHead({
  title,
  children,
  actions,
  id,
}: {
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
  id?: string;
}) {
  return (
    <div
      className="page-head row"
      style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}
    >
      <div>
        <h2 id={id}>{title}</h2>
        {children ? <p>{children}</p> : null}
      </div>
      {actions ? <div className="row">{actions}</div> : null}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  return (
    <p role="alert" className="notice notice-bad">
      {describeError(error)}
    </p>
  );
}

export function Loading({ height = 80 }: { height?: number }) {
  return <div className="skeleton" style={{ height }} aria-busy="true" />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="card empty">{children}</p>;
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { id: T; label: string; count?: number | undefined }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          className="tab"
          aria-selected={t.id === value}
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count !== undefined ? <span className="tab-count">{t.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

const ROLE_TONE: Record<string, string> = {
  owner: 'badge-info',
  admin: 'badge-info',
  manager: 'badge-ok',
  member: 'badge-plain',
  auditor: 'badge-warn',
  guest: 'badge-plain',
};

export function RoleBadge({ role }: { role: string }) {
  return <span className={`badge ${ROLE_TONE[role] ?? ''}`}>{role}</span>;
}
