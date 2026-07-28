import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base "./" so the built app works from any subpath (e.g. GitHub Pages)
export default defineConfig({
  plugins: [react()],
  base: "./",
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{js,jsx}"],
  },
});
