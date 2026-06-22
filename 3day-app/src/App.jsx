import { useCallback, useState } from 'react'
import { submitConsultation } from './lib/api.js'
import Home from './screens/Home.jsx'
import CustomerPicker from './screens/CustomerPicker.jsx'
import NewCustomer from './screens/NewCustomer.jsx'
import Capture from './screens/Capture.jsx'
import Processing from './screens/Processing.jsx'
import Confirmation from './screens/Confirmation.jsx'
import ErrorScreen from './screens/ErrorScreen.jsx'

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

function explainError(data, httpStatus) {
  const s = data && data.status
  if (FRIENDLY[s]) {
    return { ...FRIENDLY[s], detail: data && data.error }
  }
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

export default function App() {
  const [screen, setScreen] = useState('home')
  const [customer, setCustomer] = useState(null)
  const [result, setResult] = useState(null)
  const [errorState, setErrorState] = useState(null)
  const [lastPayload, setLastPayload] = useState(null)

  const reset = useCallback(() => {
    setCustomer(null)
    setResult(null)
    setErrorState(null)
    setLastPayload(null)
    setScreen('home')
  }, [])

  const handleSubmit = useCallback(
    async (payload) => {
      setLastPayload(payload)
      setScreen('processing')
      try {
        const { ok, status, data } = await submitConsultation({
          ...payload,
          customerId: customer && customer.id,
        })
        if (ok && data && data.status === 'success') {
          setResult(data)
          setScreen('confirmation')
        } else {
          setErrorState({ ...explainError(data, status), data })
          setScreen('error')
        }
      } catch (e) {
        setErrorState({
          title: "Couldn't reach the server",
          message: (e && e.message) || String(e),
        })
        setScreen('error')
      }
    },
    [customer],
  )

  const retry = useCallback(() => {
    if (lastPayload) handleSubmit(lastPayload)
    else setScreen('capture')
  }, [lastPayload, handleSubmit])

  return (
    <div className="app">
      {screen === 'home' && <Home onStart={() => setScreen('pick')} />}
      {screen === 'pick' && (
        <CustomerPicker
          onBack={() => setScreen('home')}
          onSelect={(c) => {
            setCustomer(c)
            setScreen('capture')
          }}
          onAddNew={() => setScreen('new-customer')}
        />
      )}
      {screen === 'new-customer' && (
        <NewCustomer
          onBack={() => setScreen('pick')}
          onCreated={(c) => {
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
      {screen === 'processing' && <Processing customer={customer} />}
      {screen === 'confirmation' && (
        <Confirmation customer={customer} result={result} onDone={reset} />
      )}
      {screen === 'error' && (
        <ErrorScreen error={errorState} onRetry={retry} onHome={reset} />
      )}
    </div>
  )
}
