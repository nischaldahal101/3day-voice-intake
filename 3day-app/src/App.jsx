import { useCallback, useEffect, useRef, useState } from 'react'
import {
  baseContentType,
  fetchJobStatus,
  filenameForBlob,
  putToR2,
  requestUploadUrl,
  submitConsultationJob,
  submitConsultationSync,
} from './lib/api.js'
import { clearActiveJob, getActiveJob, setActiveJob } from './lib/activeJob.js'
import Home from './screens/Home.jsx'
import CustomerPicker from './screens/CustomerPicker.jsx'
import Capture from './screens/Capture.jsx'
import Processing from './screens/Processing.jsx'
import Confirmation from './screens/Confirmation.jsx'
import ErrorScreen from './screens/ErrorScreen.jsx'
import RecentJobs from './screens/RecentJobs.jsx'

const FRIENDLY = {
  transcription_failed: {
    title: "Couldn't transcribe that recording",
    message:
      "The audio didn't come through clearly enough to transcribe. Try recording somewhere quieter, or paste the transcript instead.",
  },
  extraction_failed: {
    title: 'Analysis failed',
    message:
      "We got the transcript but couldn't pull structured data out of it. Tap retry — if it keeps failing, save the transcript and try later.",
  },
  writer_failed: {
    title: 'Saved analysis, but FieldPulse update failed',
    message:
      "We got the structured data but couldn't update FieldPulse. The analysis is safe — retry to push it again.",
  },
  fieldpulse_unreachable: {
    title: "Can't reach FieldPulse",
    message:
      'The FieldPulse server appears to be offline. Check the connection and try again.',
  },
}

function explainSyncError(data, httpStatus) {
  const s = data && data.status
  if (FRIENDLY[s]) return { ...FRIENDLY[s], detail: data && data.error }
  if (httpStatus >= 500) {
    return {
      title: 'Something went wrong on our side',
      message: (data && data.error) || 'The server returned an unexpected error.',
    }
  }
  return {
    title: "Couldn't complete the consultation",
    message: (data && data.error) || 'Unknown error.',
  }
}

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
    fieldpulse: result.fieldpulse,
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

  // ---- submit: async path (Record + Upload modes) ---------------------

  const handleSubmitAsync = useCallback(async ({ audioBlob }) => {
    setLastPayload({ audioBlob })
    setUploadPct(0)
    setJobResult(null)
    setErrorState(null)
    setPhase('uploading')
    setScreen('processing')

    const contentType = baseContentType(audioBlob.type)
    const filename = filenameForBlob(audioBlob)
    try {
      const { upload_url, audio_key } = await requestUploadUrl({
        filename,
        contentType,
        customerId: customer && customer.id,
      })
      await putToR2(upload_url, audioBlob, contentType, (pct) => setUploadPct(pct))
      setPhase('submitting')
      const submitted = await submitConsultationJob({
        audioKey: audio_key,
        customer,
        source: '3day-app',
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

  // ---- submit: sync path (Paste mode / transcript) --------------------

  const handleSubmitSync = useCallback(async ({ transcript }) => {
    setLastPayload({ transcript })
    setJobResult(null)
    setErrorState(null)
    setPhase(null)
    setScreen('processing')
    try {
      const { ok, status, data } = await submitConsultationSync({
        transcript,
        customerId: customer && customer.id,
      })
      if (ok && data && data.status === 'success') {
        setJobResult(data)
        setScreen('confirmation')
      } else {
        setErrorState({ ...explainSyncError(data, status), data })
        setScreen('error')
      }
    } catch (e) {
      setErrorState({
        title: "Couldn't reach the server",
        message: (e && e.message) || String(e),
      })
      setScreen('error')
    }
  }, [customer])

  // ---- unified submit dispatcher --------------------------------------

  const handleSubmit = useCallback((payload) => {
    if (payload && payload.audioBlob) return handleSubmitAsync(payload)
    if (payload && payload.transcript) return handleSubmitSync(payload)
  }, [handleSubmitAsync, handleSubmitSync])

  const retry = useCallback(() => {
    if (lastPayload) return handleSubmit(lastPayload)
    setScreen('capture')
  }, [lastPayload, handleSubmit])

  // ---- opening a job from the Recent list -----------------------------

  const openRecentJob = useCallback((job) => {
    stopPolling()
    setCustomer(null)
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
