// Same-origin proxy from the Pages Function to the Cairn API (free-tier design §14, FT-15, FT-16).
//
// The browser calls /api/<path> on the Pages domain. This module forwards it to
// API_ORIGIN/<path> with the shared secret header the API checks, so session cookies stay
// same-origin and the public onrender.com URL answers nothing but its liveness probe.
//
// Only standard Fetch API types are used, so the same code runs in the Pages runtime and in
// Node 24 under vitest. Nothing here logs: the token and the request contents never leave
// the response path.

export interface ProxyEnv {
  API_ORIGIN?: string | undefined;
  CAIRN_PROXY_TOKEN?: string | undefined;
}

/** nginx `client_max_body_size 1m` in the local web image (Requirement 17.9). */
export const MAX_BODY_BYTES = 1_048_576;
/** The Render wake takes about 60 s (FV6); Cloudflare gives up on a proxied origin after 100 s. */
export const UPSTREAM_TIMEOUT_MS = 90_000;
export const RETRY_AFTER_SECONDS = 30;
export const PROXY_TOKEN_HEADER = 'x-cairn-proxy-token';

/**
 * Request headers never forwarded. `host` is replaced by the upstream host, the hop-by-hop
 * set is RFC 9110 section 7.6.1 plus the legacy `proxy-connection`, and the token header is
 * dropped so a client cannot supply its own. Names starting with `proxy-` are also dropped
 * (see `isHopByHop`), and so is every header named in the `Connection` header.
 */
export const HOP_BY_HOP: ReadonlySet<string> = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'proxy-connection',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
  'host',
  PROXY_TOKEN_HEADER,
]);

/** Headers the proxy sets itself, so any client-supplied copy is dropped first. */
const FORWARDED_HEADERS: ReadonlySet<string> = new Set([
  'x-forwarded-for',
  'x-forwarded-proto',
  'x-forwarded-host',
  // fetch computes the length of the buffered body; a copied value could disagree with it.
  'content-length',
]);

const BODYLESS_METHODS: ReadonlySet<string> = new Set(['GET', 'HEAD']);

function isHopByHop(name: string): boolean {
  return HOP_BY_HOP.has(name) || name.startsWith('proxy-');
}

/** Validates API_ORIGIN: https, or http only for 127.0.0.1 (tests); no credentials, path, query or fragment. */
export function parseApiOrigin(apiOrigin: string | undefined): URL | null {
  if (apiOrigin === undefined || apiOrigin.trim() === '') return null;
  let origin: URL;
  try {
    origin = new URL(apiOrigin.trim());
  } catch {
    return null;
  }
  const secure = origin.protocol === 'https:';
  const loopback = origin.protocol === 'http:' && origin.hostname === '127.0.0.1';
  if (!secure && !loopback) return null;
  if (origin.username !== '' || origin.password !== '') return null;
  if (origin.pathname !== '/' || origin.search !== '' || origin.hash !== '') return null;
  return origin;
}

/**
 * "/api/readyz?x=1" -> "<origin>/readyz?x=1". Returns null for "/api", for paths outside
 * "/api/", and for an invalid origin. The target is built by string concatenation on the
 * origin, never by resolving against it, so a path such as "/api//evil.example/x" stays on
 * the API host. The request URL is already normalised by the URL parser, which resolves
 * "." and ".." segments (including the percent-encoded forms) before the prefix is checked.
 */
export function upstreamUrl(requestUrl: string, apiOrigin: string): URL | null {
  const origin = parseApiOrigin(apiOrigin);
  if (origin === null) return null;
  let incoming: URL;
  try {
    incoming = new URL(requestUrl);
  } catch {
    return null;
  }
  if (!incoming.pathname.startsWith('/api/')) return null;
  const target = new URL(
    `${origin.origin}${incoming.pathname.slice('/api'.length)}${incoming.search}`,
  );
  return target.origin === origin.origin ? target : null;
}

/**
 * Copies the client headers except the hop-by-hop set, then sets the proxy token and the
 * X-Forwarded-* trio. `x-forwarded-for` comes from `cf-connecting-ip` and is omitted when
 * that header is absent, so a client-supplied value is never passed on. `host` is the host
 * the browser used (from the request URL).
 */
export function forwardHeaders(
  incoming: Headers,
  token: string | undefined,
  host?: string | null,
): Headers {
  const named = new Set(
    (incoming.get('connection') ?? '')
      .split(',')
      .map((part) => part.trim().toLowerCase())
      .filter((part) => part !== ''),
  );
  const out = new Headers();
  incoming.forEach((value, name) => {
    const key = name.toLowerCase();
    if (isHopByHop(key) || FORWARDED_HEADERS.has(key) || named.has(key)) return;
    out.append(name, value);
  });
  if (token !== undefined && token !== '') out.set(PROXY_TOKEN_HEADER, token);
  const client = incoming.get('cf-connecting-ip');
  if (client !== null && client !== '') out.set('x-forwarded-for', client);
  out.set('x-forwarded-proto', 'https');
  const forwardedHost = host ?? incoming.get('host');
  if (forwardedHost !== null && forwardedHost !== '') out.set('x-forwarded-host', forwardedHost);
  return out;
}

function jsonError(status: number, error: string, extra?: Record<string, string>): Response {
  return new Response(JSON.stringify({ error }), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store', ...extra },
  });
}

/** Reads a request body up to `limit` bytes; null when it is larger. */
async function readLimited(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<Uint8Array | null> {
  if (body === null) return new Uint8Array(0);
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return joined;
}

/**
 * Forwards one request to the API.
 *
 * - 503 `proxy_not_configured` when API_ORIGIN (valid) or the token is missing;
 * - 404 `not_found` for "/api" and for paths outside "/api/";
 * - 413 `payload_too_large` when Content-Length, or the body itself, exceeds MAX_BODY_BYTES;
 * - 503 `api_unavailable` with `Retry-After: 30` when the fetch fails or takes longer than
 *   UPSTREAM_TIMEOUT_MS to answer with headers;
 * - otherwise the upstream response unchanged (status, headers including Set-Cookie, body).
 *   Redirects are not followed.
 */
export async function proxyToApi(
  request: Request,
  env: ProxyEnv,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const origin = parseApiOrigin(env.API_ORIGIN);
  const token = env.CAIRN_PROXY_TOKEN;
  if (origin === null || token === undefined || token === '') {
    return jsonError(503, 'proxy_not_configured');
  }
  const target = upstreamUrl(request.url, origin.origin);
  if (target === null) return jsonError(404, 'not_found');

  const declared = request.headers.get('content-length');
  if (declared !== null && /^\d+$/.test(declared) && Number(declared) > MAX_BODY_BYTES) {
    return jsonError(413, 'payload_too_large');
  }
  let body: Uint8Array<ArrayBuffer> | undefined;
  if (!BODYLESS_METHODS.has(request.method)) {
    const read = await readLimited(request.body, MAX_BODY_BYTES);
    if (read === null) return jsonError(413, 'payload_too_large');
    if (read.byteLength > 0) body = new Uint8Array(read);
  }

  const headers = forwardHeaders(request.headers, token, new URL(request.url).host);
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort();
  }, UPSTREAM_TIMEOUT_MS);
  try {
    const init: RequestInit = {
      method: request.method,
      headers,
      redirect: 'manual',
      signal: controller.signal,
    };
    if (body !== undefined) init.body = body;
    return await fetchImpl(target.toString(), init);
  } catch {
    return jsonError(503, 'api_unavailable', { 'retry-after': String(RETRY_AFTER_SECONDS) });
  } finally {
    clearTimeout(timer);
  }
}
