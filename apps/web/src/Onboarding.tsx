import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import { acceptInvite, createOrg, signOut, type Session } from './api';
import { AuthShell } from './AuthPages';
import { ErrorNote } from './ui';

/** A signed-in account with no workspace yet: create one, or join with an invitation. */
export function Onboarding({ session }: { session: Session }) {
  const client = useQueryClient();
  const nameId = useId();
  const tokenId = useId();
  const [name, setName] = useState('');
  const [link, setLink] = useState('');
  const refresh = () => client.invalidateQueries({ queryKey: ['session'] });
  const create = useMutation({ mutationFn: createOrg, onSuccess: refresh });
  const join = useMutation({ mutationFn: acceptInvite, onSuccess: refresh });
  const out = useMutation({ mutationFn: signOut, onSuccess: refresh });

  function submitCreate(event: FormEvent) {
    event.preventDefault();
    if (name.trim().length >= 2) create.mutate(name.trim());
  }
  function submitJoin(event: FormEvent) {
    event.preventDefault();
    const token = link.trim().split('/invite/').pop() ?? '';
    if (token.length >= 20) join.mutate(token);
  }

  return (
    <AuthShell>
      <div className="stack" style={{ maxWidth: 440, width: '100%' }}>
        <div>
          <h2 style={{ fontSize: 24, letterSpacing: '-0.02em' }}>
            Welcome{session.account ? `, ${session.account.name.split(' ')[0]}` : ''}
          </h2>
          <p className="muted">
            Create a workspace for your team, or join one you were invited to.
          </p>
        </div>
        <form
          onSubmit={submitCreate}
          className="card card-pad stack"
          aria-label="Create a workspace"
        >
          <div className="field">
            <label htmlFor={nameId}>Workspace name</label>
            <input
              id={nameId}
              value={name}
              maxLength={80}
              placeholder="Acme Engineering"
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          {create.isError ? <ErrorNote error={create.error} /> : null}
          <button type="submit" disabled={create.isPending || name.trim().length < 2}>
            {create.isPending ? 'Creating…' : 'Create workspace'}
          </button>
          <p className="muted" style={{ fontSize: 12.5 }}>
            You become the owner. You can invite people and set roles next.
          </p>
        </form>
        <form
          onSubmit={submitJoin}
          className="card card-pad stack"
          aria-label="Join with an invitation"
        >
          <div className="field">
            <label htmlFor={tokenId}>Invitation link</label>
            <input
              id={tokenId}
              value={link}
              placeholder="Paste the link you were sent"
              onChange={(e) => setLink(e.target.value)}
            />
          </div>
          {join.isError ? <ErrorNote error={join.error} /> : null}
          <button
            type="submit"
            className="btn-ghost"
            disabled={join.isPending || link.trim().length < 20}
          >
            Join workspace
          </button>
        </form>
        <button type="button" className="link" onClick={() => out.mutate()}>
          Sign out ({session.account?.email})
        </button>
      </div>
    </AuthShell>
  );
}
