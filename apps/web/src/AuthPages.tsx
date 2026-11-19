import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useId, useState, type FormEvent, type ReactNode } from 'react';

import { confirmEmail, forgotPassword, getAuthConfig, resetPassword, signIn, signUp } from './api';
import { BrandMark } from './icons';
import { ErrorNote } from './ui';
import { Welcome } from './Welcome';

type Mode = 'login' | 'signup' | 'forgot';

function readMode(): Mode {
  const id = window.location.hash.replace(/^#\/?/, '');
  return id === 'signup' || id === 'forgot' ? id : 'login';
}

export function AuthShell({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <div className="auth-page">
      <div className="auth-side" aria-hidden="false">
        <div className="brand">
          <span className="brand-mark">
            <BrandMark />
          </span>
          <div>
            <h1>Cairn</h1>
            <p className="brand-tag">Permission-aware answers from your engineering history.</p>
          </div>
        </div>
        <ul className="auth-points">
          <li>
            <strong>Decisions with evidence.</strong> Every answer cites the source it came from.
          </li>
          <li>
            <strong>Daily work, reported.</strong> What the team did, rolled up for managers.
          </li>
          <li>
            <strong>Audit-ready.</strong> A tamper-evident trail of who did what.
          </li>
        </ul>
        <p className="muted" style={{ fontSize: 12.5 }}>
          Demo environment. Do not enter real company data.
        </p>
      </div>
      <div className={`auth-main${wide ? ' auth-wide' : ''}`}>{children}</div>
    </div>
  );
}

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: (id: string) => ReactNode;
  hint?: string | undefined;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint ? (
        <span className="muted" style={{ fontSize: 12 }}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

/** Sign in, create an account, or recover a password. Calls the identity provider through the API. */
export function AuthPages({
  initialEmail = '',
  lockEmail = false,
  note,
}: {
  initialEmail?: string;
  lockEmail?: boolean;
  note?: ReactNode;
}) {
  const client = useQueryClient();
  const config = useQuery({ queryKey: ['auth-config'], queryFn: getAuthConfig, retry: false });
  const [mode, setMode] = useState<Mode>(lockEmail ? 'signup' : readMode);
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [step, setStep] = useState<'form' | 'confirm' | 'reset'>('form');
  const [info, setInfo] = useState('');

  useEffect(() => {
    if (lockEmail) return;
    const on = () => setMode(readMode());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, [lockEmail]);

  const done = () => client.invalidateQueries({ queryKey: ['session'] });
  const login = useMutation({ mutationFn: signIn, onSuccess: done });
  const signup = useMutation({
    mutationFn: signUp,
    onSuccess: async (res) => {
      if (res.status === 'confirm_needed') setStep('confirm');
      else await done();
    },
  });
  const confirm = useMutation({
    mutationFn: confirmEmail,
    onSuccess: () => login.mutate({ email: email.trim(), password }),
  });
  const forgot = useMutation({
    mutationFn: forgotPassword,
    onSuccess: () => {
      setInfo('If that address has an account, we sent a code to it.');
      setStep('reset');
    },
  });
  const reset = useMutation({
    mutationFn: resetPassword,
    onSuccess: () => {
      setInfo('Password changed. Sign in with the new one.');
      setStep('form');
      setMode('login');
      setCode('');
      setPassword('');
    },
  });

  const busy =
    login.isPending || signup.isPending || confirm.isPending || forgot.isPending || reset.isPending;
  const error = login.error ?? signup.error ?? confirm.error ?? forgot.error ?? reset.error;
  const passwordLoginOff = config.data !== undefined && !config.data.password_login;

  function go(next: Mode) {
    setStep('form');
    setInfo('');
    setMode(next);
    if (!lockEmail) window.location.hash = `/${next}`;
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const mail = email.trim();
    if (step === 'confirm') confirm.mutate({ email: mail, code: code.trim() });
    else if (step === 'reset') reset.mutate({ email: mail, code: code.trim(), password });
    else if (mode === 'login') login.mutate({ email: mail, password });
    else if (mode === 'signup') signup.mutate({ email: mail, password, name: name.trim() });
    else forgot.mutate(mail);
  }

  const title =
    step === 'confirm'
      ? 'Check your email'
      : step === 'reset'
        ? 'Choose a new password'
        : mode === 'login'
          ? 'Sign in to Cairn'
          : mode === 'signup'
            ? 'Create your account'
            : 'Reset your password';

  return (
    <AuthShell wide={config.data?.demo_login === true}>
      <div className="stack" style={{ maxWidth: 400, width: '100%' }}>
        <div>
          <h2 style={{ fontSize: 24, letterSpacing: '-0.02em' }}>{title}</h2>
          {note ? <p className="muted">{note}</p> : null}
        </div>
        {passwordLoginOff ? (
          <p className="notice notice-warn" role="status">
            Email sign-in is not enabled here. Use the demo below.
          </p>
        ) : (
          <form onSubmit={submit} className="stack" aria-label={title}>
            {step === 'confirm' ? (
              <p className="notice" role="status">
                We sent a 6-digit code to {email}. Enter it to confirm your address.
              </p>
            ) : null}
            {info ? (
              <p className="notice" role="status">
                {info}
              </p>
            ) : null}
            {mode === 'signup' && step === 'form' ? (
              <Field label="Your name">
                {(id) => (
                  <input
                    id={id}
                    value={name}
                    maxLength={80}
                    autoComplete="name"
                    onChange={(e) => setName(e.target.value)}
                  />
                )}
              </Field>
            ) : null}
            <Field label="Work email">
              {(id) => (
                <input
                  id={id}
                  type="email"
                  value={email}
                  autoComplete="email"
                  readOnly={lockEmail || step !== 'form'}
                  onChange={(e) => setEmail(e.target.value)}
                />
              )}
            </Field>
            {step === 'confirm' || step === 'reset' ? (
              <Field label="Verification code">
                {(id) => (
                  <input
                    id={id}
                    value={code}
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={12}
                    onChange={(e) => setCode(e.target.value)}
                  />
                )}
              </Field>
            ) : null}
            {(mode !== 'forgot' && step === 'form') || step === 'reset' ? (
              <Field
                label={step === 'reset' ? 'New password' : 'Password'}
                hint={
                  mode === 'signup' || step === 'reset'
                    ? 'At least 10 characters, with upper and lower case letters and a number.'
                    : undefined
                }
              >
                {(id) => (
                  <input
                    id={id}
                    type="password"
                    value={password}
                    autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                    maxLength={128}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                )}
              </Field>
            ) : null}
            {error ? <ErrorNote error={error} /> : null}
            <button type="submit" disabled={busy} style={{ width: '100%' }}>
              {busy
                ? 'One moment…'
                : step === 'confirm'
                  ? 'Confirm and continue'
                  : step === 'reset'
                    ? 'Set password'
                    : mode === 'login'
                      ? 'Sign in'
                      : mode === 'signup'
                        ? 'Create account'
                        : 'Send reset code'}
            </button>
            <div className="row" style={{ justifyContent: 'space-between', fontSize: 13 }}>
              {mode === 'login' ? (
                <>
                  <button type="button" className="link" onClick={() => go('signup')}>
                    Create an account
                  </button>
                  <button type="button" className="link" onClick={() => go('forgot')}>
                    Forgot password?
                  </button>
                </>
              ) : (
                <button type="button" className="link" onClick={() => go('login')}>
                  Back to sign in
                </button>
              )}
            </div>
          </form>
        )}
      </div>
      {config.data?.demo_login ? (
        <div className="demo-panel">
          <Welcome compact />
        </div>
      ) : null}
    </AuthShell>
  );
}
