import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';

// basename matches Vite config "base" (e.g. "/ui/" in production, "/" in dev)
// When UI is mounted at /ui/ via FastAPI, all routes need this prefix
const basename = import.meta.env.BASE_URL.replace(/\/$/, '');

const root = createRoot(document.getElementById('root')!);
root.render(
  <React.StrictMode>
    <BrowserRouter basename={basename || undefined}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
