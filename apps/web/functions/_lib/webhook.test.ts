// @vitest-environment node
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { receiveWebhook, thinRow, verifySignature, type WebhookEnv } from './webhook';

const DIR = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'webhooks');
interface Fixture {
  name: string;
  headers: Record<string, string>;
  body: string;
  expected: Record<string, unknown> | null;
}
const index = JSON.parse(readFileSync(join(DIR, 'index.json'), 'utf8')) as {
  secret: string;
  fixtures: { name: string; file: string }[];
};
const fixtures: Fixture[] = index.fixtures.map(
  (f) => JSON.parse(readFileSync(join(DIR, f.file), 'utf8')) as Fixture,
);
const SECRET = index.secret;

const ENV: WebhookEnv = {
  GITHUB_WEBHOOK_SECRET: SECRET,
  NEON_SQL_URL: 'https://neon.example.test/sql',
  NEON_EDGE_CONNECTION: 'postgresql://cairn_edge:pw@neon.example.test/cairn',
  API_ORIGIN: 'https://api.example.test',
};

function request(f: Fixture, headers: Record<string, string> = {}, method = 'POST'): Request {
  return new Request('https://cairn.example.test/webhooks/github', {
    method,
    headers: { ...f.headers, ...headers },
    ...(method === 'POST' ? { body: f.body } : {}),
  });
}

/** Records the Neon insert and the wake call; answers the insert with `neonStatus`. */
function harness(neonStatus = 200) {
  const calls: { url: string; init?: RequestInit | undefined }[] = [];
  const woken: Promise<unknown>[] = [];
  const doFetch = ((url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return Promise.resolve(
      new Response('{}', { status: String(url).endsWith('/sql') ? neonStatus : 200 }),
    );
  }) as typeof fetch;
  return {
    calls,
    woken,
    deps: { fetch: doFetch, waitUntil: (p: Promise<unknown>) => void woken.push(p) },
  };
}

describe('the shared signed fixtures', () => {
  it('has the fixtures the Python side wrote', () => {
    expect(readdirSync(DIR).filter((n) => n.endsWith('.json')).length).toBe(fixtures.length + 1);
  });

  for (const f of fixtures) {
    it(`${f.name}: the signature verifies and the thin row equals the Python one`, async () => {
      const body = new TextEncoder().encode(f.body);
      expect(await verifySignature(SECRET, body, f.headers['x-hub-signature-256'] ?? null)).toBe(
        true,
      );
      const row = thinRow(new Headers(f.headers), JSON.parse(f.body));
      expect(row).toEqual(f.expected);
    });
  }
});

describe('verifySignature', () => {
  const f = fixtures[0] as Fixture;
  const body = new TextEncoder().encode(f.body);
  const good = f.headers['x-hub-signature-256'] as string;

  it('rejects a wrong secret, a flipped digit, a short MAC, a missing prefix and no header', async () => {
    expect(await verifySignature('another-secret', body, good)).toBe(false);
    const last = good.slice(-1) === '0' ? '1' : '0';
    expect(await verifySignature(SECRET, body, good.slice(0, -1) + last)).toBe(false);
    expect(await verifySignature(SECRET, body, 'sha256=abcd')).toBe(false);
    expect(await verifySignature(SECRET, body, good.replace('sha256=', 'sha1='))).toBe(false);
    expect(await verifySignature(SECRET, body, null)).toBe(false);
    expect(await verifySignature('', body, good)).toBe(false);
    expect(await verifySignature(SECRET, body, 'sha256=zz' + good.slice(9))).toBe(false);
  });
});

describe('receiveWebhook', () => {
  const acl = fixtures.find((f) => f.name === 'membership_removed') as Fixture;

  it('stores a thin row, answers 202 and wakes the API after the response', async () => {
    const h = harness();
    const response = await receiveWebhook(request(acl), ENV, h.deps);
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ status: 'accepted' });
    const insert = h.calls[0] as { url: string; init: RequestInit };
    expect(insert.url).toBe('https://neon.example.test/sql');
    const sent = JSON.parse(String(insert.init.body)) as { query: string; params: unknown[] };
    expect(sent.query).toContain('ON CONFLICT DO NOTHING');
    expect(sent.params[0]).toBe(acl.headers['x-github-delivery']);
    expect(sent.params[4]).toBe(0); // ACL class drains first
    expect(JSON.stringify(sent)).not.toContain('"member"'); // no payload text is stored
    expect((insert.init.headers as Record<string, string>)['neon-connection-string']).toContain(
      'cairn_edge',
    );
    await Promise.all(h.woken);
    expect(h.calls.at(-1)?.url).toBe('https://api.example.test/healthz');
  });

  it('never stores comment text', async () => {
    const f = fixtures.find((x) => x.name === 'issue_comment_created') as Fixture;
    const h = harness();
    expect((await receiveWebhook(request(f), ENV, h.deps)).status).toBe(202);
    expect(String((h.calls[0] as { init: RequestInit }).init.body)).not.toContain('private text');
  });

  it('a bad signature gets 401 and nothing is stored or woken', async () => {
    const h = harness();
    const r = await receiveWebhook(
      request(acl, { 'x-hub-signature-256': 'sha256=' + '0'.repeat(64) }),
      ENV,
      h.deps,
    );
    expect(r.status).toBe(401);
    expect(h.calls).toHaveLength(0);
  });

  it('an unused event or ping is acknowledged without a write', async () => {
    for (const name of ['star_ignored', 'ping_ignored']) {
      const h = harness();
      const r = await receiveWebhook(
        request(fixtures.find((f) => f.name === name) as Fixture),
        ENV,
        h.deps,
      );
      expect(r.status).toBe(202);
      expect(await r.json()).toEqual({ status: 'ignored' });
      expect(h.calls).toHaveLength(0);
    }
  });

  it('a duplicate delivery is a no-op for the caller: the same 202 both times', async () => {
    const h = harness();
    expect((await receiveWebhook(request(acl), ENV, h.deps)).status).toBe(202);
    expect((await receiveWebhook(request(acl), ENV, h.deps)).status).toBe(202);
  });

  it('answers 500 when the database write fails, so GitHub marks it failed and redelivery can recover', async () => {
    const r = await receiveWebhook(request(acl), ENV, harness(503).deps);
    expect(r.status).toBe(500);
  });

  it('fails closed without a secret, on a non-POST, on an oversized body and on bad JSON', async () => {
    const h = harness();
    expect(
      (await receiveWebhook(request(acl), { ...ENV, GITHUB_WEBHOOK_SECRET: undefined }, h.deps))
        .status,
    ).toBe(500);
    expect((await receiveWebhook(request(acl, {}, 'GET'), ENV, h.deps)).status).toBe(405);
    expect(
      (await receiveWebhook(request(acl, { 'content-length': '2000000' }), ENV, h.deps)).status,
    ).toBe(413);
    const bad: Fixture = { ...acl, body: '{not json' };
    const body = new TextEncoder().encode(bad.body);
    const key = await crypto.subtle.importKey(
      'raw',
      new TextEncoder().encode(SECRET),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign'],
    );
    const mac = new Uint8Array(await crypto.subtle.sign('HMAC', key, body));
    const hex = [...mac].map((b) => b.toString(16).padStart(2, '0')).join('');
    expect(
      (await receiveWebhook(request(bad, { 'x-hub-signature-256': `sha256=${hex}` }), ENV, h.deps))
        .status,
    ).toBe(400);
    expect(h.calls).toHaveLength(0);
  });

  it('handles the largest fixture quickly (a proxy for the CPU limit; the real one is measured on Cloudflare)', async () => {
    const f = fixtures.find((x) => x.name === 'push_large') as Fixture;
    const t0 = performance.now();
    const r = await receiveWebhook(request(f), ENV, harness().deps);
    expect(r.status).toBe(202);
    expect(performance.now() - t0).toBeLessThan(250);
  });
});
