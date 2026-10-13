import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(() => ({
  plugins: [react()],
  server:
    process.env.E2E_BACKEND === "1"
      ? {
          host: "127.0.0.1",
          port: 5174,
          strictPort: true,
          proxy: {
            "/api": "http://127.0.0.1:18000",
            "/ws": {
              target: "ws://127.0.0.1:18000",
              ws: true,
            },
          },
        }
      : {
          port: 5173,
          strictPort: true,
          proxy: {
            "/api": "http://localhost:8000",
            "/ws": {
              target: "ws://localhost:8000",
              ws: true,
            },
          },
        },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
}));
