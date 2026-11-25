/** Tailwind (standalone CLI v3) - build: tools/tailwindcss -i static/css/input.css -o static/css/app.css --minify */
module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html', './static/js/**/*.js'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', '"SF Pro Display"', '"Segoe UI Variable Text"',
          '"Segoe UI"', 'Inter', 'Roboto', 'system-ui', 'sans-serif'],
        mono: ['"SF Mono"', '"JetBrains Mono"', '"Cascadia Code"', 'Menlo', 'Consolas', 'monospace'],
      },
      colors: {
        accent: { DEFAULT: '#0A84FF', 50: '#eef6ff', 100: '#d9ebff', 200: '#bcdcff', 300: '#8ec6ff', 400: '#59a6ff',
          500: '#0A84FF', 600: '#0070e0', 700: '#005bb8', 800: '#064d94', 900: '#0b417a' },
      },
      boxShadow: {
        glass: '0 1px 0 0 rgba(255,255,255,.55) inset, 0 8px 32px -8px rgba(15,23,42,.18), 0 2px 6px -2px rgba(15,23,42,.08)',
        'glass-dark': '0 1px 0 0 rgba(255,255,255,.08) inset, 0 12px 40px -10px rgba(0,0,0,.6), 0 2px 8px -2px rgba(0,0,0,.4)',
        float: '0 24px 60px -18px rgba(15,23,42,.35)',
      },
      borderRadius: { '4xl': '2rem' },
      keyframes: {
        'fade-up': { '0%': { opacity: 0, transform: 'translateY(6px)' }, '100%': { opacity: 1, transform: 'none' } },
        pulseDot: { '0%,100%': { opacity: .25 }, '50%': { opacity: 1 } },
      },
      animation: { 'fade-up': 'fade-up .28s ease-out both', 'pulse-dot': 'pulseDot 1.2s ease-in-out infinite' },
    },
  },
  plugins: [],
};
