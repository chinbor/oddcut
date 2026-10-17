import React, { useEffect, useMemo, useRef, useState } from 'react'
import Icon from './icons.jsx'

// Bottom timeline: ruler + playhead, picture / sticker / drawn / music tracks, beat lines through all of them.
// Wheel zooms around the pointer (Shift + wheel or a touchpad scrolls sideways). Click a clip to select it
// (again to deselect); drag the line between clips to move a cut, which jumps onto the nearest beat while
// 切点对齐节拍 is on (Alt = the other way round); drag a sticker bar's ends or its keyframe diamonds.

const LABEL_W = 92
const fmtS = (t) => (Number.isInteger(t) ? `${t}s` : `${t.toFixed(1)}s`)

export default function Timeline({ plan, thumbs, wave, time, onSeek, selected, onSelect, stickerName, styleName,
                                   draft, onInOut, onCutMove, busy, pending, live, looks, keySel, onKeySelect, onKeyTime,
                                   metronome, setMetronome }) {
  const scroller = useRef(null)
  const canvas = useRef(null)
  const [width, setWidth] = useState(800)
  const [zoom, setZoom] = useState(1)              // 1 = whole edit fits
  const [zoomNote, setZoomNote] = useState(null)
  const [snap, setSnap] = useState(true)
  const [cutDrag, setCutDrag] = useState(null)
  const zoomTimer = useRef(null)
  const zoomRef = useRef(1)                          // latest zoom, for wheel events that arrive faster than renders
  const duration = plan.duration
  const fps = plan.fps || 30

  useEffect(() => {
    const el = scroller.current
    const ro = new ResizeObserver(() => setWidth(el.clientWidth))
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])
  const fit = Math.max(16, (width - LABEL_W - 28) / Math.max(1, duration))
  const scale = fit * zoom
  const x = (t) => LABEL_W + t * scale

  // zoom around a point on screen (the pointer for the wheel, the playhead for the buttons)
  const zoomTo = (z, clientX = null) => {
    const el = scroller.current
    const r = el.getBoundingClientRect()
    const px = clientX == null ? x(time) - el.scrollLeft : clientX - r.left
    const t = (px + el.scrollLeft - LABEL_W) / (fit * zoomRef.current)
    const nz = Math.min(16, Math.max(1, z))
    zoomRef.current = nz
    setZoom(nz)
    requestAnimationFrame(() => { el.scrollLeft = LABEL_W + t * fit * nz - px })
    setZoomNote(nz <= 1.001 ? '适应宽度' : `${nz.toFixed(1)}×`)
    clearTimeout(zoomTimer.current)
    zoomTimer.current = setTimeout(() => setZoomNote(null), 900)
  }
  useEffect(() => {
    const el = scroller.current
    const onWheel = (e) => {
      if (e.shiftKey && !e.ctrlKey) { e.preventDefault(); el.scrollLeft += e.deltaY || e.deltaX; return }
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return            // touchpad sideways: native scroll
      e.preventDefault()
      zoomTo(zoomRef.current * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })
  useEffect(() => {                                // keep the playhead in view
    const el = scroller.current
    const px = x(time)
    if (px < el.scrollLeft + LABEL_W || px > el.scrollLeft + el.clientWidth - 24) el.scrollLeft = px - LABEL_W - 60
  }, [time])

  // music: muted waveform, beat ticks along the top (downbeats orange and taller)
  const g = wave?.grid || {}
  useEffect(() => {
    const c = canvas.current
    if (!c || !wave) return
    const w = Math.ceil(duration * scale), h = 56
    const dpr = window.devicePixelRatio || 1
    c.width = w * dpr; c.height = h * dpr
    c.style.width = `${w}px`; c.style.height = `${h}px`
    const ctx = c.getContext('2d')
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, w, h)
    ctx.fillStyle = '#4A505C'
    wave.peaks.forEach((p, i) => {
      const ph = Math.max(1, p * 20)
      ctx.fillRect(i * wave.step * scale, 32 - ph, Math.max(1, wave.step * scale - 0.6), ph * 2)
    })
    ;(g.t || []).forEach((bt, i) => {
      if (!g.on_beat?.[i]) return
      const down = g.downbeat?.[i]
      ctx.fillStyle = down ? '#FF8A5C' : '#8A91A0'
      ctx.fillRect(bt * scale - (down ? 1 : 0.5), 0, down ? 2 : 1, down ? 10 : 5)
    })
  }, [wave, scale, duration])

  const ticks = useMemo(() => {
    const every = scale > 160 ? 0.5 : scale > 70 ? 1 : scale > 30 ? 2 : 5
    const out = []
    for (let t = 0; t <= duration + 1e-6; t += every) out.push(Math.round(t * 100) / 100)
    return out
  }, [scale, duration])

  // beats as a list: {t, no, down, bar, half}
  const beats = useMemo(() => (g.t || []).map((t, i) => ({ t, no: g.beat_no?.[i] || 0, down: !!g.downbeat?.[i], bar: g.bar?.[i] || 0,
                                                           half: !g.on_beat?.[i] })), [wave])
  const nearestBeat = (t) => beats.reduce((b, v) => (!b || Math.abs(v.t - t) < Math.abs(b.t - t) ? v : b), null)
  const beatLabel = (b) => (b.half ? '对齐半拍' : `对齐第 ${b.no} 拍${b.down ? ' · 重拍' : ''}`)

  const tAt = (clientX) => {
    const el = scroller.current
    const r = el.getBoundingClientRect()
    return (clientX - r.left + el.scrollLeft - LABEL_W) / scale
  }
  const scrub = (e) => {
    e.preventDefault()
    const go = (ev) => onSeek(Math.min(duration - 0.01, Math.max(0, tAt(ev.clientX))))
    go(e)
    const up = () => { window.removeEventListener('pointermove', go); window.removeEventListener('pointerup', up) }
    window.addEventListener('pointermove', go)
    window.addEventListener('pointerup', up)
  }
  const pick = (i, e) => {
    e.stopPropagation()
    if (selected === i) return onSelect(null)
    onSelect(i)
    const s = plan.shots[i]
    if (time < s.t_start || time >= s.t_end) onSeek(s.t_start + 0.01)
  }
  const startCut = (e, k) => {
    if (busy) return
    e.preventDefault(); e.stopPropagation()
    const lo = plan.shots[k - 1].t_start + 0.4, hi = plan.shots[k].t_end - 0.4
    const inRange = beats.filter((b) => b.t >= lo && b.t <= hi)
    let st = null
    const at = (ev) => {
      const raw = Math.min(hi, Math.max(lo, tAt(ev.clientX)))
      const useSnap = snap !== ev.altKey
      let t = raw, beat = null
      if (useSnap && inRange.length) {
        beat = inRange.reduce((b, v) => (Math.abs(v.t - raw) < Math.abs(b.t - raw) ? v : b), inRange[0])
        t = beat.t
      }
      st = { k, t, raw, beat, useSnap }
      setCutDrag(st)
    }
    at(e)
    const up = () => {
      window.removeEventListener('pointermove', at); window.removeEventListener('pointerup', up)
      setCutDrag(null)
      if (st && Math.abs(st.t - plan.shots[k].t_start) > 0.005) onCutMove(k, st.t, st.useSnap)
    }
    window.addEventListener('pointermove', at); window.addEventListener('pointerup', up)
  }
  const edge = (e, i, which) => {
    e.preventDefault(); e.stopPropagation()
    const s = plan.shots[i]
    const len = s.t_end - s.t_start
    let v
    let moved = false
    const mv = (ev) => {
      const t = Math.min(len, Math.max(0, tAt(ev.clientX) - s.t_start))
      v = which === 'in' ? Math.min(t, (draft.out ?? len) - 0.2) : (t >= len - 0.02 ? null : Math.max(t, (draft.in || 0) + 0.2))
      moved = true
      onInOut(which, v, false)
    }
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) onInOut(which, v, true) }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }
  // keyframe diamonds: click = select + jump there; drag = move in time (whole frames, between its neighbours)
  const diamond = (e, i, j, k, keys) => {
    e.stopPropagation(); e.preventDefault()
    const s = plan.shots[i]
    if (selected !== i) onSelect(i)
    onKeySelect(j)
    onSeek(s.t_start + k.t + 0.001)
    if (!draft || selected !== i) return
    const lo = j > 0 ? keys[j - 1].t + 1 / fps : 0
    const hi = j < keys.length - 1 ? keys[j + 1].t - 1 / fps : s.t_end - s.t_start - 1 / fps
    let moved = false, tt = k.t
    const mv = (ev) => {
      tt = Math.round(Math.min(hi, Math.max(lo, tAt(ev.clientX) - s.t_start)) * fps) / fps
      moved = true
      onKeyTime(j, tt, false)
      onSeek(s.t_start + tt + 0.001)
    }
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); if (moved) onKeyTime(j, tt, true) }
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  }

  const cutState = (t) => {
    const b = nearestBeat(t)
    if (b && Math.abs(b.t - t) < 0.6 / fps) return { on: true, text: b.half ? '踩在半拍上' : `踩在第 ${b.no} 拍${b.down ? '（重拍）' : ''}` }
    return { on: false, text: '这个切点不在节拍上' }
  }
  const near = cutDrag ? cutDrag.raw : null

  return (
    <section className="timeline">
      <div className="tl-toolbar">
        <button className={`toggle ${snap ? 'on' : ''}`} onClick={() => setSnap((v) => !v)} aria-pressed={snap}
                title="拖动镜头切点时，自动吸到最近的节拍上；按住 Alt 临时自由拖">
          <Icon name="magnet" size={18} />切点对齐节拍
        </button>
        <button className={`toggle ${metronome ? 'on' : ''}`} onClick={() => setMetronome((v) => !v)} aria-pressed={metronome}
                title="播放时在每一拍上响一声（重拍音更高），帮你听出节奏">
          <Icon name="music" size={18} />播放时听节拍
        </button>
        <span className="legend"><i className="lg-on" />踩点的切点<i className="lg-off" />没踩点</span>
        <span className="spacer" />
        {live && live.total > live.done && (
          <span className="live-note"><i className="spin" />正在把贴纸和手绘加进预览（{live.done} / {live.total}）</span>
        )}
        <div className="zoom-btns" title="滚轮缩放（Shift + 滚轮左右移动）">
          <button className="icon" onClick={() => zoomTo(zoom / 1.5)} disabled={zoom <= 1} aria-label="缩小">−</button>
          <button className="icon" onClick={() => zoomTo(zoom * 1.5)} disabled={zoom >= 16} aria-label="放大">+</button>
          <button className="fit" onClick={() => zoomTo(1)} disabled={zoom <= 1}>适应</button>
        </div>
      </div>
      <div className="tl-scroll" ref={scroller}>
        <div className="tl-inner" style={{ width: LABEL_W + duration * scale + (zoom > 1 ? 20 : 8) }}>
          <div className="tl-ruler" onPointerDown={scrub}>
            <span className="lab-pad" />
            {ticks.map((t) => <span key={t} className="tick" style={{ left: x(t) }}>{fmtS(t)}</span>)}
            {beats.filter((b) => b.down && b.bar > 0).map((b) => (
              <span key={`bar${b.t}`} className="bar-no" style={{ left: x(b.t) }} title={`第 ${b.bar} 小节的第一拍（重拍）`}>{b.bar}</span>
            ))}
          </div>

          <div className="beat-lines" style={{ left: 0 }}>
            {beats.map((b) => {
              const hot = near != null && Math.abs(b.t - near) < 1.2
              const target = cutDrag?.beat && cutDrag.beat.t === b.t
              return <i key={b.t} className={`bl ${b.down ? 'down' : b.half ? 'half' : ''} ${hot && snap ? 'hot' : ''} ${target ? 'target' : ''} ${cutDrag && !cutDrag.useSnap ? 'dim' : ''}`}
                        style={{ left: x(b.t) }} />
            })}
          </div>

          <div className="track video">
            <label><Icon name="film" size={16} />画面</label>
            {plan.shots.map((s, i) => (
              <div key={i} className={`clip ${selected === i ? 'sel' : ''} ${pending.includes(i) ? 'pending' : ''}`}
                   style={{ left: x(s.t_start) + 1, width: Math.max(3, (s.t_end - s.t_start) * scale - 2),
                            backgroundImage: thumbs?.[i] ? `url(${thumbs[i]})` : undefined }}
                   onPointerDown={(e) => pick(i, e)} title={`镜头 ${i + 1} · ${(s.t_end - s.t_start).toFixed(1)} 秒`}>
                <span className="num">{i + 1}</span>
                {pending.includes(i) && <span className="gen">生成中</span>}
              </div>
            ))}
            {plan.shots.slice(1).map((s, j) => {
              const cs = cutState(s.t_start)
              return (
                <i key={`c${j}`} className={`cut ${busy ? 'off' : ''} ${cutDrag?.k === j + 1 ? 'dragging' : ''}`} style={{ left: x(s.t_start) - 5 }}
                   onPointerDown={(e) => startCut(e, j + 1)} title={`${cs.text}。拖动可调整切点`}>
                  <b className={`cut-dot ${cs.on ? 'on' : ''}`} />
                </i>
              )
            })}
          </div>

          <div className="track bars sticker">
            <label><Icon name="sticker" size={16} />贴纸</label>
            {plan.shots.map((s, i) => {
              const d = draft && selected === i ? draft : null
              const at = d || s.sticker_at
              if (!s.sticker && !d) return null
              const len = s.t_end - s.t_start
              const tIn = at?.in || 0
              const tOut = at?.out ?? len
              const id = (d?.id) || s.sticker.split('@')[0]
              const img = looks.stickers.find((x2) => x2.id === id)?.preview
              const keys = at?.keys || []
              return (
                <div key={i} className={`bar ${s.sticker_at || d ? 'manual' : 'auto'} ${selected === i ? 'sel' : ''}`}
                     style={{ left: x(s.t_start + tIn) + 1, width: Math.max(6, (tOut - tIn) * scale - 2) }}
                     onPointerDown={(e) => pick(i, e)} title={stickerName(s.sticker || id)}>
                  {d && <i className="edge l" onPointerDown={(e) => edge(e, i, 'in')} title="拖动改出现时间" />}
                  {img && <img src={img} alt="" />}
                  <span>{stickerName(s.sticker || id)}{s.sticker_at ? ' ✎' : ''}</span>
                  {(at?.mode === 'still' ? keys.slice(0, 1) : keys).map((k, j) => (
                    <i key={j} className={`diamond ${selected === i && keySel === j ? 'sel' : ''}`} style={{ left: (k.t - tIn) * scale - 5 }}
                       title={`第 ${j + 1} 个关键帧 · ${k.t.toFixed(2)} 秒（拖动改时间）`} onPointerDown={(e) => diamond(e, i, j, k, keys)} />
                  ))}
                  {d && <i className="edge r" onPointerDown={(e) => edge(e, i, 'out')} title="拖动改消失时间" />}
                </div>
              )
            })}
          </div>

          <div className="track bars drawn">
            <label><Icon name="brush" size={16} />手绘</label>
            {plan.shots.map((s, i) => s.drawn && (
              <div key={i} className={`bar ${s.drawn.manual ? 'manual' : 'auto'} ${selected === i ? 'sel' : ''}`}
                   style={{ left: x(s.t_start) + 1, width: Math.max(6, (s.t_end - s.t_start) * scale - 2) }}
                   onPointerDown={(e) => pick(i, e)} title={`手绘 · ${styleName(s.drawn.style)}`}>
                <span>{styleName(s.drawn.style)}{s.drawn.manual ? ' ✎' : ''}</span>
                <i className="reveal" style={{ left: s.drawn.reveal_at * scale }} title="在这里变回实拍（踩在重拍上）" />
              </div>
            ))}
          </div>

          <div className="track music">
            <label><Icon name="music" size={16} />音乐</label>
            <canvas ref={canvas} style={{ position: 'absolute', left: LABEL_W, top: 4 }} />
          </div>

          {cutDrag && (
            <div className={`cut-ghost ${cutDrag.beat ? 'snapped' : 'free'}`} style={{ left: x(cutDrag.t) }}>
              <span key={cutDrag.beat ? cutDrag.beat.t : 'free'}>
                {cutDrag.beat ? <><Icon name="magnet" size={12} />{beatLabel(cutDrag.beat)}</> : snap ? `自由位置 ${cutDrag.t.toFixed(2)}s` : `自由拖动 ${cutDrag.t.toFixed(2)}s`}
              </span>
            </div>
          )}
          <div className="playhead" style={{ left: x(time) }} onPointerDown={scrub} />
          {zoomNote && <div className="zoom-note" style={{ left: (scroller.current?.scrollLeft || 0) + (width / 2) }}>{zoomNote}</div>}
        </div>
      </div>
    </section>
  )
}
