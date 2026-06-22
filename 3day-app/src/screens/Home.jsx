export default function Home({ onStart }) {
  return (
    <section className="screen screen-home">
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
        <p className="footer-hint">
          For 3 Day Kitchen <em>&amp;</em> Bath sales reps
        </p>
      </div>
    </section>
  )
}
