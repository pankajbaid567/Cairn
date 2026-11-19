// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  HOP_BY_HOP,
  MAX_BODY_BYTES,
  UPSTREAM_TIMEOUT_MS,
  forwardHeaders,
  parseApiOrigin,
  proxyToApi,
  upstreamUrl,
  type ProxyEnv,
} from './proxy';

const ORIGIN = 'https://cairn-api.example.test';
const TOKEN = 'proxy-token-for-tests-0123456789abcdef';
const ENV: ProxyEnv = { API_ORIGIN: ORIGIN, CAIRN_PROXY_TOKEN: TOKEN };
const PAGES = 'https://cairn.example.test';

interface Captured {
  url: string;
  init: RequestInit;
}

/** A fetch that records its call and answers with `respond`. */
function recorder(respond: () => Response = () => new Response('ok')) {
  const calls: Captured[] = [];
  const fetchImpl = ((url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(respond());
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function forwarded(calls: Captured[]): Headers {
  const first = calls[0];
  if (first === undefined) throw new Error('no upstream call');
  return new Headers(first.init.headers);
}

afterEach(() => {
  vi.useRealTimers();
});

describe('upstreamUrl', () => {
  it.each([
    ['/api/readyz', `${ORIGIN}/readyz`],
    ['/api/readyz?x=1&y=a%20b', `${ORIGIN}/readyz?x=1&y=a%20b`],
    ['/api/v1/items/42?limit=10', `${ORIGIN}/v1/items/42?limit=10`],
    ['/api/', `${ORIGIN}/`],
    ['/api/a/b/', `${ORIGIN}/a/b/`],
    ['/api//evil.example/x', `${ORIGIN}//evil.example/x`],
    ['/api/%2e%2e/x', null],
    ['/api/../x', null],
    ['/api', null],
    ['/apix/readyz', null],
    ['/', null],
    ['/index.html', null],
    ['/other/api/readyz', null],
  ])('maps %s to %s', (path, expected) => {
    const result = upstreamUrl(`${PAGES}${path}`, ORIGIN);
    expect(result === null ? null : result.toString()).toBe(expected);
  });

  it('keeps a protocol-relative looking path on the API host', () => {
    const result = upstreamUrl(`${PAGES}/api//evil.example/x`, ORIGIN);
    expect(result?.host).toBe('cairn-api.example.test');
  });

  it.each([
    ['', null],
    ['not a url', null],
    ['http://cairn-api.example.test', null],
    ['https://user:pw@cairn-api.example.test', null],
    ['https://cairn-api.example.test/base', null],
    ['https://cairn-api.example.test/?q=1', null],
    ['http://127.0.0.1:8000', 'http://127.0.0.1:8000'],
    ['https://cairn-api.example.test/', ORIGIN],
  ])('validates API_ORIGIN %j', (value, expected) => {
    const parsed = parseApiOrigin(value);
    expect(parsed === null ? null : parsed.origin).toBe(expected);
  });
});

describe('forwardHeaders', () => {
  it('drops hop-by-hop headers and the client token, keeps the rest', () => {
    const incoming = new Headers({
      accept: 'application/json',
      cookie: 'session=abc',
      authorization: 'Bearer user',
      'content-type': 'application/json',
      connection: 'keep-alive, x-custom-hop',
      'keep-alive': 'timeout=5',
      'x-custom-hop': 'secret',
      te: 'trailers',
      trailer: 'x',
      'transfer-encoding': 'chunked',
      upgrade: 'websocket',
      'proxy-authorization': 'Basic zzz',
      'proxy-foo': 'bar',
      host: 'cairn.example.test',
      'x-cairn-proxy-token': 'client-supplied',
    });
    const out = forwardHeaders(incoming, TOKEN, 'cairn.example.test');
    for (const name of HOP_BY_HOP) {
      if (name !== 'x-cairn-proxy-token') expect(out.has(name)).toBe(false);
    }
    expect(out.has('proxy-foo')).toBe(false);
    expect(out.has('x-custom-hop')).toBe(false);
    expect(out.get('x-cairn-proxy-token')).toBe(TOKEN);
    expect(out.get('accept')).toBe('application/json');
    expect(out.get('cookie')).toBe('session=abc');
    expect(out.get('authorization')).toBe('Bearer user');
    expect(out.get('content-type')).toBe('application/json');
  });

  it('sets the X-Forwarded-* headers and ignores client-supplied copies', () => {
    const incoming = new Headers({
      'cf-connecting-ip': '203.0.113.7',
      'x-forwarded-for': '198.51.100.1',
      'x-forwarded-proto': 'http',
      'x-forwarded-host': 'evil.example',
    });
    const out = forwardHeaders(incoming, TOKEN, 'cairn.example.test');
    expect(out.get('x-forwarded-for')).toBe('203.0.113.7');
    expect(out.get('x-forwarded-proto')).toBe('https');
    expect(out.get('x-forwarded-host')).toBe('cairn.example.test');
  });

  it('omits X-Forwarded-For when cf-connecting-ip is absent', () => {
    const out = forwardHeaders(new Headers({ 'x-forwarded-for': '198.51.100.1' }), TOKEN, 'h.test');
    expect(out.has('x-forwarded-for')).toBe(false);
  });

  it('does not copy content-length (fetch computes it)', () => {
    const out = forwardHeaders(new Headers({ 'content-length': '3' }), TOKEN, 'h.test');
    expect(out.has('content-length')).toBe(false);
  });
});

describe('proxyToApi: configuration and routing', () => {
  it.each<[string, ProxyEnv]>([
    ['no API_ORIGIN', { CAIRN_PROXY_TOKEN: TOKEN }],
    ['empty API_ORIGIN', { API_ORIGIN: '', CAIRN_PROXY_TOKEN: TOKEN }],
    [
      'invalid API_ORIGIN',
      { API_ORIGIN: 'http://cairn-api.example.test', CAIRN_PROXY_TOKEN: TOKEN },
    ],
    ['no token', { API_ORIGIN: ORIGIN }],
    ['empty token', { API_ORIGIN: ORIGIN, CAIRN_PROXY_TOKEN: '' }],
    ['nothing', {}],
  ])('answers 503 proxy_not_configured with %s and never fetches', async (_name, env) => {
    const { calls, fetchImpl } = recorder();
    const response = await proxyToApi(new Request(`${PAGES}/api/readyz`), env, fetchImpl);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: 'proxy_not_configured' });
    expect(calls).toHaveLength(0);
  });

  it.each(['/api', '/index.html', '/apix/readyz', '/api/../secret'])(
    'answers 404 for %s and never fetches',
    async (path) => {
      const { calls, fetchImpl } = recorder();
      const response = await proxyToApi(new Request(`${PAGES}${path}`), ENV, fetchImpl);
      expect(response.status).toBe(404);
      expect(await response.json()).toEqual({ error: 'not_found' });
      expect(calls).toHaveLength(0);
    },
  );

  it('strips the /api prefix and keeps the query string', async () => {
    const { calls, fetchImpl } = recorder();
    await proxyToApi(new Request(`${PAGES}/api/readyz?x=1&y=2`), ENV, fetchImpl);
    expect(calls[0]?.url).toBe(`${ORIGIN}/readyz?x=1&y=2`);
  });

  it('adds the token and forwarding headers and drops a client token', async () => {
    const { calls, fetchImpl } = recorder();
    await proxyToApi(
      new Request(`${PAGES}/api/readyz`, {
        headers: {
          'x-cairn-proxy-token': 'client-supplied',
          'cf-connecting-ip': '203.0.113.7',
          cookie: 'session=abc',
        },
      }),
      ENV,
      fetchImpl,
    );
    const headers = forwarded(calls);
    expect(headers.get('x-cairn-proxy-token')).toBe(TOKEN);
    expect(headers.get('x-forwarded-for')).toBe('203.0.113.7');
    expect(headers.get('x-forwarded-proto')).toBe('https');
    expect(headers.get('x-forwarded-host')).toBe('cairn.example.test');
    expect(headers.get('cookie')).toBe('session=abc');
    expect(calls[0]?.init.redirect).toBe('manual');
  });
});

describe('proxyToApi: methods and bodies', () => {
  it.each(['GET', 'HEAD', 'DELETE', 'OPTIONS'])('%s without a body sends none', async (method) => {
    const { calls, fetchImpl } = recorder();
    await proxyToApi(new Request(`${PAGES}/api/items`, { method }), ENV, fetchImpl);
    expect(calls[0]?.init.method).toBe(method);
    expect(calls[0]?.init.body).toBeUndefined();
  });

  it.each(['POST', 'PUT', 'PATCH', 'DELETE'])(
    '%s with a body forwards it unchanged',
    async (method) => {
      const { calls, fetchImpl } = recorder();
      const payload = '{"name":"Kestrel","note":"héllo"}';
      await proxyToApi(
        new Request(`${PAGES}/api/items`, {
          method,
          body: payload,
          headers: { 'content-type': 'application/json' },
        }),
        ENV,
        fetchImpl,
      );
      expect(calls[0]?.init.method).toBe(method);
      const sent = calls[0]?.init.body;
      expect(sent).toBeInstanceOf(Uint8Array);
      expect(new TextDecoder().decode(sent as Uint8Array)).toBe(payload);
      expect(forwarded(calls).get('content-type')).toBe('application/json');
    },
  );

  it.each(['POST', 'PUT', 'PATCH'])('%s without a body sends none', async (method) => {
    const { calls, fetchImpl } = recorder();
    await proxyToApi(new Request(`${PAGES}/api/items`, { method }), ENV, fetchImpl);
    expect(calls[0]?.init.method).toBe(method);
    expect(calls[0]?.init.body).toBeUndefined();
  });

  it('accepts a body of exactly 1 MiB', async () => {
    const { calls, fetchImpl } = recorder();
    const body = new Uint8Array(MAX_BODY_BYTES).fill(97);
    const response = await proxyToApi(
      new Request(`${PAGES}/api/items`, { method: 'POST', body }),
      ENV,
      fetchImpl,
    );
    expect(response.status).toBe(200);
    expect((calls[0]?.init.body as Uint8Array).byteLength).toBe(MAX_BODY_BYTES);
  });

  it('answers 413 when Content-Length is over 1 MiB, without reading further', async () => {
    const { calls, fetchImpl } = recorder();
    const response = await proxyToApi(
      new Request(`${PAGES}/api/items`, {
        method: 'POST',
        body: 'x',
        headers: { 'content-length': String(MAX_BODY_BYTES + 1) },
      }),
      ENV,
      fetchImpl,
    );
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({ error: 'payload_too_large' });
    expect(calls).toHaveLength(0);
  });

  it('answers 413 when a body without Content-Length grows past 1 MiB', async () => {
    const { calls, fetchImpl } = recorder();
    const chunk = new Uint8Array(MAX_BODY_BYTES / 2 + 1);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(chunk);
        controller.enqueue(chunk);
        controller.close();
      },
    });
    const request = new Request(`${PAGES}/api/items`, {
      method: 'POST',
      body: stream,
      duplex: 'half',
    } as RequestInit);
    const response = await proxyToApi(request, ENV, fetchImpl);
    expect(response.status).toBe(413);
    expect(calls).toHaveLength(0);
  });
});

describe('proxyToApi: upstream responses', () => {
  it('returns status, headers including Set-Cookie, and body unchanged', async () => {
    const upstream = new Response('{"ready":true}', {
      status: 201,
      headers: { 'content-type': 'application/json', 'x-request-id': 'r-1' },
    });
    upstream.headers.append('set-cookie', 'a=1; Path=/; HttpOnly');
    upstream.headers.append('set-cookie', 'b=2; Path=/; Secure');
    const { fetchImpl } = recorder(() => upstream);
    const response = await proxyToApi(new Request(`${PAGES}/api/readyz`), ENV, fetchImpl);
    expect(response.status).toBe(201);
    expect(response.headers.get('x-request-id')).toBe('r-1');
    expect(response.headers.getSetCookie()).toEqual([
      'a=1; Path=/; HttpOnly',
      'b=2; Path=/; Secure',
    ]);
    expect(await response.text()).toBe('{"ready":true}');
  });

  it.each([404, 500, 503])('passes a %i from the API through', async (status) => {
    const { fetchImpl } = recorder(() => new Response('{"error":"x"}', { status }));
    const response = await proxyToApi(new Request(`${PAGES}/api/readyz`), ENV, fetchImpl);
    expect(response.status).toBe(status);
    expect(await response.text()).toBe('{"error":"x"}');
  });

  it('does not follow redirects and returns them as they are', async () => {
    const { fetchImpl } = recorder(
      () => new Response(null, { status: 302, headers: { location: '/elsewhere' } }),
    );
    const response = await proxyToApi(new Request(`${PAGES}/api/x`), ENV, fetchImpl);
    expect(response.status).toBe(302);
    expect(response.headers.get('location')).toBe('/elsewhere');
  });

  it('answers 503 api_unavailable with Retry-After when the fetch rejects', async () => {
    const fetchImpl = (() =>
      Promise.reject(new TypeError('network down'))) as unknown as typeof fetch;
    const response = await proxyToApi(new Request(`${PAGES}/api/readyz`), ENV, fetchImpl);
    expect(response.status).toBe(503);
    expect(response.headers.get('retry-after')).toBe('30');
    expect(await response.json()).toEqual({ error: 'api_unavailable' });
  });

  it('answers 503 api_unavailable when the upstream does not answer in 90 seconds', async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | null | undefined;
    const fetchImpl = ((_url: string, init: RequestInit) => {
      signal = init.signal;
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => {
          reject(new DOMException('aborted', 'AbortError'));
        });
      });
    }) as unknown as typeof fetch;
    const pending = proxyToApi(new Request(`${PAGES}/api/readyz`), ENV, fetchImpl);
    await vi.advanceTimersByTimeAsync(UPSTREAM_TIMEOUT_MS - 1);
    expect(signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    const response = await pending;
    expect(signal?.aborted).toBe(true);
    expect(response.status).toBe(503);
    expect(response.headers.get('retry-after')).toBe('30');
    expect(await response.json()).toEqual({ error: 'api_unavailable' });
  });

  it('never puts the token in an error body or header', async () => {
    const fetchImpl = (() => Promise.reject(new Error(`boom ${TOKEN}`))) as unknown as typeof fetch;
    const response = await proxyToApi(new Request(`${PAGES}/api/readyz`), ENV, fetchImpl);
    const dump = JSON.stringify([...response.headers.entries()]) + (await response.text());
    expect(dump).not.toContain(TOKEN);
  });
});
