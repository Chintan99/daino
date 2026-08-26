import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Daino frontend build config.
// - base "./" so built assets resolve when FastAPI serves dist/ from "/".
// - dev proxy forwards /api and /ws to the local backend (default port 4173).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:4173",
        changeOrigin: true,
      },
      "/ws": {
        target: "http://127.0.0.1:4173",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
