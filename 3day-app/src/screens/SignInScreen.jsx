import { SignIn } from '@clerk/clerk-react'

// The signed-out landing. Renders Clerk's email + password sign-in card,
// centered on the same terracotta background the rest of the app uses so
// the login step reads as part of the app, not a bolted-on redirect.
export default function SignInScreen() {
  return (
    <section className="screen screen-signin">
      <header className="signin-header">
        <span className="signin-eyebrow">3 Day Kitchen <em>&amp;</em> Bath</span>
        <h1 className="signin-title">Consultation <em>Capture</em></h1>
        <p className="signin-sub">Sign in to record and submit consultations.</p>
      </header>
      <div className="signin-card-slot">
        <SignIn
          routing="virtual"
          appearance={{
            elements: {
              // Trim Clerk's outer card chrome so the wrapper CSS controls
              // the visual frame — keeps the sign-in card the same width
              // as the app's other primary surfaces.
              rootBox: { width: '100%' },
              cardBox: { width: '100%', boxShadow: 'none' },
              card: { boxShadow: 'none', border: 'none' },
            },
          }}
        />
      </div>
    </section>
  )
}
