import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  cacheDir: process.env.VITE_CACHE_DIR ?? '.vite-cache-local',
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
