import { useEffect, useRef, useState } from 'react'

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return ''
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/ogg;codecs=opus',
  ]
  for (const c of candidates) {
    if (MediaRecorder.isTypeSupported(c)) return c
  }
  return ''
}

function fmtDuration(sec) {
  const s = Math.floor(sec)
  const m = Math.floor(s / 60)
  const ss = String(s % 60).padStart(2, '0')
  return `${m}:${ss}`
}

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes)) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function Capture({ customer, onBack, onSubmit }) {
  const [mode, setMode] = useState('record')
  const [isRecording, setIsRecording] = useState(false)
  const [audioBlob, setAudioBlob] = useState(null)
  const [transcript, setTranscript] = useState('')
  const [recordError, setRecordError] = useState(null)
  const [elapsed, setElapsed] = useState(0)
  const [uploadedFile, setUploadedFile] = useState(null)

  const recorderRef = useRef(null)
  const chunksRef = useRef([])
  const streamRef = useRef(null)
  const timerRef = useRef(null)
  const startTsRef = useRef(0)
  const fileInputRef = useRef(null)

  useEffect(() => {
    return () => {
      if (recorderRef.current && recorderRef.current.state !== 'inactive') {
        try { recorderRef.current.stop() } catch (_) { /* ignore */ }
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop())
      }
      if (timerRef.current) clearInterval(timerRef.current)
    }
  }, [])

  async function startRecording() {
    setRecordError(null)
    setAudioBlob(null)
    setElapsed(0)

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setRecordError(
        "This browser doesn't support recording. Try the Paste tab.",
      )
      return
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      const mimeType = pickMimeType()
      const recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream)
      recorderRef.current = recorder
      chunksRef.current = []
      recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data)
      }
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: recorder.mimeType || 'audio/webm',
        })
        setAudioBlob(blob)
        if (streamRef.current) {
          streamRef.current.getTracks().forEach((t) => t.stop())
          streamRef.current = null
        }
        if (timerRef.current) {
          clearInterval(timerRef.current)
          timerRef.current = null
        }
      }
      recorder.start(250)
      setIsRecording(true)
      startTsRef.current = Date.now()
      timerRef.current = setInterval(() => {
        setElapsed((Date.now() - startTsRef.current) / 1000)
      }, 200)
    } catch (err) {
      const name = (err && err.name) || ''
      let msg = 'Microphone access was denied or unavailable.'
      if (name === 'NotAllowedError') {
        msg = 'Microphone access was denied. You can use the Paste tab instead.'
      } else if (name === 'NotFoundError') {
        msg = 'No microphone found on this device.'
      } else if (name === 'NotSupportedError') {
        msg = "This browser doesn't support recording. Try Paste."
      } else if (
        typeof location !== 'undefined' &&
        location.protocol === 'http:' &&
        location.hostname !== 'localhost' &&
        location.hostname !== '127.0.0.1'
      ) {
        msg =
          'Phones require HTTPS for the microphone. On your phone, use the Paste tab — or run the dev server over HTTPS.'
      }
      setRecordError(msg)
      setIsRecording(false)
    }
  }

  function stopRecording() {
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop()
    }
    setIsRecording(false)
  }

  function discardRecording() {
    setAudioBlob(null)
    setElapsed(0)
  }

  function openFilePicker() {
    if (fileInputRef.current) fileInputRef.current.click()
  }

  function handleFileSelected(event) {
    const file = event.target.files && event.target.files[0]
    // Clear the input so picking the same file twice still fires onChange.
    event.target.value = ''
    if (!file) return
    setUploadedFile(file)
  }

  function clearUpload() {
    setUploadedFile(null)
  }

  function submit() {
    if (mode === 'record' && audioBlob) {
      onSubmit({ audioBlob })
    } else if (mode === 'paste' && transcript.trim()) {
      onSubmit({ transcript: transcript.trim() })
    } else if (mode === 'upload' && uploadedFile) {
      onSubmit({ audioBlob: uploadedFile, source: '3day-app-upload' })
    }
  }

  const canSubmit =
    (mode === 'record' && !!audioBlob && !isRecording) ||
    (mode === 'paste' && transcript.trim().length > 0) ||
    (mode === 'upload' && !!uploadedFile)

  const customerName =
    (customer && customer.display_name) ||
    (customer && `${customer.first_name || ''} ${customer.last_name || ''}`.trim()) ||
    'this customer'

  return (
    <section className="screen screen-capture">
      <header className="topbar">
        <button className="topbar-back" onClick={onBack} aria-label="Back">
          ←
        </button>
        <div className="topbar-text">
          <span className="topbar-eyebrow">Consultation for</span>
          <h2 className="topbar-title">{customerName}</h2>
        </div>
      </header>

      <div className="mode-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={mode === 'record'}
          className={`mode-tab ${mode === 'record' ? 'is-active' : ''}`}
          onClick={() => setMode('record')}
        >
          Record
        </button>
        <button
          role="tab"
          aria-selected={mode === 'upload'}
          className={`mode-tab ${mode === 'upload' ? 'is-active' : ''}`}
          onClick={() => setMode('upload')}
        >
          Upload
        </button>
        <button
          role="tab"
          aria-selected={mode === 'paste'}
          className={`mode-tab ${mode === 'paste' ? 'is-active' : ''}`}
          onClick={() => setMode('paste')}
        >
          Paste
        </button>
      </div>

      <div className="capture-body">
        {mode === 'record' && (
          <div className="record-zone">
            {!audioBlob && !isRecording && (
              <>
                <p className="record-hint">
                  Tap to start. Hold the phone near the conversation — Deepgram
                  handles real, two-speaker dialogue.
                </p>
                <button
                  className="record-btn"
                  onClick={startRecording}
                  aria-label="Start recording"
                >
                  <span className="record-btn-dot" />
                </button>
                <p className="record-cta">Tap to record</p>
              </>
            )}
            {isRecording && (
              <>
                <p className="record-hint">Listening…</p>
                <button
                  className="record-btn is-recording"
                  onClick={stopRecording}
                  aria-label="Stop recording"
                >
                  <span className="record-btn-square" />
                </button>
                <p className="record-timer" aria-live="polite">
                  {fmtDuration(elapsed)}
                </p>
                <p className="record-cta">Tap to stop</p>
              </>
            )}
            {audioBlob && !isRecording && (
              <div className="record-done">
                <div className="record-done-badge" aria-hidden>
                  <svg viewBox="0 0 24 24" width="22" height="22">
                    <path
                      d="M5 12.5l4.5 4.5L19 7"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <p className="record-done-title">Recording ready</p>
                <p className="record-done-meta">
                  {fmtDuration(elapsed)} · {fmtBytes(audioBlob.size)}
                </p>
                <button className="btn btn-tertiary" onClick={discardRecording}>
                  Re-record
                </button>
              </div>
            )}
            {recordError && <div className="record-error">{recordError}</div>}
          </div>
        )}

        {mode === 'upload' && (
          <div className="upload-zone">
            <input
              ref={fileInputRef}
              type="file"
              accept="audio/*,.m4a,.mp3,.wav,.aac,.ogg,.webm"
              className="upload-input"
              onChange={handleFileSelected}
            />
            {!uploadedFile && (
              <>
                <p className="record-hint">
                  Already have a recording from another app? Tap below and pick
                  the file from your phone.
                </p>
                <button
                  type="button"
                  className="upload-card"
                  onClick={openFilePicker}
                >
                  <span className="upload-icon" aria-hidden>
                    <svg
                      viewBox="0 0 24 24"
                      width="22"
                      height="22"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                      <polyline points="17 8 12 3 7 8" />
                      <line x1="12" y1="3" x2="12" y2="15" />
                    </svg>
                  </span>
                  <p className="upload-title">Choose an audio file</p>
                  <p className="upload-sub">Tap to pick from your phone or files app</p>
                  <p className="upload-formats">m4a · mp3 · wav · aac</p>
                </button>
              </>
            )}
            {uploadedFile && (
              <div className="record-done">
                <div className="record-done-badge" aria-hidden>
                  <svg viewBox="0 0 24 24" width="22" height="22">
                    <path
                      d="M5 12.5l4.5 4.5L19 7"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </div>
                <p className="record-done-title">{uploadedFile.name}</p>
                <p className="record-done-meta">{fmtBytes(uploadedFile.size)}</p>
                <button className="btn btn-tertiary" onClick={clearUpload}>
                  Choose a different file
                </button>
              </div>
            )}
          </div>
        )}

        {mode === 'paste' && (
          <div className="paste-zone">
            <label htmlFor="transcript" className="paste-label">
              Paste or type the consultation transcript
            </label>
            <textarea
              id="transcript"
              className="paste-textarea"
              placeholder="REP: ...&#10;HOMEOWNER: ..."
              value={transcript}
              onChange={(e) => setTranscript(e.target.value)}
              rows={10}
            />
            <p className="paste-hint">
              {transcript.trim().length.toLocaleString()} characters
            </p>
          </div>
        )}
      </div>

      <div className="footer-actions footer-actions-stick">
        <button
          className="btn btn-primary btn-lg"
          disabled={!canSubmit}
          onClick={submit}
        >
          Submit Consultation
        </button>
      </div>
    </section>
  )
}
