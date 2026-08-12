import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const apiPort = process.env.FINDASH_DASHBOARD_API_PORT || '8765'

export default defineConfig({
  plugins: [react()],
  server: {
    strictPort: true,
    proxy: {
      '/api': `http://127.0.0.1:${apiPort}`,
    },
  },
})
