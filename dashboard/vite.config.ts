/// <reference types="node" />
import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Static base path matches FastAPI's StaticFiles mount at /admin/ui — the
// server serves index.html for any deep link, so SPA routing works even on
// first-page deep-link hits.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/admin/ui/',
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
    proxy: {
      '/admin': 'http://localhost:8711',
      '/health': 'http://localhost:8711',
      '/containers': 'http://localhost:8711',
      '/search': 'http://localhost:8711',
      '/jobs': 'http://localhost:8711',
      '/index-status': 'http://localhost:8711',
    },
  },
});
