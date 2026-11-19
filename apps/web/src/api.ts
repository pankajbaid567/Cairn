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
    try {
      const body = (await response.json()) as { error?: unknown };
      if (typeof body.error === 'string') code = body.error;
    } catch {
      // not JSON: keep the generic code
    }
    const retry = Number.parseInt(response.headers.get('retry-after') ?? '', 10);
    throw new ApiError(response.status, code, Number.isFinite(retry) ? retry : null);
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
export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Something went wrong. Try again.';
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
