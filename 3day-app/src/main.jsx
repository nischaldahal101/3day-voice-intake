import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ClerkProvider, SignedIn, SignedOut } from '@clerk/clerk-react'
import App from './App.jsx'
import SignInScreen from './screens/SignInScreen.jsx'
import './index.css'

// Clerk publishable key is baked in at build time via Vite's env system.
// It's public (not a secret), so shipping it in the JS bundle is fine.
const CLERK_PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY

if (!CLERK_PUBLISHABLE_KEY) {
  // Fail loudly at boot instead of rendering a mysteriously blank app —
  // saves an hour of "why doesn't sign-in work" the first time someone
  // forgets to set .env.
  throw new Error(
    'Missing VITE_CLERK_PUBLISHABLE_KEY. Set it in 3day-app/.env ' +
      '(see .env.example) — grab the value from your Clerk dashboard → API Keys.',
  )
}

// Style Clerk's built-in UI to sit inside the terracotta theme. The values
// mirror the CSS custom properties in index.css so the sign-in card, user
// button, and any future Clerk components read as part of the same app.
const CLERK_APPEARANCE = {
  variables: {
    colorPrimary: '#C5734A',        // --terracotta
    colorText: '#1F1A17',           // --text
    colorTextSecondary: '#5B5450',  // --text-secondary
    colorBackground: '#FFFFFF',     // --surface
    colorInputBackground: '#FFFFFF',
    colorInputText: '#1F1A17',
    colorDanger: '#B83A3A',         // --danger
    borderRadius: '12px',           // --radius-md
    fontFamily:
      "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
  },
  elements: {
    // Kill Clerk's default drop shadow — the sign-in wrapper in App.jsx
    // supplies the framing so the card looks native inside the app shell.
    card: {
      boxShadow: '0 12px 32px rgba(31, 26, 23, 0.14)',
      borderRadius: '16px',
    },
    formButtonPrimary: {
      backgroundColor: '#C5734A',
      textTransform: 'none',
      fontWeight: 600,
      letterSpacing: '0',
      '&:hover': { backgroundColor: '#9B5A36' },
      '&:focus': { backgroundColor: '#9B5A36' },
      '&:active': { backgroundColor: '#9B5A36' },
    },
  },
}

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ClerkProvider
      publishableKey={CLERK_PUBLISHABLE_KEY}
      appearance={CLERK_APPEARANCE}
    >
      {/* Gate the whole app: signed-out users see only the sign-in card,
          signed-in users get the existing app tree. App itself only mounts
          (and its localStorage-resume effect only fires) after sign-in. */}
      <SignedOut>
        <SignInScreen />
      </SignedOut>
      <SignedIn>
        <App />
      </SignedIn>
    </ClerkProvider>
  </StrictMode>,
)
