/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
  "./src/components/**/*.{ts,tsx}",
  "./src/app/**/*.{ts,tsx}",
    "./src/app/**/*.{js,ts,jsx,tsx}",
    "./src/components/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        slate: {
          50: '#F8FAFC',
          100: '#F1F5F9',
          200: '#E2E8F0',
          400: '#94A3B8',
          500: '#64748B',
          700: '#334155',
          800: '#1E293B',
          900: '#0F172A',
        },
        blue: {
          500: '#3B82F6',
          600: '#2563EB',
          700: '#1D4ED8',
        },
        emerald: {
          600: '#059669',
        },
        amber: {
          500: '#F59E0B',
        },
        red: {
          600: '#DC2626',
        }
      },
      fontFamily: {
        sans: ['Inter', 'sans-serif'],
      }
    }
  },
  plugins: []
}
