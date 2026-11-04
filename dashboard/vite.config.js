// SPDX-License-Identifier: MIT
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// built into dist/, which mcu-admin init installs next to api.py (ui/)
export default defineConfig({
  plugins: [svelte()],
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 300 },
});
