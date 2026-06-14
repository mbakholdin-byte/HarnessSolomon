import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite dev server:
//  - :5173 (default)
//  - proxies /api/* → http://localhost:8765 (Solomon Harness FastAPI backend)
//  - ws: true keeps the proxy WebSocket-aware (used by /ws/chat in Step 7)
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8765",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
