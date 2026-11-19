import { useQueryClient } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';
import { AppProviders } from './providers';
import { createQueryClient } from './query-client';

function ClientProbe() {
  const client = useQueryClient();
  const retry = client.getDefaultOptions().queries?.retry;
  return <p>{retry === false ? 'client ready' : 'unexpected'}</p>;
}

describe('App', () => {
  beforeEach(() => {
    // The readiness widget calls fetch; keep the heading test off the network.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the level-one heading "Cairn"', () => {
    render(
      <AppProviders>
        <App />
      </AppProviders>,
    );
    expect(screen.getByRole('heading', { level: 1, name: 'Cairn' })).toBeInTheDocument();
    expect(screen.getByText(/Permission-aware answers/)).toBeInTheDocument();
  });
});

describe('AppProviders', () => {
  it('makes a QueryClient available to descendants', () => {
    render(
      <AppProviders client={createQueryClient()}>
        <ClientProbe />
      </AppProviders>,
    );
    expect(screen.getByText('client ready')).toBeInTheDocument();
  });

  it('is required: useQueryClient throws without the provider', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(() => render(<ClientProbe />)).toThrow(/No QueryClient set/);
    consoleError.mockRestore();
  });
});
