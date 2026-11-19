import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';

import {
  createInvite,
  createTeam,
  getInvites,
  getMembers,
  getTeams,
  patchMember,
  revokeInvite,
  setTeamMember,
  type InviteCreated,
  type Member,
  type Session,
  type Team,
} from './api';
import { Empty, ErrorNote, Loading, PageHead, RoleBadge, Tabs } from './ui';
import { ROLE_HELP, ROLE_LABEL, when } from './ui-helpers';

type Tab = 'members' | 'teams' | 'invites';

const INVITE_ROLES = ['admin', 'manager', 'member', 'auditor', 'guest'];
const ALL_ROLES = ['owner', 'admin', 'manager', 'member', 'auditor', 'guest'];

/** Which roles the viewer may assign to someone currently holding ``target``. */
function assignable(viewer: string, target: string): string[] {
  if (viewer === 'owner') return ALL_ROLES;
  if (viewer === 'admin' && target !== 'owner' && target !== 'admin') {
    return ['manager', 'member', 'auditor', 'guest'];
  }
  return [];
}

function MemberRow({ m, viewer, teams }: { m: Member; viewer: string; teams: Team[] }) {
  const client = useQueryClient();
  const options = m.you || m.state !== 'active' ? [] : assignable(viewer, m.role);
  const canAct =
    (viewer === 'owner' || viewer === 'admin') &&
    !m.you &&
    (viewer === 'owner' || (m.role !== 'owner' && m.role !== 'admin'));
  const update = useMutation({
    mutationFn: (body: { role?: string; active?: boolean }) => patchMember(m.account_id, body),
    onSuccess: () => client.invalidateQueries({ queryKey: ['members'] }),
  });
  const names = teams.filter((t) => m.team_ids.includes(t.principal_id)).map((t) => t.name);
  return (
    <tr>
      <td>
        <strong>{m.name}</strong> {m.you ? <span className="badge badge-plain">you</span> : null}
        <div className="muted" style={{ fontSize: 12.5 }}>
          {m.email}
        </div>
      </td>
      <td>
        {options.length > 0 ? (
          <select
            aria-label={`Role of ${m.name}`}
            value={m.role}
            disabled={update.isPending}
            onChange={(e) => update.mutate({ role: e.target.value })}
          >
            {options.includes(m.role) ? null : <option value={m.role}>{ROLE_LABEL[m.role]}</option>}
            {options.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]}
              </option>
            ))}
          </select>
        ) : (
          <RoleBadge role={m.role} />
        )}
      </td>
      <td>{names.length ? names.join(', ') : <span className="muted">none</span>}</td>
      <td>
        <span className={`badge ${m.state === 'active' ? 'badge-ok' : 'badge-bad'}`}>
          {m.state === 'active' ? 'Active' : 'Deactivated'}
        </span>
      </td>
      <td style={{ textAlign: 'right' }}>
        {canAct ? (
          <button
            type="button"
            className="btn-ghost"
            disabled={update.isPending}
            onClick={() => update.mutate({ active: m.state !== 'active' })}
          >
            {m.state === 'active' ? 'Deactivate' : 'Reactivate'}
          </button>
        ) : null}
        {update.isError ? <ErrorNote error={update.error} /> : null}
      </td>
    </tr>
  );
}

function InviteForm({ onCreated }: { onCreated: (i: InviteCreated) => void }) {
  const client = useQueryClient();
  const emailId = useId();
  const roleId = useId();
  const [email, setEmail] = useState('');
  const [role, setRole] = useState('member');
  const create = useMutation({
    mutationFn: createInvite,
    onSuccess: async (invite) => {
      setEmail('');
      onCreated(invite);
      await client.invalidateQueries({ queryKey: ['invites'] });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (email.trim()) create.mutate({ email: email.trim(), role });
  }
  return (
    <form onSubmit={submit} className="card card-pad stack" aria-label="Invite a teammate">
      <div className="field-row">
        <div className="field">
          <label htmlFor={emailId}>Email address</label>
          <input
            id={emailId}
            type="email"
            value={email}
            placeholder="teammate@company.com"
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor={roleId}>Role</label>
          <select id={roleId} value={role} onChange={(e) => setRole(e.target.value)}>
            {INVITE_ROLES.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]}
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className="muted" style={{ fontSize: 12.5 }}>
        {ROLE_HELP[role]}
      </p>
      {create.isError ? <ErrorNote error={create.error} /> : null}
      <div className="row">
        <button type="submit" disabled={create.isPending || email.trim() === ''}>
          {create.isPending ? 'Creating…' : 'Create invitation link'}
        </button>
      </div>
    </form>
  );
}

function CopyLink({ invite }: { invite: InviteCreated }) {
  const url = `${window.location.origin}${invite.path}`;
  const [copied, setCopied] = useState(false);
  return (
    <div className="notice" role="status" style={{ flexDirection: 'column' }}>
      <strong>
        Invitation for {invite.email} ({invite.role})
      </strong>
      <span>
        Share this link with them. It works once, only for that address, and expires in 7 days.
      </span>
      <div className="row" style={{ width: '100%' }}>
        <input
          readOnly
          value={url}
          aria-label="Invitation link"
          style={{ flex: 1, minWidth: 220 }}
          onFocus={(e) => e.target.select()}
        />
        <button
          type="button"
          className="btn-ghost"
          onClick={() => {
            void navigator.clipboard?.writeText(url).then(() => setCopied(true));
          }}
        >
          {copied ? 'Copied' : 'Copy link'}
        </button>
      </div>
    </div>
  );
}

function TeamsTab({ session, members }: { session: Session; members: Member[] }) {
  const client = useQueryClient();
  const nameId = useId();
  const teams = useQuery({ queryKey: ['teams'], queryFn: getTeams });
  const [name, setName] = useState('');
  const [open, setOpen] = useState<number | null>(null);
  const admin = session.role === 'owner' || session.role === 'admin';
  const create = useMutation({
    mutationFn: createTeam,
    onSuccess: async () => {
      setName('');
      await client.invalidateQueries({ queryKey: ['teams'] });
    },
  });
  const toggle = useMutation({
    mutationFn: (v: { team: number; member: number; on: boolean }) =>
      setTeamMember(v.team, v.member, v.on),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ['teams'] });
      await client.invalidateQueries({ queryKey: ['members'] });
    },
  });
  return (
    <div className="stack">
      {admin ? (
        <form
          className="card card-pad row"
          aria-label="Create a team"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim().length >= 2) create.mutate(name.trim());
          }}
        >
          <label htmlFor={nameId}>New team</label>
          <input
            id={nameId}
            value={name}
            placeholder="Payments"
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" disabled={create.isPending || name.trim().length < 2}>
            Create team
          </button>
          {create.isError ? <ErrorNote error={create.error} /> : null}
        </form>
      ) : null}
      {teams.isPending ? <Loading /> : null}
      {teams.data && teams.data.teams.length === 0 ? (
        <Empty>No teams yet. Teams decide who sees whose reports and which documents.</Empty>
      ) : null}
      {teams.data?.teams.map((t) => (
        <div key={t.principal_id} className="card card-pad stack">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <strong>{t.name}</strong>
              <div className="muted" style={{ fontSize: 12.5 }}>
                {t.member_count} {t.member_count === 1 ? 'member' : 'members'}
              </div>
            </div>
            {admin ? (
              <button
                type="button"
                className="btn-ghost"
                aria-expanded={open === t.principal_id}
                onClick={() => setOpen(open === t.principal_id ? null : t.principal_id)}
              >
                {open === t.principal_id ? 'Done' : 'Edit members'}
              </button>
            ) : null}
          </div>
          {open === t.principal_id ? (
            <ul className="checklist">
              {members
                .filter((m) => m.state === 'active')
                .map((m) => (
                  <li key={m.account_id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={m.team_ids.includes(t.principal_id)}
                        disabled={toggle.isPending}
                        onChange={(e) =>
                          toggle.mutate({
                            team: t.principal_id,
                            member: m.principal_id,
                            on: e.target.checked,
                          })
                        }
                      />{' '}
                      {m.name} <span className="muted">({m.email})</span>
                    </label>
                  </li>
                ))}
            </ul>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/** Members, teams and invitations for the workspace. Only owners and admins change anything. */
export function PeoplePage({ session }: { session: Session }) {
  const client = useQueryClient();
  const role = session.role ?? 'guest';
  const admin = role === 'owner' || role === 'admin';
  const [tab, setTab] = useState<Tab>('members');
  const [created, setCreated] = useState<InviteCreated | null>(null);
  const members = useQuery({ queryKey: ['members'], queryFn: getMembers });
  const teams = useQuery({ queryKey: ['teams'], queryFn: getTeams });
  const invites = useQuery({ queryKey: ['invites'], queryFn: getInvites, enabled: admin });
  const revoke = useMutation({
    mutationFn: revokeInvite,
    onSuccess: () => client.invalidateQueries({ queryKey: ['invites'] }),
  });
  const list = members.data?.members ?? [];
  const active = list.filter((m) => m.state === 'active').length;

  return (
    <section aria-labelledby="people-h" className="stack">
      <PageHead title="People" id="people-h">
        Who is in this workspace, what role they have, and which teams they belong to.
      </PageHead>
      <Tabs
        label="People sections"
        value={tab}
        onChange={setTab}
        tabs={[
          { id: 'members', label: 'Members', count: active },
          { id: 'teams', label: 'Teams', count: teams.data?.teams.length },
          ...(admin
            ? [
                {
                  id: 'invites' as const,
                  label: 'Invitations',
                  count: invites.data?.invites.length,
                },
              ]
            : []),
        ]}
      />
      {tab === 'members' ? (
        <div className="stack">
          {members.isPending ? <Loading /> : null}
          {members.isError ? <ErrorNote error={members.error} /> : null}
          {members.data ? (
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Person</th>
                    <th scope="col">Role</th>
                    <th scope="col">Teams</th>
                    <th scope="col">Status</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((m) => (
                    <MemberRow
                      key={m.account_id}
                      m={m}
                      viewer={role}
                      teams={teams.data?.teams ?? []}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <dl className="role-help">
            {Object.entries(ROLE_HELP).map(([r, text]) => (
              <div key={r}>
                <dt>
                  <RoleBadge role={r} />
                </dt>
                <dd>{text}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
      {tab === 'teams' ? <TeamsTab session={session} members={list} /> : null}
      {tab === 'invites' && admin ? (
        <div className="stack">
          <InviteForm onCreated={setCreated} />
          {created ? <CopyLink invite={created} /> : null}
          {invites.data && invites.data.invites.length === 0 ? (
            <Empty>No pending invitations.</Empty>
          ) : null}
          {invites.data && invites.data.invites.length > 0 ? (
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Email</th>
                    <th scope="col">Role</th>
                    <th scope="col">Sent</th>
                    <th scope="col">Expires</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {invites.data.invites.map((i) => (
                    <tr key={i.invite_id}>
                      <td>{i.email}</td>
                      <td>
                        <RoleBadge role={i.role} />
                      </td>
                      <td className="mono">{when(i.created_at)}</td>
                      <td className="mono">{when(i.expires_at)}</td>
                      <td style={{ textAlign: 'right' }}>
                        <button
                          type="button"
                          className="btn-ghost"
                          disabled={revoke.isPending}
                          onClick={() => revoke.mutate(i.invite_id)}
                        >
                          Revoke
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
