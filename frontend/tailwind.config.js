/** Design tokens: "field operations board". No blue/purple, no shadows, 0-2px radius. */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    colors: {
      transparent: 'transparent',
      current: 'currentColor',
      ink: '#15120E',
      bone: '#ECE6D8',
      paper: '#F6F2E8',
      orange: '#E4570F',
      red: '#C0182B',
      amber: '#E9A91A',
      green: '#3F7A3A',
      rule: '#BDB5A3',
    },
    fontFamily: {
      display: ['"Barlow Condensed"', 'sans-serif'],
      sans: ['"IBM Plex Sans"', 'sans-serif'],
      mono: ['"IBM Plex Mono"', 'monospace'],
    },
    borderRadius: { none: '0', DEFAULT: '0', sm: '1px', md: '2px' },
    boxShadow: { none: 'none' },
    extend: {
      minHeight: { touch: '56px' },
      minWidth: { touch: '56px' },
    },
  },
  corePlugins: { boxShadow: false },
  plugins: [],
}
