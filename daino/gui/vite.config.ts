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
    // Monaco is ~3.9 MB on its own and is vendored rather than pulled from a
    // CDN (the IDE has to work offline). It is now in its own lazily loaded
    // chunk, so it costs nothing until an editor is opened — but it will always
    // exceed any sane per-chunk limit. Raised just above it so the build stops
    // printing a warning nobody can act on: a warning that fires on every build
    // is a warning people learn to scroll past, which is worse than none.
    chunkSizeWarningLimit: 4_096,
    rollupOptions: {
      output: {
        // The heavy dependencies get their own chunks rather than being melted
        // into one 4.5 MB file. This is not about total bytes — it is about
        // what has to arrive before anything renders, and about caching: the
        // editor and canvas libraries change on their own release cycles, so a
        // change to Daino's own code should not invalidate them.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          // Monaco is by far the largest, and only CODE needs it.
          if (id.includes("monaco-editor") || id.includes("@monaco-editor")) {
            return "monaco";
          }
          if (id.includes("reactflow") || id.includes("@reactflow")) {
            return "reactflow";
          }
          if (id.includes("@xterm")) return "xterm";
          // Markdown rendering pulls in remark/unified and its whole plugin
          // graph; it belongs with the panels that render documents.
          if (
            id.includes("react-markdown") ||
            id.includes("remark") ||
            id.includes("micromark") ||
            id.includes("mdast") ||
            id.includes("unified") ||
            id.includes("hast")
          ) {
            return "markdown";
          }
          if (id.includes("react-dom") || id.includes("/react/") || id.includes("scheduler")) {
            return "react";
          }
          return "vendor";
        },
      },
    },
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
