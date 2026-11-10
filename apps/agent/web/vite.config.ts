import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const target = process.env.CABIN_BACKEND ?? 'http://127.0.0.1:8810'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': target,
      '/auth': target,
      '/local': target,
      '/evidence': target,
      '/simcar': target,
      '/ws': { target: target.replace('http', 'ws'), ws: true },
    },
  },
})
