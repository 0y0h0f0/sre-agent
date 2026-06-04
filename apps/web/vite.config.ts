import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  cacheDir: '.vite-cache',
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://api:8000',
        changeOrigin: true,
      },
    },
  },
});
