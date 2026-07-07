// Loader build — dist/loader.js. Vanilla TS, no framework, single IIFE file.
// Served by the web tier as /js/project_{widget_key}.js with the literal
// token __WIDGET_KEY__ replaced by the widget key at serve time.
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2018",
    minify: "esbuild",
    lib: {
      entry: "src/loader/index.ts",
      formats: ["iife"],
      name: "SmartChatLoader",
      fileName: () => "loader.js",
    },
  },
});
