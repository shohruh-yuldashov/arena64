/** @type {import('postcss-load-config').Config} */
const config = {
  plugins: {
    // Tailwind v4 is CSS-first: no tailwind.config.js and no `content`
    // array. Source scanning and theme tokens both live in
    // src/styles/globals.css via `@import "tailwindcss"` and `@theme`.
    "@tailwindcss/postcss": {},
  },
};

export default config;
