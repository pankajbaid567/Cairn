import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { createQueryClient } from './query-client';

const defaultClient = createQueryClient();

/** App-wide providers. Rendered once at the root in main.tsx. */
export function AppProviders({
  children,
  client = defaultClient,
}: {
  children: ReactNode;
  client?: QueryClient;
}) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
