let notificationAudioContext: AudioContext | null = null

function getAudioContext() {
  if (notificationAudioContext) return notificationAudioContext
  if (typeof window.AudioContext !== 'function') return null
  try {
    notificationAudioContext = new window.AudioContext()
    return notificationAudioContext
  } catch {
    return null
  }
}

function scheduleTone(
  context: AudioContext,
  destination: AudioNode,
  frequency: number,
  startsAt: number,
  duration: number,
) {
  const oscillator = context.createOscillator()
  const envelope = context.createGain()
  oscillator.type = 'sine'
  oscillator.frequency.setValueAtTime(frequency, startsAt)
  envelope.gain.setValueAtTime(0.0001, startsAt)
  envelope.gain.exponentialRampToValueAtTime(0.22, startsAt + 0.018)
  envelope.gain.exponentialRampToValueAtTime(0.0001, startsAt + duration)
  oscillator.connect(envelope)
  envelope.connect(destination)
  oscillator.start(startsAt)
  oscillator.stop(startsAt + duration + 0.02)
}

export async function unlockHandoffAudio() {
  const context = getAudioContext()
  if (!context) return false
  if (context?.state === 'suspended') {
    try {
      await context.resume()
    } catch {
      // Browser autoplay policy may keep audio locked until another gesture.
    }
  }
  return context.state === 'running'
}

export async function playHandoffChime() {
  const context = getAudioContext()
  if (!context) return
  if (context.state === 'suspended') {
    try {
      await context.resume()
    } catch {
      return
    }
  }
  if (context.state !== 'running') return
  const master = context.createGain()
  master.gain.setValueAtTime(0.7, context.currentTime)
  master.connect(context.destination)
  const startsAt = context.currentTime + 0.02
  scheduleTone(context, master, 783.99, startsAt, 0.2)
  scheduleTone(context, master, 587.33, startsAt + 0.23, 0.32)
}
