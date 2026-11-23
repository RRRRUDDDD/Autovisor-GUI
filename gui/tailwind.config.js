/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '"HarmonyOS Sans SC"',
          '"MiSans"',
          '"PingFang SC"',
          '"Microsoft YaHei UI"',
          '"Segoe UI"',
          'system-ui',
          '-apple-system',
          'sans-serif',
        ],
        mono: [
          '"JetBrains Mono"',
          '"Cascadia Code"',
          '"SFMono-Regular"',
          'Consolas',
          'monospace',
        ],
      },
      colors: {
        /* Material Design 3 (Material You) Tonal Role System */
        primary: {
          DEFAULT: 'rgb(var(--md-primary) / <alpha-value>)',
          foreground: 'rgb(var(--md-on-primary) / <alpha-value>)',
          container: 'rgb(var(--md-primary-container) / <alpha-value>)',
        },
        'on-primary': 'rgb(var(--md-on-primary) / <alpha-value>)',
        'on-primary-container': 'rgb(var(--md-on-primary-container) / <alpha-value>)',

        secondary: {
          DEFAULT: 'rgb(var(--md-secondary) / <alpha-value>)',
          container: 'rgb(var(--md-secondary-container) / <alpha-value>)',
        },
        'on-secondary': 'rgb(var(--md-on-secondary) / <alpha-value>)',
        'on-secondary-container': 'rgb(var(--md-on-secondary-container) / <alpha-value>)',

        tertiary: {
          DEFAULT: 'rgb(var(--md-tertiary) / <alpha-value>)',
          container: 'rgb(var(--md-tertiary-container) / <alpha-value>)',
        },
        'on-tertiary': 'rgb(var(--md-on-tertiary) / <alpha-value>)',
        'on-tertiary-container': 'rgb(var(--md-on-tertiary-container) / <alpha-value>)',

        surface: {
          DEFAULT: 'rgb(var(--md-surface) / <alpha-value>)',
          dim: 'rgb(var(--md-surface-dim) / <alpha-value>)',
          'container-lowest': 'rgb(var(--md-surface-container-lowest) / <alpha-value>)',
          'container-low': 'rgb(var(--md-surface-container-low) / <alpha-value>)',
          container: 'rgb(var(--md-surface-container) / <alpha-value>)',
          'container-high': 'rgb(var(--md-surface-container-high) / <alpha-value>)',
          'container-highest': 'rgb(var(--md-surface-container-highest) / <alpha-value>)',
        },
        'on-surface': 'rgb(var(--md-on-surface) / <alpha-value>)',
        'on-surface-variant': 'rgb(var(--md-on-surface-variant) / <alpha-value>)',

        outline: {
          DEFAULT: 'rgb(var(--md-outline) / <alpha-value>)',
          variant: 'rgb(var(--md-outline-variant) / <alpha-value>)',
        },

        error: {
          DEFAULT: 'rgb(var(--md-error) / <alpha-value>)',
          container: 'rgb(var(--md-error-container) / <alpha-value>)',
        },
        'on-error': 'rgb(var(--md-on-error) / <alpha-value>)',
        'on-error-container': 'rgb(var(--md-on-error-container) / <alpha-value>)',

        success: {
          DEFAULT: 'rgb(var(--md-success) / <alpha-value>)',
          container: 'rgb(var(--md-success-container) / <alpha-value>)',
          ink: 'rgb(var(--md-on-success-container) / <alpha-value>)',
        },
        'on-success-container': 'rgb(var(--md-on-success-container) / <alpha-value>)',

        warning: {
          DEFAULT: 'rgb(var(--md-warning) / <alpha-value>)',
          container: 'rgb(var(--md-warning-container) / <alpha-value>)',
          ink: 'rgb(var(--md-on-warning-container) / <alpha-value>)',
        },
        'on-warning-container': 'rgb(var(--md-on-warning-container) / <alpha-value>)',

        /* Backwards-compatible aliases mapped to M3 roles */
        ink: 'rgb(var(--md-on-surface) / <alpha-value>)',
        body: 'rgb(var(--md-on-surface-variant) / <alpha-value>)',
        faint: 'rgb(var(--md-outline) / <alpha-value>)',
        canvas: 'rgb(var(--md-surface) / <alpha-value>)',
        soft: 'rgb(var(--md-surface-container) / <alpha-value>)',
        line: 'rgb(var(--md-outline-variant) / <alpha-value>)',
        brand: {
          DEFAULT: 'rgb(var(--md-primary) / <alpha-value>)',
          dark: 'rgb(var(--md-primary) / <alpha-value>)',
          soft: 'rgb(var(--md-primary-container) / <alpha-value>)',
        },
        danger: 'rgb(var(--md-error) / <alpha-value>)',
      },
      boxShadow: {
        'elevation-1': '0 1px 2px rgb(0 0 0 / 0.08), 0 1px 3px 1px rgb(0 0 0 / 0.05)',
        'elevation-2': '0 1px 2px rgb(0 0 0 / 0.1), 0 2px 6px 2px rgb(0 0 0 / 0.07)',
        'elevation-3': '0 4px 8px 3px rgb(0 0 0 / 0.09), 0 1px 3px rgb(0 0 0 / 0.12)',
        card: '0 1px 2px rgb(0 0 0 / 0.05)',
        lift: '0 4px 12px rgb(0 0 0 / 0.08)',
      },
      spacing: {
        '4.5': '1.125rem',
      },
      borderRadius: {
        'm3-sm': '8px',
        'm3-md': '12px',
        'm3-lg': '16px',
        'm3-xl': '24px',
        'm3-2xl': '28px',
      },
      transitionTimingFunction: {
        m3: 'cubic-bezier(0.2, 0, 0, 1)',
        'm3-emphasized': 'cubic-bezier(0.05, 0.7, 0.1, 1)',
      },
      keyframes: {
        'stagger-up': {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'pop-in': {
          from: { opacity: '0', transform: 'scale(0.97)' },
          to: { opacity: '1', transform: 'scale(1)' },
        },
      },
      animation: {
        'stagger-up': 'stagger-up 0.35s cubic-bezier(0.05, 0.7, 0.1, 1) both',
        'fade-in': 'fade-in 0.18s cubic-bezier(0.2, 0, 0, 1) both',
        'pop-in': 'pop-in 0.28s cubic-bezier(0.05, 0.7, 0.1, 1) both',
      },
    },
  },
  plugins: [],
};
