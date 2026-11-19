// Pages Function for POST /webhooks/github (public/_routes.json routes this prefix to Functions).
// The logic lives in ../_lib/webhook.ts so vitest can test it without the Pages runtime.
import { receiveWebhook, type WebhookEnv } from '../_lib/webhook';

export const onRequest = (context: {
  request: Request;
  env: WebhookEnv;
  waitUntil: (promise: Promise<unknown>) => void;
}): Promise<Response> =>
  receiveWebhook(context.request, context.env, {
    fetch: (input, init) => fetch(input, init),
    waitUntil: context.waitUntil,
  });
