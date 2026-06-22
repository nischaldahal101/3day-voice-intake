export default function Processing({ customer }) {
  const name = (customer && customer.display_name) || 'this customer'
  return (
    <section className="screen screen-processing">
      <div className="processing-card">
        <div className="processing-spinner" aria-hidden>
          <span />
          <span />
          <span />
        </div>
        <h2 className="processing-title">
          Working on <em>it…</em>
        </h2>
        <p className="processing-sub">
          Transcribing, pulling structured data, and writing the
          consultation for <strong>{name}</strong> into FieldPulse.
        </p>
        <p className="processing-meta">This typically takes 15&ndash;20 seconds.</p>
      </div>
    </section>
  )
}
