import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { sentryVitePlugin } from '@sentry/vite-plugin'

// Sentry source maps, only when SENTRY_AUTH_TOKEN is set (the Render build):
// the build emits *hidden* maps (no sourceMappingURL comment), the plugin
// uploads them to nd-tv/voice-intake-frontend and then deletes every .map
// from dist (in a finally, so also when the upload fails).
// Without the token no maps are generated at all and nothing is uploaded.
const SENTRY_AUTH_TOKEN = process.env.SENTRY_AUTH_TOKEN
const SENTRY_RELEASE = process.env.SENTRY_RELEASE || process.env.RENDER_GIT_COMMIT || ''

export default defineConfig({
  plugins: [
    react(),
    SENTRY_AUTH_TOKEN &&
      sentryVitePlugin({
        org: 'nd-tv',
        project: 'voice-intake-frontend',
        authToken: SENTRY_AUTH_TOKEN,
        // The app sets the release itself (__SENTRY_RELEASE__ below).
        release: { name: SENTRY_RELEASE || undefined, inject: false },
        sourcemaps: { filesToDeleteAfterUpload: ['./dist/**/*.map'] },
        telemetry: false,
      }),
  ].filter(Boolean),
  define: {
    __SENTRY_RELEASE__: JSON.stringify(SENTRY_RELEASE),
  },
  build: {
    sourcemap: SENTRY_AUTH_TOKEN ? 'hidden' : false,
  },
  server: {
    host: true, // bind to 0.0.0.0 so phones on the LAN can reach the dev server
    port: 5173,
  },
})
