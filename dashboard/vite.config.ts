/// <reference types="node" />
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Single source of truth for the dashboard version — read from package.json at
// build time and exposed as __APP_VERSION__ so the About panel never drifts
// from a hardcoded string.
const pkg = JSON.parse(readFileSync(path.resolve(__dirname, 'package.json'), 'utf-8')) as {
  version: string;
};

// Static base path matches FastAPI's StaticFiles mount at /admin/ui — the
// server serves index.html for any deep link, so SPA routing works even on
// first-page deep-link hits.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/admin/ui/',
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks: {
          charts: ['recharts'],
          query: ['@tanstack/react-query'],
        },
      },
    },
  },
  server: {
    // Dev-only reverse proxy. Target is env-driven so no production hostname
    // is ever hardcoded into this open-source repo (R8). Point it at a live
    // backend for acceptance with `TM_DEV_PROXY_TARGET=https://<host> pnpm dev`;
    // defaults to the local container port for normal development.
    //
    // IMPORTANT: only *specific* API path prefixes are proxied — never the bare
    // `/admin`, because the SPA base is `/admin/ui/` and a broad `/admin` rule
    // would hijack the dashboard's own dev assets (`/admin/ui/src/*`,
    // `/admin/ui/@vite/*`, …) and serve the remote build instead of local code.
    // The three auth routes live under /admin/ui/ but are exact enough not to
    // shadow Vite's static/source middleware.
    proxy: Object.fromEntries(
      [
        '/admin/usage',
        '/admin/profiles',
        '/admin/system-health',
        '/admin/probe-embedding',
        '/admin/config',
        '/admin/tools',
        '/admin/dreaming',
        '/admin/ui/login',
        '/admin/ui/me',
        '/admin/ui/logout',
        '/search',
        '/query',
        '/jobs',
        '/containers',
        '/health',
        '/index-status',
      ].map((p) => [
        p,
        {
          target: process.env.TM_DEV_PROXY_TARGET || 'http://localhost:8711',
          changeOrigin: true,
          secure: false,
          cookieDomainRewrite: '',
        },
      ]),
    ),
  },
});
