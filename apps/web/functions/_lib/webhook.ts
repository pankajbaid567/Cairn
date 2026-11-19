// GitHub webhook receiver for the Pages Function at /webhooks/github (plan 3.4, 3.16).
//
// Same contract as packages/core/src/cairn_core/webhook.py, checked against the same signed
// fixtures (tests/fixtures/webhooks, mirrored under ./fixtures): verify the HMAC-SHA256
// signature in constant time, classify the event, store a thin identifiers-only row keyed by
// delivery id, answer 202, then wake the API. No payload text is ever stored or logged.
//
// The row is written through Neon's SQL-over-HTTP endpoint as the insert-only role cairn_edge,
// so no database driver is bundled. `fetch` and `waitUntil` are injected so vitest runs this
// in Node without the Pages runtime.

export interface WebhookEnv {
  GITHUB_WEBHOOK_SECRET?: string | undefined;
  /** https://<neon host>/sql */
  NEON_SQL_URL?: string | undefined;
  /** postgresql://cairn_edge:<password>@<host>/<db>?sslmode=require (a Pages secret) */
  NEON_EDGE_CONNECTION?: string | undefined;
  /** The Render service origin, woken with GET /healthz after the response. */
  API_ORIGIN?: string | undefined;
}

export interface WebhookDeps {
  fetch: typeof fetch;
  waitUntil: (promise: Promise<unknown>) => void;
}

export const MAX_BODY_BYTES = 1_048_576;

export const ACL_EVENTS: ReadonlySet<string> = new Set([
  'membership',
  'team',
  'team_add',
  'organization',
  'member',
  'public',
  'repository',
  'installation',
  'installation_repositories',
]);
export const CONTENT_EVENTS: ReadonlySet<string> = new Set([
  'pull_request',
  'pull_request_review',
  'pull_request_review_comment',
  'issues',
  'issue_comment',
  'push',
]);

export type EventClass = 'acl' | 'content';

export interface ThinRow {
  delivery_id: string;
  event: string;
  action: string | null;
  class: EventClass;
  installation_id: number | null;
  org_id: number | null;
  repo_id: number | null;
  repo_name: string | null;
  object_number: number | null;
}

const REPO = /^[A-Za-z0-9._-]{1,100}\/[A-Za-z0-9._-]{1,100}$/;
const WORD = /^[a-z_]{1,40}$/;
const DELIVERY = /^[A-Za-z0-9._:-]{1,100}$/;

export function classify(event: string): EventClass | null {
  if (ACL_EVENTS.has(event)) return 'acl';
  if (CONTENT_EVENTS.has(event)) return 'content';
  return null;
}

function hexToBytes(hex: string): Uint8Array<ArrayBuffer> | null {
  if (hex.length % 2 !== 0 || !/^[0-9a-f]*$/i.test(hex)) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) out[i] = Number.parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

/** Constant-time check: `crypto.subtle.verify` compares the MAC without early exit. Fails closed. */
export async function verifySignature(
  secret: string | undefined,
  body: Uint8Array<ArrayBuffer>,
  header: string | null,
): Promise<boolean> {
  if (!secret || !header || !header.startsWith('sha256=')) return false;
  const mac = hexToBytes(header.slice('sha256='.length));
  if (mac === null || mac.length !== 32) return false;
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['verify'],
  );
  return crypto.subtle.verify('HMAC', key, mac, body);
}

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (typeof v === 'object' && v !== null ? (v as Obj) : {});
const int = (v: unknown): number | null =>
  typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null;

/** The inbox row for a verified delivery, or null when the event is unused or malformed. */
export function thinRow(headers: Headers, payload: unknown): ThinRow | null {
  const event = headers.get('x-github-event') ?? '';
  const delivery = headers.get('x-github-delivery') ?? '';
  const klass = classify(event);
  if (klass === null || !WORD.test(event) || !DELIVERY.test(delivery)) return null;
  const p = obj(payload);
  const repo = obj(p['repository']);
  const name = repo['full_name'];
  const number =
    p['number'] ?? obj(p['issue'])['number'] ?? obj(p['pull_request'])['number'] ?? null;
  const action = p['action'];
  return {
    delivery_id: delivery,
    event,
    action: typeof action === 'string' && WORD.test(action) ? action : null,
    class: klass,
    installation_id: int(obj(p['installation'])['id']),
    org_id: int(obj(p['organization'])['id']),
    repo_id: int(repo['id']),
    repo_name: typeof name === 'string' && REPO.test(name) ? name : null,
    object_number: int(number),
  };
}

const INSERT_SQL =
  'INSERT INTO cairn.inbox (delivery_id, event, action, class, priority, installation_id, org_id, ' +
  'repo_id, repo_name, object_number) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) ' +
  'ON CONFLICT DO NOTHING';

function json(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  });
}

async function insertRow(env: WebhookEnv, row: ThinRow, doFetch: typeof fetch): Promise<boolean> {
  if (!env.NEON_SQL_URL || !env.NEON_EDGE_CONNECTION) return false;
  const params = [
    row.delivery_id,
    row.event,
    row.action,
    row.class,
    row.class === 'acl' ? 0 : 1,
    row.installation_id,
    row.org_id,
    row.repo_id,
    row.repo_name,
    row.object_number,
  ];
  try {
    const response = await doFetch(env.NEON_SQL_URL, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'neon-connection-string': env.NEON_EDGE_CONNECTION,
      },
      body: JSON.stringify({ query: INSERT_SQL, params }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** Receive one delivery. Answers 202 only after the row is durably stored (or was already there). */
export async function receiveWebhook(
  request: Request,
  env: WebhookEnv,
  deps: WebhookDeps,
): Promise<Response> {
  if (request.method !== 'POST') return json(405, { error: 'method_not_allowed' });
  if (!env.GITHUB_WEBHOOK_SECRET) return json(500, { error: 'misconfigured' });
  const declared = Number.parseInt(request.headers.get('content-length') ?? '0', 10);
  if (declared > MAX_BODY_BYTES) return json(413, { error: 'too_large' });
  const body = new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > MAX_BODY_BYTES) return json(413, { error: 'too_large' });
  const signature = request.headers.get('x-hub-signature-256');
  if (!(await verifySignature(env.GITHUB_WEBHOOK_SECRET, body, signature))) {
    return json(401, { error: 'bad_signature' });
  }
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return json(400, { error: 'bad_json' });
  }
  const row = thinRow(request.headers, payload);
  if (row === null) return json(202, { status: 'ignored' });
  if (!(await insertRow(env, row, deps.fetch))) return json(500, { error: 'store_failed' });
  if (env.API_ORIGIN) {
    // After the response: wake the sleeping API so it drains before any query is answered.
    deps.waitUntil(
      deps.fetch(`${env.API_ORIGIN.replace(/\/+$/, '')}/healthz`).catch(() => undefined),
    );
  }
  return json(202, { status: 'accepted' });
}
