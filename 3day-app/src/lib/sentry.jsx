// Sentry for the React app: errors + tracing. Everything Sentry-specific lives
// here; main.jsx only calls initSentry() and uses AppErrorBoundary.
//
//  - Off (a silent no-op) unless VITE_SENTRY_DSN is set at build time.
//  - No Session Replay, no user info, cookies, headers, bodies or query strings.
//    (@sentry/react v11 replaced `sendDefaultPii` with `dataCollection`, whose
//    defaults collect all of those, so it is set explicitly below.)
//  - Trace headers (sentry-trace, baggage) go ONLY to our API (API_BASE), never
//    to R2 presigned URLs or Clerk. The orchestrator's CORS allows them.
//  - What may be sent is decided in ./sentryPrivacy.js (F-31..F-37): every
//    event/transaction is rebuilt from an allowlist of value rules (no free
//    text), and the transport is the final boundary: only event/transaction/
//    session/client_report items leave, with a rebuilt trace header.
//  - traceLifecycle 'static' (F-33): v11's default 'stream' sends spans as
//    separate items that skip beforeSendTransaction. Static keeps every span
//    inside a transaction event that goes through scrubEvent.
//  - Never call Sentry.setUser(): session envelopes (release health) carry the
//    user as `did` and do not pass through beforeSend.
import * as Sentry from '@sentry/react'
import { API_BASE } from './api.js'
import { safeEnvelope, scrubBreadcrumb, scrubEvent } from './sentryPrivacy.js'

const DSN = import.meta.env.VITE_SENTRY_DSN
/* global __SENTRY_RELEASE__ */
const RELEASE = typeof __SENTRY_RELEASE__ === 'string' && __SENTRY_RELEASE__ ? __SENTRY_RELEASE__ : undefined
const ENVIRONMENT = import.meta.env.VITE_SENTRY_ENVIRONMENT || import.meta.env.MODE
// Our own values for the privacy rules: trace header keys, session attrs (F-40)
// and the event/envelope `sdk` (F-41) come from here, never from a payload.
const OWN = {
  public_key: publicKey(DSN),
  environment: ENVIRONMENT,
  release: RELEASE,
  sdkVersion: Sentry.SDK_VERSION,
}

export function initSentry() {
  if (!DSN) return false
  Sentry.init({
    dsn: DSN,
    environment: ENVIRONMENT,
    release: RELEASE,
    sendDefaultPii: false, // v10 name; v11 ignores it, dataCollection rules
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: false,
      httpBodies: [],
      urlQueryParams: false,
      genAI: { inputs: false, outputs: false },
      queues: false,
      databaseQueryData: false,
      stackFrameVariables: false,
    },
    // No console breadcrumbs (their arguments hold whatever was logged). The
    // scrubber drops them too, in case a future SDK renames the integration.
    integrations: (defaults) => [
      ...defaults.filter((i) => i.name !== 'Console'),
      Sentry.browserTracingIntegration(),
    ],
    tracesSampleRate: 1.0,
    traceLifecycle: 'static',
    tracePropagationTargets: [apiTarget(API_BASE)],
    beforeSend: (event) => safely((e) => scrubEvent(e, OWN), event),
    beforeSendTransaction: (event) => safely((e) => scrubEvent(e, OWN), event),
    beforeBreadcrumb: (crumb) => safely(scrubBreadcrumb, crumb),
    transport: (options) => safeTransport(options, OWN),
  })
  return true
}

// Fallback for a render crash anywhere in the tree: the existing ErrorScreen.
// Works (as a plain error boundary) even when Sentry is off.
export function AppErrorBoundary({ children, fallback }) {
  return <Sentry.ErrorBoundary fallback={fallback}>{children}</Sentry.ErrorBoundary>
}

// --- internals ---------------------------------------------------------------

// Matches API_BASE itself and anything below it, not a longer host/port
// (http://localhost:5100 must not match http://localhost:51000).
function apiTarget(base) {
  const escaped = base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`^${escaped}(?:[/?#]|$)`)
}

// The SDK's fetch transport behind safeEnvelope: anything that did not pass
// beforeSend/beforeSendTransaction (streamed spans, logs, replays, ...) and any
// unsafe trace-header key is dropped here, whatever future SDK code produces.
function safeTransport(options, own) {
  const inner = Sentry.makeFetchTransport(options)
  return {
    send(envelope) {
      let clean = null
      try {
        clean = safeEnvelope(envelope, own)
      } catch (_) {
        clean = null
      }
      return clean ? inner.send(clean) : Promise.resolve({})
    },
    flush: (timeout) => inner.flush(timeout),
  }
}

function publicKey(dsn) {
  if (!dsn) return undefined
  try {
    return new URL(dsn).username || undefined
  } catch (_) {
    return undefined
  }
}

function safely(fn, value) {
  try {
    return fn(value)
  } catch (_) {
    return null // drop rather than send something unscrubbed
  }
}
