import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const target = process.env.CABIN_BACKEND ?? 'http://127.0.0.1:8800'

export default defineConfig({
  plugins: [react()],
  server: { port: 5174, proxy: { '/api': target, '/evidence': target, '/ws': { target: target.replace('http', 'ws'), ws: true } } },
})
