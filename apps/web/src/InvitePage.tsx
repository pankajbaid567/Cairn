import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { acceptInvite, previewInvite, type Session } from './api';
import { AuthPages } from './AuthPages';
import { AuthShell } from './AuthPages';
import { ErrorNote, Loading } from './ui';

/** The page behind an invitation link: what it is for, then sign in (or up) and join. */
export function InvitePage({ token, session }: { token: string; session: Session | null }) {
  const client = useQueryClient();
  const preview = useQuery({
    queryKey: ['invite-preview', token],
    queryFn: () => previewInvite(token),
    retry: false,
  });
  const join = useMutation({
    mutationFn: () => acceptInvite(token),
    onSuccess: async () => {
      window.location.hash = '/ask';
      await client.invalidateQueries({ queryKey: ['session'] });
    },
  });

  if (preview.isPending)
    return (
      <AuthShell>
        <Loading />
      </AuthShell>
    );
  if (preview.isError)
    return (
      <AuthShell>
        <div className="stack" style={{ maxWidth: 400 }}>
          <h2>This invitation is not valid</h2>
          <p className="muted">
            It may have expired, been used, or been withdrawn. Ask the person who invited you for a
            new link.
          </p>
          <a href="#/login">Go to sign in</a>
        </div>
      </AuthShell>
    );

  const invite = preview.data;
  const signedInAs = session?.account?.email;
  if (!session || !signedInAs) {
    return (
      <AuthPages
        initialEmail={invite.email}
        lockEmail
        note={`You were invited to join ${invite.org} as ${invite.role}. Create your account with ${invite.email} to continue.`}
      />
    );
  }
  return (
    <AuthShell>
      <div className="stack" style={{ maxWidth: 420, width: '100%' }}>
        <h2 style={{ fontSize: 24 }}>Join {invite.org}</h2>
        <p className="muted">
          You will join as <strong>{invite.role}</strong>. This invitation is for {invite.email}.
        </p>
        {signedInAs !== invite.email ? (
          <p className="notice notice-warn" role="status">
            You are signed in as {signedInAs}. Sign in with {invite.email} to accept.
          </p>
        ) : null}
        {join.isError ? <ErrorNote error={join.error} /> : null}
        <button
          type="button"
          disabled={join.isPending || signedInAs !== invite.email}
          onClick={() => join.mutate()}
        >
          {join.isPending ? 'Joining…' : `Join ${invite.org}`}
        </button>
      </div>
    </AuthShell>
  );
}
