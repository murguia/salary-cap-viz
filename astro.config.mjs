// @ts-check
import { defineConfig } from 'astro/config';

// GitHub Pages config.
// If this deploys as a *project* page (username.github.io/salary-cap-viz),
// keep `base` set to the repo name. If it later gets a custom domain/subdomain
// (e.g. linked from murguia.org at the root), set base to '/' and update `site`.
export default defineConfig({
  site: 'https://murguia.org',
  base: '/salary-cap-viz',
  trailingSlash: 'ignore',
});
