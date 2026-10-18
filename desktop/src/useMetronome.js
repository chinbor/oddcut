// Metronome: a click on every beat while playing (downbeats higher).
import { useEffect, useRef } from 'react'

export default function useMetronome(playing, metronome, wave, videoRef) {
  const audio = useRef(null)
  useEffect(() => {
    if (!playing || !metronome || !wave?.grid?.t?.length) return
    const ctx = audio.current || (audio.current = new AudioContext())
    let prev = videoRef.current?.currentTime ?? 0
    let id
    const loop = () => {
      const now = videoRef.current?.currentTime ?? prev
      if (now > prev) {
        wave.grid.t.forEach((b, i) => {
          if (b > prev && b <= now && wave.grid.on_beat?.[i]) {
            const o = ctx.createOscillator(), g = ctx.createGain()
            o.frequency.value = wave.grid.downbeat?.[i] ? 1760 : 1100
            g.gain.setValueAtTime(0.35, ctx.currentTime)
            g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.05)
            o.connect(g).connect(ctx.destination)
            o.start(); o.stop(ctx.currentTime + 0.06)
          }
        })
      }
      prev = now
      id = requestAnimationFrame(loop)
    }
    id = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(id)
  }, [playing, metronome, wave])
}
