import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: { '/api': { target: 'http://127.0.0.1:8002' } },
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      input: { index: 'index.html', authCallback: 'auth-callback.html' },
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
          icons: ['lucide-react'],
        },
      },
    },
  },
});
