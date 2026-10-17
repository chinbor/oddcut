import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base './' so the built page loads from file:// inside Electron
export default defineConfig({ plugins: [react()], base: './', build: { outDir: 'dist', emptyOutDir: true } })
