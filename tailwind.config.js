/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './templates/**/*.html',
    './app.py',
  ],
  // Keep dynamic classes that may be built via Jinja conditionals/markdown:
  safelist: [
    'btn-primary', 'btn-ghost', 'h-serif', 'eyebrow', 'rule', 'card', 'card-hover',
    'prose-anuvia', 'lang-pill',
  ],
  theme: {
    extend: {
      fontFamily: {
        'serif': ['Playfair Display', 'Georgia', 'serif'],
        'sans': ['Inter', 'system-ui', 'sans-serif'],
      },
      colors: {
        'paper': '#fafaf9',
        'ink': '#1a1a1a',
        'rule': '#e7e5e4',
        'subtle': '#78716c',
        'accent': '#0c4a6e',
      },
    },
  },
}
