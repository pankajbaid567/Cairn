// Pages Function for every /api/* request (public/_routes.json limits Functions to that prefix).
// The logic lives in ../_lib/proxy.ts so vitest can test it without the Pages runtime.
import { proxyToApi, type ProxyEnv } from '../_lib/proxy';

export const onRequest = (context: { request: Request; env: ProxyEnv }): Promise<Response> =>
  proxyToApi(context.request, context.env);
