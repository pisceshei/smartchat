// Chat app build — dist/chat/* (index.html + hashed assets). Runs inside the
// iframe the loader creates. Preact via esbuild automatic JSX (no babel).
import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  esbuild: {
    jsx: "automatic",
    jsxImportSource: "preact",
  },
  build: {
    outDir: "dist/chat",
    emptyOutDir: true,
    target: "es2018",
    minify: "esbuild",
  },
  server: {
    port: 5175,
  },
});
