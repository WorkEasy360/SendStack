import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Proxied to **nginx on :80**, not to a `runserver` on :8000.
    //
    // Both answer /api identically, which makes this look like a free choice
    // and it is not. docker-compose deliberately never publishes Redis (see
    // the redis service), so a host-side runserver reaches Postgres on :5432
    // but has no broker at all. Campaign launch writes its rows inside a
    // transaction and publishes dispatch_campaign *outside* it, so against
    // :8000 the campaign and its messages commit, the dispatch is never
    // queued, and every recipient sits at QUEUED for ever -- no error in the
    // UI, nothing in the worker log, and an idle worker looks identical to a
    // working one. The containerised backend behind nginx shares a network
    // with Redis and the Celery worker, which is what makes a send happen.
    //
    // nginx proxies /api, /media, /admin and /health to backend:8000 itself
    // (nginx/locations.conf), so every path below is served exactly as it is
    // in production.
    proxy: {
      '/api': 'http://127.0.0.1:80',
      // Uploaded attachments: nginx serves these from the media volume, and
      // the picker previews them straight from this origin.
      '/media': 'http://127.0.0.1:80',
      '/admin': 'http://127.0.0.1:80',
      '/health': 'http://127.0.0.1:80',
    },
  },
})
