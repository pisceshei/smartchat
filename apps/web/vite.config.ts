import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      // realtime gateway runs as a separate service (:8001) — see plan A.8
      "/ws": {
        target: "http://localhost:8001",
        changeOrigin: true,
        ws: true,
      },
      "/widget": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          antd: ["antd"],
          icons: ["@ant-design/icons"],
          data: ["@tanstack/react-query", "zustand", "dayjs"],
        },
      },
    },
  },
});
