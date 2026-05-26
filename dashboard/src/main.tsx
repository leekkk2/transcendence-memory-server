import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './styles/globals.css';

// Single QueryClient instance — staleTime defaults to 30s so the dashboard
// doesn't hammer the server on every view switch; specific queries (Overview
// polling) override staleTime to keep their 10s cadence.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename="/admin/ui">
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
