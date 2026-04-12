/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: ['./app/static/**/*.html'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular'],
      },
      colors: {
        violet: { 500: '#8b5cf6', 600: '#7c3aed' },
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(139,92,246,.25), 0 8px 30px -8px rgba(139,92,246,.35)',
      },
      keyframes: {
        slideIn: {
          from: { opacity: '0', transform: 'translateX(12px)' },
          to:   { opacity: '1', transform: 'translateX(0)' },
        },
      },
      animation: {
        slideIn: 'slideIn .25s ease',
      },
    },
  },
  plugins: [],
}
