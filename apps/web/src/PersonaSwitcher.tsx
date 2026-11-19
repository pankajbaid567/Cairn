import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId } from 'react';

import { ApiError, devLogin, getMe, getPersonas } from './api';

/** Chooses who is asking. Posts the dev login and refreshes the `me` query. */
export function PersonaSwitcher() {
  const id = useId();
  const client = useQueryClient();
  const personas = useQuery({ queryKey: ['personas'], queryFn: getPersonas, retry: false });
  const me = useQuery({ queryKey: ['me'], queryFn: getMe, retry: false });
  const login = useMutation({
    mutationFn: devLogin,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['me'] });
      client.removeQueries({ queryKey: ['answer'] });
    },
  });

  if (personas.isPending)
    return (
      <p role="status" className="switcher-note">
        Loading personas
      </p>
    );
  if (personas.error instanceof ApiError && personas.error.status === 404) {
    return (
      <p role="status" className="switcher-note">
        Persona login is not enabled on this deployment.
      </p>
    );
  }
  if (personas.isError)
    return (
      <p role="status" className="switcher-note">
        Personas are unavailable (the API may be waking up).
      </p>
    );

  return (
    <div className="switcher">
      {personas.data.demo ? (
        <p className="switcher-note">Demo: the personas and all data are synthetic.</p>
      ) : null}
      <label htmlFor={id}>Ask as </label>
      <select
        id={id}
        value={me.data?.persona ?? ''}
        onChange={(event) => {
          if (event.target.value) login.mutate(event.target.value);
        }}
        disabled={login.isPending}
      >
        <option value="" disabled>
          Choose a persona
        </option>
        {personas.data.personas.map((p) => (
          <option key={p.persona} value={p.persona}>
            {p.display_name}
          </option>
        ))}
      </select>
      {login.isError ? (
        <p role="alert" className="switcher-note">
          Could not switch persona.
        </p>
      ) : null}
    </div>
  );
}
