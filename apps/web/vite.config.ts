import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// `pnpm dev` on the host proxies /api to the compose API, as nginx does in the web image.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/setup-tests.ts'],
    include: ['src/**/*.test.{ts,tsx}', 'functions/**/*.test.ts'],
  },
});
