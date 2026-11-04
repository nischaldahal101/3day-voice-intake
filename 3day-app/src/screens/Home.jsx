import { UserButton } from '@clerk/clerk-react'

export default function Home({ onStart, onRecent, onAdmin }) {
  return (
    <section className="screen screen-home">
      {/* Clerk's UserButton — shows the signed-in user's avatar and opens a
          menu with "Sign out". Anchored top-right so it's always reachable
          without pushing hero copy around. */}
      <div className="home-account">
        <UserButton
          afterSignOutUrl="/"
          appearance={{
            elements: {
              userButtonAvatarBox: { width: '36px', height: '36px' },
            },
          }}
        />
      </div>
      <header className="hero">
        <span className="hero-eyebrow">3 Day Kitchen <em>&amp;</em> Bath</span>
        <h1 className="hero-title">
          Consultation <em>Capture</em>
        </h1>
        <p className="hero-subtitle">
          Record an in-home consultation. We&rsquo;ll transcribe it,
          summarize the scope, and push the result into FieldPulse so the
          team can pick up right where you left off.
        </p>
      </header>
      <div className="footer-actions">
        <button className="btn btn-primary btn-lg" onClick={onStart}>
          New Consultation
        </button>
        {onRecent && (
          <button className="btn btn-secondary" onClick={onRecent}>
            Recent consultations
          </button>
        )}
        {onAdmin && (
          <button className="btn btn-tertiary" onClick={onAdmin}>
            Admin: all consultations
          </button>
        )}
        <p className="footer-hint">
          For 3 Day Kitchen <em>&amp;</em> Bath sales reps
        </p>
      </div>
    </section>
  )
}
