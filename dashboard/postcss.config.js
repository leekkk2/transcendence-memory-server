// Tailwind v4 ships a dedicated Vite plugin (see vite.config.ts) so the
// PostCSS pipeline only needs autoprefixer. Keeping the file lets editor
// integrations pick up the standard PostCSS surface for tooling.
export default {
  plugins: {
    autoprefixer: {},
  },
};
