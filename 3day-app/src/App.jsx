import { useCallback, useEffect, useRef, useState } from 'react'
import {
  baseContentType,
  fetchJobStatus,
  filenameForBlob,
  putToR2,
  requestUploadUrl,
  submitConsultationJob,
} from './lib/api.js'
import { clearActiveJob, forgetJob, getActiveJob, setActiveJob } from './lib/activeJob.js'
import { parseAdminHash } from './lib/admin.js'
import Home from './screens/Home.jsx'
import CustomerPicker from './screens/CustomerPicker.jsx'
import Capture from './screens/Capture.jsx'
import Processing from './screens/Processing.jsx'
import Confirmation from './screens/Confirmation.jsx'
import ErrorScreen from './screens/ErrorScreen.jsx'
import AdminList from './screens/AdminList.jsx'
import AdminDetail from './screens/AdminDetail.jsx'

const isAdminScreen = (s) => s === 'admin' || s === 'admin-detail'

// The Consultations screens (internally 'admin' / 'admin-detail') have their
// own URL (#admin, #admin/<job_id>; #consultations[/<id>] also accepted) so Mike can
// bookmark them and the FieldPulse comment can link straight to one. A hash
// needs no server rewrite rules and survives the Clerk sign-in screen.
function setAdminHash(hash) {
  if (window.location.hash !== hash) window.location.hash = hash // fires hashchange
}
// Replace (not push) the hash: used after a delete, so browser Back doesn't
// land on the deleted consultation's URL. replaceState fires no hashchange.
function replaceAdminHash(hash) {
  window.history.replaceState(null, '', window.location.pathname + window.location.search + hash)
}
function clearAdminHash() {
  if (parseAdminHash(window.location.hash)) {
    window.history.pushState(null, '', window.location.pathname + window.location.search)
  }
}

const GONE = {
  title: 'That consultation no longer exists',
  message: 'It was deleted from Consultations. Anything already sent to FieldPulse is still there.',
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
  const [adminJobId, setAdminJobId] = useState(null)
  const [adminListState, setAdminListState] = useState(null) // search/filter/page to restore

  const pollTimerRef = useRef(null)
  const screenRef = useRef(screen)
  screenRef.current = screen

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
        // A background job finishing never pulls the user out of the
        // Consultations screens; it shows up in that list.
        if (!isAdminScreen(screenRef.current)) setScreen('confirmation')
        return
      }
      if (data.status === 'failed') {
        stopPolling()
        clearActiveJob()
        if (isAdminScreen(screenRef.current)) return
        setErrorState({
          title: 'The consultation failed',
          message: data.error || 'The pipeline reported an error.',
        })
        setScreen('error')
        return
      }
      setPhase(data.status) // queued | processing
      pollTimerRef.current = setTimeout(() => pollOnce(jobId), 3000)
    } catch (e) {
      if (e && e.status === 404) {
        // Deleted (admin panel, maybe on another device): stop, don't resume it again.
        stopPolling()
        forgetJob(jobId)
        setActiveJobId((cur) => (cur === jobId ? null : cur))
        setPhase(null)
        if (isAdminScreen(screenRef.current)) return
        setErrorState(GONE)
        setScreen('error')
        return
      }
      // transient — keep polling
      pollTimerRef.current = setTimeout(() => pollOnce(jobId), 3000)
    }
  }, [stopPolling])

  const startPolling = useCallback((jobId) => {
    stopPolling()
    // First poll almost immediately, then every 3s.
    pollTimerRef.current = setTimeout(() => pollOnce(jobId), 500)
  }, [pollOnce, stopPolling])

  // On mount: an admin URL (#admin, #admin/<id>) opens the admin panel;
  // otherwise resume any in-flight job stored in localStorage (also resumed
  // when an admin URL is open, just without switching screens).
  useEffect(() => {
    const route = parseAdminHash(window.location.hash)
    if (route) {
      setAdminJobId(route.view === 'detail' ? route.jobId : null)
      setScreen(route.view === 'detail' ? 'admin-detail' : 'admin')
    }
    const active = getActiveJob()
    if (active && active.job_id) {
      if (active.customer) setCustomer(active.customer)
      setActiveJobId(active.job_id)
      setPhase('queued')
      if (!route) setScreen('processing')
      startPolling(active.job_id)
    }
    return stopPolling
  }, [startPolling, stopPolling])

  // Browser back/forward and typed URLs between admin screens and the app.
  useEffect(() => {
    const onHash = () => {
      const route = parseAdminHash(window.location.hash)
      if (route) {
        setAdminJobId(route.view === 'detail' ? route.jobId : null)
        setScreen(route.view === 'detail' ? 'admin-detail' : 'admin')
      } else if (isAdminScreen(screenRef.current)) {
        setScreen('home')
      }
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const openAdmin = useCallback(() => {
    setAdminJobId(null)
    setScreen('admin')
    setAdminHash('#admin')
  }, [])

  const openAdminDetail = useCallback((jobId, listState) => {
    if (listState) setAdminListState(listState)
    setAdminJobId(jobId)
    setScreen('admin-detail')
    setAdminHash(`#admin/${jobId}`)
  }, [])

  // After AdminDetail deleted a consultation: forget it locally (stored
  // in-flight job, polling, a loaded result), then back to a fresh list.
  const onAdminDeleted = useCallback((jobId) => {
    forgetJob(jobId)
    if (activeJobId === jobId) {
      stopPolling()
      setActiveJobId(null)
      setPhase(null)
      setJobResult(null)
    }
    setAdminJobId(null)
    setScreen('admin')
    replaceAdminHash('#admin')
  }, [activeJobId, stopPolling])

  const leaveAdmin = useCallback(() => {
    clearAdminHash()
    setScreen('home')
  }, [])

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
        // Capture tags uploads ('3day-app-upload'); recordings and pastes here.
        source: payload.source || (audioBlob ? '3day-app-record' : '3day-app-paste'),
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

  return (
    <div className={isAdminScreen(screen) ? 'app app-wide' : 'app'}>
      {screen === 'home' && (
        <Home
          onStart={() => setScreen('pick')}
          onConsultations={openAdmin}
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
      {screen === 'admin' && (
        <AdminList onBack={leaveAdmin} onOpen={openAdminDetail} initial={adminListState} />
      )}
      {screen === 'admin-detail' && adminJobId && (
        <AdminDetail key={adminJobId} jobId={adminJobId} onBack={openAdmin} onDeleted={onAdminDeleted} />
      )}
    </div>
  )
}
