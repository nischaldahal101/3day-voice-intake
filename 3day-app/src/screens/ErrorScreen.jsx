export default function ErrorScreen({ error, onRetry, onHome }) {
  return (
    <section className="screen screen-error">
      <div className="error-card">
        <div className="error-badge" aria-hidden>!</div>
        <h2 className="error-title">
          {(error && error.title) || 'Something went wrong'}
        </h2>
        <p className="error-message">
          {(error && error.message) || 'Try again in a moment.'}
        </p>
        {error && error.detail && (
          <details className="error-details">
            <summary>Technical detail</summary>
            <pre>{error.detail}</pre>
          </details>
        )}
      </div>
      <div className="footer-actions">
        <button className="btn btn-secondary" onClick={onHome}>
          Home
        </button>
        <button className="btn btn-primary btn-lg" onClick={onRetry}>
          Try again
        </button>
      </div>
    </section>
  )
}
