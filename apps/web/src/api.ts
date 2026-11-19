/** Calls to the Cairn API (design §17, §20). Same origin: `/api` is proxied locally and by the Pages Function. */

export interface Persona {
  persona: string;
  display_name: string;
}

export interface PersonasResponse {
  demo: boolean;
  personas: Persona[];
}

export interface CitationInfo {
  id: string;
  title: string;
  source_kind: string;
  location: string;
  quote: string;
}

export interface Claim {
  text: string;
  citations: string[];
}

export interface AnswerResponse {
  answer: string;
  claims: Claim[];
  citations: CitationInfo[];
  abstained: boolean;
  searched: string[];
  degraded: string[];
  freshness: { weakest_badge: string };
  model: { provider?: string; model?: string; prompt_version?: string };
  audit_id: string;
}

export interface DecisionClaim {
  key: string;
  text: string;
  tier: string;
  citations: { external_id: string; title: string; support_set: number }[];
}

export interface TimelineEvent {
  event: string;
  valid_at: string | null;
  recorded_at: string | null;
  by: string | null;
  note: string | null;
  source: string | null;
}

export interface DecisionResponse {
  decision: {
    key: string;
    title: string;
    question: string | null;
    outcome: string | null;
    verification: string;
    origin: string;
  };
  as_of: { valid: string; recorded: string };
  status: string;
  claims: DecisionClaim[];
  summary: string | null;
  timeline: TimelineEvent[];
  links: { from: string; to: string; kind: string }[];
  audit_id: string;
}

export interface DecisionHit {
  key: string;
  title: string | null;
  outcome: string | null;
  verification: string | null;
}

/** A failed API call: the HTTP status, the fixed error code and any Retry-After in seconds. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfter: number | null,
    readonly reason: string | null = null,
  ) {
    super(`${status} ${code}`);
    this.name = 'ApiError';
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      ...init,
    });
  } catch {
    throw new ApiError(0, 'unreachable', null);
  }
  if (!response.ok) {
    let code = 'error';
    let reason: string | null = null;
    try {
      const body = (await response.json()) as { error?: unknown; reason?: unknown };
      if (typeof body.error === 'string') code = body.error;
      if (typeof body.reason === 'string') reason = body.reason;
    } catch {
      // not JSON: keep the generic code
    }
    const retry = Number.parseInt(response.headers.get('retry-after') ?? '', 10);
    throw new ApiError(response.status, code, Number.isFinite(retry) ? retry : null, reason);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const getPersonas = () => request<PersonasResponse>('/auth/personas');
export const devLogin = (persona: string) =>
  request<void>('/auth/dev-login', { method: 'POST', body: JSON.stringify({ persona }) });
export const logout = () => request<void>('/auth/logout', { method: 'POST' });
export const getMe = () => request<{ persona: string; display_name: string }>('/me');
export const postAnswer = (question: string) =>
  request<AnswerResponse>('/answer', { method: 'POST', body: JSON.stringify({ question }) });

export interface AsOf {
  valid?: string | undefined;
  recorded?: string | undefined;
}

export const getDecision = (key: string, asOf: AsOf = {}) => {
  const params = new URLSearchParams();
  if (asOf.valid) params.set('valid', asOf.valid);
  if (asOf.recorded) params.set('recorded', asOf.recorded);
  const query = params.toString();
  return request<DecisionResponse>(
    `/decisions/${encodeURIComponent(key)}${query ? `?${query}` : ''}`,
  );
};
export const searchDecisions = (q: string) =>
  request<{ decisions: DecisionHit[] }>(`/decisions?q=${encodeURIComponent(q)}`);

export interface SourceInfo {
  source: string;
  badge: 'Live' | 'Delayed' | 'Stale' | 'Imported';
  last_synced_at: string | null;
  oldest_pending_s: number | null;
}

export interface SyncSources {
  sources: SourceInfo[];
  pending_acl: number;
  pending_content: number;
  oldest_pending_s: number | null;
  last_applied_at: string | null;
}

export interface AuditEntry {
  audit_id: string;
  at: string;
  endpoint: string;
  outcome: string;
  returned: number;
  acl_version: number;
  model: string | null;
}

export interface ReviewItem {
  review_id: number;
  kind: string;
  decision: string | null;
  state: string;
  reason: string | null;
  created_at: string;
  resolved_at: string | null;
  detail: { subject?: string };
  sides: { value: string; tier: string; source: string | null }[] | null;
}

export type ReviewAction = 'verified' | 'edited' | 'disputed' | 'rejected';

export const getReviews = (state = 'open') =>
  request<{ reviews: ReviewItem[] }>(`/reviews?state=${encodeURIComponent(state)}`);
export const resolveReview = (
  id: number,
  body: { state: ReviewAction; reason: string; edit?: Record<string, string> },
) =>
  request<{ review_id: number; state: string }>(`/reviews/${id}/resolve`, {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const getSyncSources = () => request<SyncSources>('/sync/sources');
export const getMyAudit = () => request<{ entries: AuditEntry[] }>('/audit/mine');

/** The text a user sees for an API failure. Never shows a status color alone. */
const CODE_TEXT: Record<string, string> = {
  invalid_credentials: 'The email or password is not right.',
  not_confirmed: 'Confirm your email address first: enter the code we sent you.',
  reset_required: 'This account needs a password reset.',
  weak_password:
    'Use at least 10 characters with an upper-case letter, a lower-case letter and a number.',
  bad_code: 'That code is wrong or has expired.',
  invalid_request: 'Some of what you entered is not valid.',
  invalid_invite: 'This invitation is not valid for your account. Sign in with the invited email.',
  not_a_member: 'You are not a member of that workspace.',
  no_organisation: 'Create or join a workspace first.',
  forbidden: 'Your role does not allow this.',
  team_exists: 'A team with that name already exists.',
  slug_taken: 'That workspace name is taken. Try another.',
  invalid_date: 'Reports can be written for today or the last 14 days.',
  empty_report: 'Write what you did, or a blocker, before saving.',
  refused: 'That was not allowed.',
  policy_exists: 'A policy with that name already exists.',
  not_pending: 'That request is no longer pending.',
  digest_unavailable: 'The digest could not be produced right now. Try again in a moment.',
};

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Something went wrong. Try again.';
  if (error.reason && (error.status === 403 || error.status === 409)) return error.reason;
  const byCode = CODE_TEXT[error.code];
  if (byCode) return byCode;
  if (error.status === 401) return 'Choose a persona first.';
  if (error.code === 'permissions_syncing')
    return 'Access rules are being updated. Try again in a few seconds.';
  if (error.status === 403) return 'This action is not allowed for this account.';
  if (error.status === 404) return 'No such decision, or you cannot see it.';
  if (error.status === 429)
    return error.retryAfter !== null
      ? `Too many requests, try again in ${error.retryAfter} seconds.`
      : 'Too many requests, try again shortly.';
  if (error.code === 'model_budget_exhausted')
    return "The demo's daily model allowance is used up; try again after 00:00 UTC.";
  if (error.code === 'api_unavailable' || error.status === 0)
    return 'The API is waking up. This can take about a minute; try again soon.';
  if (error.status === 503 || error.status === 502)
    return 'The answer service is unavailable right now. Try again.';
  if (error.status === 422) return 'That question could not be used. Keep it under 500 characters.';
  return 'Something went wrong. Try again.';
}

// ---- accounts, organisations and people ----

export interface OrgSummary {
  org_id: number;
  slug: string;
  name: string;
  role: string;
  is_demo: boolean;
}

export interface Session {
  authenticated: boolean;
  account: { id: string; email: string; name: string } | null;
  persona: string | null;
  org: OrgSummary | null;
  role: string | null;
  orgs: OrgSummary[];
  demo: boolean;
  principal_id: number | null;
}

export interface AuthConfig {
  password_login: boolean;
  demo_login: boolean;
  auto_confirm: boolean;
}

/** The signed-in session, or null when nobody is signed in. */
export async function getSession(): Promise<Session | null> {
  try {
    return await request<Session>('/session');
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

export const getAuthConfig = () => request<AuthConfig>('/auth/config');
export const signUp = (b: { email: string; password: string; name: string }) =>
  request<{ status?: string; org_id?: number | null }>('/auth/signup', {
    method: 'POST',
    body: JSON.stringify(b),
  });
export const confirmEmail = (b: { email: string; code: string }) =>
  request<{ status: string }>('/auth/confirm', { method: 'POST', body: JSON.stringify(b) });
export const signIn = (b: { email: string; password: string }) =>
  request<{ org_id: number | null }>('/auth/login', { method: 'POST', body: JSON.stringify(b) });
export const forgotPassword = (email: string) =>
  request<{ status: string }>('/auth/forgot', { method: 'POST', body: JSON.stringify({ email }) });
export const resetPassword = (b: { email: string; code: string; password: string }) =>
  request<{ status: string }>('/auth/reset', { method: 'POST', body: JSON.stringify(b) });
export const signOut = () => request<{ status: string }>('/auth/signout', { method: 'POST' });
export const createOrg = (name: string) =>
  request<{ org_id: number }>('/orgs', { method: 'POST', body: JSON.stringify({ name }) });
export const switchOrg = (org_id: number) =>
  request<{ org_id: number }>('/session/org', { method: 'POST', body: JSON.stringify({ org_id }) });

export interface OrgInfo {
  org_id: number;
  slug: string;
  name: string;
  plan: string;
  is_demo: boolean;
  created_at: string;
  active_members: number;
  deactivated_members: number;
  role: string;
}
export const getOrg = () => request<OrgInfo>('/org');
export const renameOrg = (name: string) =>
  request<{ name: string }>('/org', { method: 'PATCH', body: JSON.stringify({ name }) });

export interface Member {
  account_id: string;
  principal_id: number;
  email: string;
  name: string;
  role: string;
  state: string;
  title: string | null;
  joined_at: string;
  team_ids: number[];
  you: boolean;
}
export const getMembers = () => request<{ members: Member[] }>('/org/members');
export const patchMember = (
  id: string,
  body: { role?: string; active?: boolean; title?: string },
) =>
  request<{ status: string }>(`/org/members/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });

export interface InviteRow {
  invite_id: string;
  email: string;
  role: string;
  state: string;
  created_at: string;
  expires_at: string;
}
export interface InviteCreated {
  invite_id: string;
  email: string;
  role: string;
  token: string;
  path: string;
  expires_in_hours: number;
}
export const getInvites = () => request<{ invites: InviteRow[] }>('/org/invites');
export const createInvite = (b: { email: string; role: string }) =>
  request<InviteCreated>('/org/invites', { method: 'POST', body: JSON.stringify(b) });
export const revokeInvite = (id: string) =>
  request<{ status: string }>(`/org/invites/${id}`, { method: 'DELETE' });
export const previewInvite = (token: string) =>
  request<{ org: string; role: string; email: string }>(
    `/invites/preview?token=${encodeURIComponent(token)}`,
  );
export const acceptInvite = (token: string) =>
  request<{ org_id: number }>('/invites/accept', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });

export interface Team {
  principal_id: number;
  slug: string;
  name: string;
  member_count: number;
}
export const getTeams = () => request<{ teams: Team[] }>('/org/teams');
export const createTeam = (name: string) =>
  request<Team>('/org/teams', { method: 'POST', body: JSON.stringify({ name }) });
export const setTeamMember = (team: number, principal_id: number, on: boolean) =>
  request<{ status: string }>(`/org/teams/${team}/members`, {
    method: 'POST',
    body: JSON.stringify({ principal_id, on }),
  });

export interface AuditEvent {
  seq: number;
  at: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  outcome: string;
  actor_role: string | null;
  actor: string | null;
  detail: Record<string, unknown>;
}
export const getAuditEvents = (action?: string, before?: number) => {
  const q = new URLSearchParams({ limit: '50' });
  if (action) q.set('action', action);
  if (before) q.set('before_seq', String(before));
  return request<{ all: boolean; events: AuditEvent[] }>(`/org/audit?${q.toString()}`);
};
export interface ChainCheck {
  ok: boolean;
  checked: number;
  first_bad_seq: number | null;
  head_seq: number | null;
  head_hash: string | null;
}
export const verifyAudit = () => request<ChainCheck>('/org/audit/verify');
export const AUDIT_EXPORT_URL = '/api/org/audit/export';

// ---- daily reports ----

export interface ReportLink {
  kind: 'decision' | 'pr' | 'doc' | 'url';
  ref: string;
  title?: string | undefined;
}
export interface Report {
  report_id: number;
  author_principal: number;
  author: string;
  date: string;
  done: string;
  next: string;
  blockers: string;
  links: ReportLink[];
  updated_at: string;
  comments: number;
}
export interface MyReport {
  date: string;
  report: Report | null;
  editable: boolean;
  window_days: number;
}
export interface ReportBodyIn {
  done: string;
  next: string;
  blockers: string;
  links: ReportLink[];
}
export const getMyReport = (date: string) => request<MyReport>(`/reports/mine?date=${date}`);
export const putReport = (date: string, body: ReportBodyIn) =>
  request<{ report_id: number; created: boolean }>(`/reports/${date}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
export const getReportFeed = (start: string, end: string) =>
  request<{ start: string; end: string; scope: string; reports: Report[] }>(
    `/reports?start=${start}&end=${end}`,
  );
export interface ReportDetail {
  report: Report;
  comments: { comment_id: number; author: string; body: string; at: string }[];
}
export const getReport = (id: number) => request<ReportDetail>(`/reports/${id}`);
export const commentOnReport = (id: number, body: string) =>
  request<{ comment_id: number }>(`/reports/${id}/comments`, {
    method: 'POST',
    body: JSON.stringify({ body }),
  });
export interface Rollup {
  start: string;
  end: string;
  people: number;
  days: { date: string; expected: number; submitted: number; missing: string[] }[];
  reports: number;
  with_blockers: number;
  coverage: number | null;
}
export const getRollup = (start: string, end: string) =>
  request<Rollup>(`/reports/rollup?start=${start}&end=${end}`);
export interface DigestItem {
  text: string;
  reports: number[];
}
export interface Digest {
  empty?: boolean;
  summary: string;
  highlights: DigestItem[];
  blockers: DigestItem[];
  model?: string;
  cached?: boolean;
  reports: { report_id: number; author: string; date: string }[];
}
export const makeDigest = (b: { start: string; end: string; scope: string }) =>
  request<Digest>('/reports/digest', { method: 'POST', body: JSON.stringify(b) });
export const reportsExportUrl = (start: string, end: string) =>
  `/api/reports/export?start=${start}&end=${end}`;

// ---- governance ----

export interface Approval {
  approval_id: number;
  kind: 'decision' | 'policy' | 'access' | 'exception';
  title: string;
  detail: Record<string, unknown>;
  requested_by: string;
  mine: boolean;
  required: number;
  state: 'pending' | 'approved' | 'rejected' | 'withdrawn';
  approvals: number;
  created_at: string;
  decided_at: string | null;
  can_vote: boolean;
  voted: boolean;
}
export const getApprovals = (state: 'pending' | 'all') =>
  request<{ approvals: Approval[] }>(`/governance/approvals?state=${state}`);
export const voteOnApproval = (id: number, vote: 'approve' | 'reject', comment: string) =>
  request<{ state: string }>(`/governance/approvals/${id}/vote`, {
    method: 'POST',
    body: JSON.stringify({ vote, comment }),
  });
export const withdrawApproval = (id: number) =>
  request<{ state: string }>(`/governance/approvals/${id}/withdraw`, { method: 'POST' });
export const requestApproval = (b: {
  kind: 'access' | 'exception';
  title: string;
  role?: string;
  reason: string;
  expires_at?: string;
}) =>
  request<{ approval_id: number }>('/governance/approvals', {
    method: 'POST',
    body: JSON.stringify(b),
  });

export interface ManualDecision {
  decision_id: number;
  key: string;
  title: string;
  question: string | null;
  outcome: string;
  status: string;
  created_at: string;
  proposed_by: string | null;
  approval_id: number | null;
  approval_state: string | null;
  required: number | null;
  approvals: number | null;
}
export const getManualDecisions = () =>
  request<{ decisions: ManualDecision[] }>('/governance/decisions');
export const proposeDecision = (b: {
  title: string;
  question: string;
  outcome: string;
  required: number;
}) =>
  request<{ decision_id: number; key: string; approval_id: number }>('/governance/decisions', {
    method: 'POST',
    body: JSON.stringify(b),
  });
export const markImplemented = (id: number, note: string) =>
  request<{ status: string }>(`/governance/decisions/${id}/implemented`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  });

export interface PolicyRow {
  policy_id: number;
  slug: string;
  title: string;
  state: string;
  version: number | null;
  published_at: string | null;
  attest_by: string | null;
  review_due: string | null;
  review_overdue: boolean;
  attested: number | null;
  expected: number;
  mine: boolean;
  needs_my_attestation: boolean;
  overdue: boolean;
  latest_version_state: string | null;
}
export const getPolicies = () => request<{ policies: PolicyRow[] }>('/governance/policies');
export interface PolicyDetail {
  policy: { policy_id: number; slug: string; title: string; state: string; version: number | null };
  can_edit: boolean;
  versions: {
    version: number;
    title: string;
    body: string;
    summary: string | null;
    state: string;
    published_at: string | null;
    attest_by: string | null;
    author: string;
    approval_id: number | null;
  }[];
  attested: { name: string; at: string }[];
  missing: string[];
}
export const getPolicy = (id: number) => request<PolicyDetail>(`/governance/policies/${id}`);
export const createPolicy = (b: {
  title: string;
  body: string;
  review_days: number;
  attest_days: number;
}) =>
  request<{ policy_id: number }>('/governance/policies', {
    method: 'POST',
    body: JSON.stringify(b),
  });
export const savePolicyDraft = (id: number, b: { title: string; body: string; summary: string }) =>
  request<{ version: number }>(`/governance/policies/${id}/draft`, {
    method: 'PUT',
    body: JSON.stringify(b),
  });
export const submitPolicy = (id: number) =>
  request<{ approval_id: number }>(`/governance/policies/${id}/submit`, { method: 'POST' });
export const attestPolicy = (id: number) =>
  request<{ attested: boolean }>(`/governance/policies/${id}/attest`, { method: 'POST' });

export interface Risk {
  risk_id: number;
  title: string;
  description: string;
  category: string;
  likelihood: number;
  impact: number;
  score: number;
  state: string;
  mitigation: string;
  review_by: string | null;
  owner: string;
  can_edit: boolean;
  review_overdue: boolean;
}
export const getRisks = () => request<{ risks: Risk[] }>('/governance/risks');
export const addRisk = (b: {
  title: string;
  description: string;
  category: string;
  likelihood: number;
  impact: number;
  mitigation: string;
}) =>
  request<{ risk_id: number }>('/governance/risks', { method: 'POST', body: JSON.stringify(b) });
export const patchRisk = (id: number, b: { state?: string; mitigation?: string }) =>
  request<{ status: string }>(`/governance/risks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(b),
  });

export interface GovSummary {
  approvals_waiting_for_me: number;
  approvals_pending: number;
  policies_to_attest: number;
  high_risks: number | null;
  open_risks: number | null;
}
export const getGovSummary = () => request<GovSummary>('/governance/summary');
