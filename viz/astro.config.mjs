import { defineConfig } from 'astro/config';
import svelte from '@astrojs/svelte';

export default defineConfig({
  integrations: [svelte()],
  trailingSlash: 'ignore',
  build: { format: 'directory' },
  devToolbar: { enabled: false },
  vite: {
    // Pre-bundle the map stack up front: discovering these lazily makes the dev server
    // re-optimize mid-session and answer 504 "Outdated Optimize Dep" until a reload.
    optimizeDeps: {
      include: [
        'maplibre-gl',
        '@deck.gl/core',
        '@deck.gl/layers',
        '@deck.gl/geo-layers',
        '@deck.gl/mapbox',
        '@deck.gl/extensions',
      ],
    },
  },
});
