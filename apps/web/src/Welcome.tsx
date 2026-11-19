import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { ApiError, devLogin, getPersonas } from './api';
import { initials, PERSONA_ROLES } from './personas';

/** Shown until a persona is chosen: what Cairn is, and who to sign in as. */
export function Welcome() {
  const client = useQueryClient();
  const personas = useQuery({ queryKey: ['personas'], queryFn: getPersonas, retry: false });
  const login = useMutation({
    mutationFn: devLogin,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['me'] });
      client.removeQueries({ queryKey: ['answer'] });
    },
  });

  return (
    <div className="stack">
      <section className="card hero" aria-label="Welcome">
        <h2>The reasons behind your engineering decisions, with sources.</h2>
        <p>
          Ask why something was decided, who approved it, and whether it still holds. Every answer
          cites the chat, meeting or document it came from. You only ever see what your account may
          read.
        </p>
      </section>

      <section className="stack" aria-label="Choose a persona">
        <p className="section-title">Sign in as a demo persona</p>
        {personas.isPending ? <div className="skeleton" style={{ height: 96 }} /> : null}
        {personas.error instanceof ApiError && personas.error.status === 404 ? (
          <p className="notice notice-warn" role="status">
            Persona login is not enabled on this deployment.
          </p>
        ) : null}
        {personas.isError &&
        !(personas.error instanceof ApiError && personas.error.status === 404) ? (
          <p className="notice notice-warn" role="status">
            The service is waking up. This takes about ten seconds; reload in a moment.
          </p>
        ) : null}
        {personas.data ? (
          <div className="persona-grid">
            {personas.data.personas.map((p) => (
              <button
                key={p.persona}
                type="button"
                className="persona-card"
                disabled={login.isPending}
                onClick={() => login.mutate(p.persona)}
              >
                <span className="avatar" aria-hidden="true">
                  {initials(p.display_name)}
                </span>
                <span className="persona-name">{p.display_name}</span>
                <span className="persona-role">
                  {PERSONA_ROLES[p.persona] ?? 'Demo persona with its own access rules.'}
                </span>
              </button>
            ))}
          </div>
        ) : null}
        {login.isError ? (
          <p role="alert" className="notice notice-bad">
            Could not sign in. Try again.
          </p>
        ) : null}
      </section>

      <section className="stack" aria-label="How it works">
        <p className="section-title">How it works</p>
        <ul className="how">
          <li className="card card-pad">
            <strong>Cited answers</strong>
            <span className="muted">
              Each claim links to the exact quote it rests on. If nothing supports an answer, Cairn
              says so instead of guessing.
            </span>
          </li>
          <li className="card card-pad">
            <strong>Permission-aware</strong>
            <span className="muted">
              Access is enforced in the database. Switch persona and ask the same question to see
              the difference.
            </span>
          </li>
          <li className="card card-pad">
            <strong>Decisions over time</strong>
            <span className="muted">
              See what was decided, what replaced it, and what we knew on a given date.
            </span>
          </li>
        </ul>
      </section>
      <p className="muted" style={{ fontSize: 12.5 }}>
        Demo: the personas and all data are synthetic.
      </p>
    </div>
  );
}
