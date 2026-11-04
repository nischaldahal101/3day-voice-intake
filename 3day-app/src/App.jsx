import { useCallback, useEffect, useRef, useState } from 'react'
import {
  baseContentType,
  fetchJobStatus,
  filenameForBlob,
  putToR2,
  requestUploadUrl,
  submitConsultationJob,
} from './lib/api.js'
import { clearActiveJob, getActiveJob, setActiveJob } from './lib/activeJob.js'
import Home from './screens/Home.jsx'
import CustomerPicker from './screens/CustomerPicker.jsx'
import Capture from './screens/Capture.jsx'
import Processing from './screens/Processing.jsx'
import Confirmation from './screens/Confirmation.jsx'
import ErrorScreen from './screens/ErrorScreen.jsx'
import RecentJobs from './screens/RecentJobs.jsx'

// Take a done /job-status response and shape it for the existing Confirmation
// component (which was written against the old sync response). `elapsed_seconds`
// is synthesized from the row's created_at → updated_at.
function shapeJobResult(data) {
  const result = data.result || {}
  let elapsed = null
  try {
    const c = new Date(data.created_at).getTime()
    const u = new Date(data.updated_at).getTime()
    if (Number.isFinite(c) && Number.isFinite(u)) elapsed = (u - c) / 1000
  } catch (_) { /* leave null */ }
  return {
    extraction: result.extraction,
    fieldpulse: result.fieldpulse,           // old mock-writer jobs only
    fieldpulse_sync: result.fieldpulse_sync, // new jobs: real FieldPulse sync summary
    input: result.input,
    transcription_seconds: result.transcription_seconds,
    transcript: result.transcript,
    elapsed_seconds: elapsed,
  }
}

export default function App() {
  const [screen, setScreen] = useState('home')
  const [customer, setCustomer] = useState(null)
  const [phase, setPhase] = useState(null)      // 'uploading' | 'submitting' | 'queued' | 'processing'
  const [uploadPct, setUploadPct] = useState(0) // 0..1 for the R2 PUT
  const [activeJobId, setActiveJobId] = useState(null)
  const [jobResult, setJobResult] = useState(null)
  const [errorState, setErrorState] = useState(null)
  const [lastPayload, setLastPayload] = useState(null)

  const pollTimerRef = useRef(null)

  // ---- polling helpers -------------------------------------------------

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  const pollOnce = useCallback(async (jobId) => {
    try {
      const data = await fetchJobStatus(jobId)
      if (data.status === 'done') {
        stopPolling()
        clearActiveJob()
        setJobResult(shapeJobResult(data))
        setPhase('done')
        setScreen('confirmation')
        return
      }
      if (data.status === 'failed') {
        stopPolling()
        clearActiveJob()
        setErrorState({
          title: 'The consultation failed',
          message: data.error || 'The pipeline reported an error.',
        })
        setScreen('error')
        return
      }
      setPhase(data.status) // queued | processing
      pollTimerRef.current = setTimeout(() => pollOnce(jobId), 3000)
    } catch (_) {
      // transient — keep polling
      pollTimerRef.current = setTimeout(() => pollOnce(jobId), 3000)
    }
  }, [stopPolling])

  const startPolling = useCallback((jobId) => {
    stopPolling()
    // First poll almost immediately, then every 3s.
    pollTimerRef.current = setTimeout(() => pollOnce(jobId), 500)
  }, [pollOnce, stopPolling])

  // On mount, resume any in-flight job stored in localStorage.
  useEffect(() => {
    const active = getActiveJob()
    if (active && active.job_id) {
      if (active.customer) setCustomer(active.customer)
      setActiveJobId(active.job_id)
      setPhase('queued')
      setScreen('processing')
      startPolling(active.job_id)
    }
    return stopPolling
  }, [startPolling, stopPolling])

  // ---- reset / navigation ---------------------------------------------

  const reset = useCallback(() => {
    stopPolling()
    clearActiveJob()
    setCustomer(null)
    setJobResult(null)
    setErrorState(null)
    setLastPayload(null)
    setActiveJobId(null)
    setPhase(null)
    setUploadPct(0)
    setScreen('home')
  }, [stopPolling])

  // ---- submit: one async path for Record, Upload and Paste ------------
  // Audio is PUT straight to R2 first; a pasted transcript is sent with the
  // submit itself. Either way the backend queues a job and we poll it.

  const handleSubmitAsync = useCallback(async (payload) => {
    const { audioBlob, transcript } = payload
    setLastPayload(payload)
    setUploadPct(0)
    setJobResult(null)
    setErrorState(null)
    setPhase(audioBlob ? 'uploading' : 'submitting')
    setScreen('processing')

    try {
      let input
      if (audioBlob) {
        const contentType = baseContentType(audioBlob.type)
        const filename = filenameForBlob(audioBlob)
        const { upload_url, audio_key } = await requestUploadUrl({
          filename,
          contentType,
          customerId: customer && customer.id,
        })
        await putToR2(upload_url, audioBlob, contentType, (pct) => setUploadPct(pct))
        setPhase('submitting')
        input = { audioKey: audio_key }
      } else {
        input = { transcript }
      }
      const submitted = await submitConsultationJob({
        ...input,
        customer,
        source: audioBlob ? '3day-app' : '3day-app-paste',
      })
      const active = {
        job_id: submitted.job_id,
        customer,
        submitted_at: new Date().toISOString(),
      }
      setActiveJob(active) // localStorage — survives reload
      setActiveJobId(submitted.job_id)
      setPhase(submitted.status || 'queued')
      startPolling(submitted.job_id)
    } catch (e) {
      setPhase(null)
      setErrorState({
        title: "Couldn't submit the consultation",
        message: (e && e.message) || String(e),
      })
      setScreen('error')
    }
  }, [customer, startPolling])

  // ---- unified submit dispatcher --------------------------------------

  const handleSubmit = useCallback((payload) => {
    if (payload && (payload.audioBlob || payload.transcript)) return handleSubmitAsync(payload)
  }, [handleSubmitAsync])

  const retry = useCallback(() => {
    if (lastPayload) return handleSubmit(lastPayload)
    setScreen('capture')
  }, [lastPayload, handleSubmit])

  // ---- opening a job from the Recent list -----------------------------

  const openRecentJob = useCallback((job) => {
    stopPolling()
    // The picker snapshot stored on the job, for the name on Confirmation /
    // Processing (display only; null for old jobs). Failed jobs keep the old
    // behaviour (no customer), so Retry from there still starts clean.
    const c = job && job.customer
    const snapshot = c && typeof c === 'object' && !Array.isArray(c) ? c : null
    setCustomer(job.status === 'failed' ? null : snapshot)
    setActiveJobId(job.id)
    if (job.status === 'done') {
      // Fetch full result for the confirmation screen.
      fetchJobStatus(job.id)
        .then((data) => {
          setJobResult(shapeJobResult(data))
          setPhase('done')
          setScreen('confirmation')
        })
        .catch((e) => {
          setErrorState({ title: "Couldn't load that consultation", message: e.message })
          setScreen('error')
        })
      return
    }
    if (job.status === 'failed') {
      setErrorState({
        title: 'That consultation failed',
        message: job.error || 'The pipeline reported an error.',
      })
      setScreen('error')
      return
    }
    // queued or processing — resume polling
    setPhase(job.status)
    setScreen('processing')
    startPolling(job.id)
  }, [startPolling, stopPolling])

  return (
    <div className="app">
      {screen === 'home' && (
        <Home
          onStart={() => setScreen('pick')}
          onRecent={() => setScreen('recent-jobs')}
        />
      )}
      {screen === 'pick' && (
        <CustomerPicker
          onBack={() => setScreen('home')}
          onSelect={(c) => {
            setCustomer(c)
            setScreen('capture')
          }}
        />
      )}
      {screen === 'capture' && (
        <Capture
          customer={customer}
          onBack={() => setScreen('pick')}
          onSubmit={handleSubmit}
        />
      )}
      {screen === 'processing' && (
        <Processing
          customer={customer}
          phase={phase}
          uploadPct={uploadPct}
          jobId={activeJobId}
          onLeave={() => setScreen('home')}
        />
      )}
      {screen === 'confirmation' && (
        <Confirmation customer={customer} result={jobResult} onDone={reset} />
      )}
      {screen === 'error' && (
        <ErrorScreen error={errorState} onRetry={retry} onHome={reset} />
      )}
      {screen === 'recent-jobs' && (
        <RecentJobs
          onBack={() => setScreen('home')}
          onOpen={openRecentJob}
        />
      )}
    </div>
  )
}
