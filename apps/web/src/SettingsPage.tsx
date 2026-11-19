import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import { getOrg, renameOrg, type Session } from './api';
import { ErrorNote, Loading, PageHead, RoleBadge } from './ui';
import { when } from './ui-helpers';

/** Workspace and account settings. */
export function SettingsPage({ session }: { session: Session }) {
  const client = useQueryClient();
  const nameId = useId();
  const org = useQuery({ queryKey: ['org'], queryFn: getOrg });
  const [edited, setName] = useState<string | null>(null);
  const name = edited ?? org.data?.name ?? '';
  const admin = session.role === 'owner' || session.role === 'admin';
  const rename = useMutation({
    mutationFn: renameOrg,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['org'] });
      await client.invalidateQueries({ queryKey: ['session'] });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim().length >= 2) rename.mutate(name.trim());
  }
  return (
    <section aria-labelledby="settings-h" className="stack">
      <PageHead title="Settings" id="settings-h">
        Your workspace and your account.
      </PageHead>
      {org.isPending ? <Loading /> : null}
      {org.isError ? <ErrorNote error={org.error} /> : null}
      {org.data ? (
        <>
          <form onSubmit={submit} className="card card-pad stack" aria-label="Workspace">
            <p className="section-title">Workspace</p>
            <div className="field">
              <label htmlFor={nameId}>Name</label>
              <input
                id={nameId}
                value={name}
                maxLength={80}
                disabled={!admin}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <dl className="facts">
              <div>
                <dt>Plan</dt>
                <dd>
                  <span className="badge badge-info">{org.data.plan}</span>
                  {org.data.is_demo ? <span className="badge badge-warn">demo data</span> : null}
                </dd>
              </div>
              <div>
                <dt>Members</dt>
                <dd>
                  {org.data.active_members} active, {org.data.deactivated_members} deactivated
                </dd>
              </div>
              <div>
                <dt>Created</dt>
                <dd>{when(org.data.created_at)}</dd>
              </div>
              <div>
                <dt>Address</dt>
                <dd className="mono">{org.data.slug}</dd>
              </div>
            </dl>
            {rename.isError ? <ErrorNote error={rename.error} /> : null}
            {admin ? (
              <div className="row">
                <button
                  type="submit"
                  disabled={
                    rename.isPending || name.trim().length < 2 || name.trim() === org.data.name
                  }
                >
                  Save name
                </button>
              </div>
            ) : (
              <p className="muted">Only owners and admins can change workspace settings.</p>
            )}
          </form>
          <div className="card card-pad stack">
            <p className="section-title">Your account</p>
            <dl className="facts">
              <div>
                <dt>Name</dt>
                <dd>{session.account?.name ?? session.persona}</dd>
              </div>
              <div>
                <dt>Email</dt>
                <dd>{session.account?.email ?? 'demo persona'}</dd>
              </div>
              <div>
                <dt>Role</dt>
                <dd>{session.role ? <RoleBadge role={session.role} /> : null}</dd>
              </div>
            </dl>
            <p className="muted" style={{ fontSize: 12.5 }}>
              Sessions last {session.demo ? '2' : '8'} hours. Sign out from the menu at the top
              right.
            </p>
          </div>
        </>
      ) : null}
    </section>
  );
}
