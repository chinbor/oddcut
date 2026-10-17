import React, { useEffect, useMemo, useRef, useState } from 'react'
import Icon from './icons.jsx'

// Bottom timeline: ruler + playhead, picture / sticker / drawn / music tracks. Click a clip to select it (again to
// deselect), drag between clips to move a cut (snaps to beats unless 吸附节拍 is off or Alt is held), drag a
// sticker bar's ends to set when it shows, click a diamond to jump to that keyframe.

const LABEL_W = 92
const fmtS = (t) => (Number.isInteger(t) ? `${t}s` : `${t.toFixed(1)}s`)

export default function Timeline({ plan, thumbs, wave, time, onSeek, selected, onSelect, stickerName, styleName,
                                   draft, onInOut, onCutMove, busy, pending, live, looks }) {
  const scroller = useRef(null)
  const canvas = useRef(null)
  const [width, setWidth] = useState(800)
  const [zoom, setZoom] = useState(1)              // 1 = whole edit fits
  const [snap, setSnap] = useState(true)
  const [cutDrag, setCutDrag] = useState(null)
  const duration = plan.duration

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

  useEffect(() => {
    const el = scroller.current
    const onWheel = (e) => {
      if (!e.ctrlKey) return
      e.preventDefault()
      setZoom((z) => Math.min(12, Math.max(1, z * (e.deltaY < 0 ? 1.2 : 1 / 1.2))))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])
  useEffect(() => {                                // keep the playhead in view
    const el = scroller.current
    const px = x(time)
    if (px < el.scrollLeft + LABEL_W || px > el.scrollLeft + el.clientWidth - 24) el.scrollLeft = px - LABEL_W - 60
  }, [time, scale])

  // music: muted waveform, beat ticks along the top (strong beats orange and taller)
  useEffect(() => {
    const c = canvas.current
    if (!c || !wave) return
    const w = Math.ceil(duration * scale), h = 56
    const dpr = window.devicePixelRatio || 1
    c.width = w * dpr; c.height = h * dpr
    c.style.width = `${w}px`; c.style.height = `${h}px`
    const g = c.getContext('2d')
    g.scale(dpr, dpr)
    g.clearRect(0, 0, w, h)
    g.fillStyle = '#4A505C'
    const mid = 32
    wave.peaks.forEach((p, i) => {
      const ph = Math.max(1, p * 22)
      g.fillRect(i * wave.step * scale, mid - ph, Math.max(1, wave.step * scale - 0.6), ph * 2)
    })
    const { t = [], strength = [] } = wave.grid || {}
    t.forEach((bt, i) => {
      const strong = strength[i] > 0.6
      g.fillStyle = strong ? '#FF8A5C' : '#7C8391'
      g.fillRect(bt * scale - (strong ? 1 : 0.5), 0, strong ? 2 : 1, strong ? 9 : 5)
    })
  }, [wave, scale, duration])

  const ticks = useMemo(() => {
    const every = scale > 160 ? 0.5 : scale > 70 ? 1 : scale > 30 ? 2 : 5
    const out = []
    for (let t = 0; t <= duration + 1e-6; t += every) out.push(Math.round(t * 100) / 100)
    return out
  }, [scale, duration])

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
    const beats = (wave?.grid?.t || []).filter((b) => b >= lo && b <= hi)
    let st = null
    const at = (ev) => {
      let t = Math.min(hi, Math.max(lo, tAt(ev.clientX)))
      const useSnap = snap !== ev.altKey
      let snapped = false
      if (useSnap && beats.length) {
        t = beats.reduce((b, v) => (Math.abs(v - t) < Math.abs(b - t) ? v : b), beats[0])
        snapped = true
      }
      st = { k, t, snapped, useSnap }
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

  return (
    <section className="timeline">
      <div className="tl-toolbar">
        <button className={`toggle ${snap ? 'on' : ''}`} onClick={() => setSnap((v) => !v)} title="切点自动吸附到节拍（拖动时按住 Alt 临时反过来）">
          <Icon name="magnet" size={18} />吸附节拍
        </button>
        <span className="caption">拖镜头之间的竖线调切点</span>
        <span className="spacer" />
        {live && live.total > live.done && (
          <span className="live-note"><i className="spin" />正在把贴纸和手绘加进预览（{live.done} / {live.total}）</span>
        )}
        <label className="zoom" title="缩放（Ctrl + 滚轮）">
          <span>缩放</span>
          <input type="range" min="1" max="12" step="0.1" value={zoom} onChange={(e) => setZoom(Number(e.target.value))} />
        </label>
      </div>
      <div className="tl-scroll" ref={scroller}>
        <div className="tl-inner" style={{ width: LABEL_W + duration * scale + (zoom > 1 ? 20 : 8) }}>
          <div className="tl-ruler" onPointerDown={scrub}>
            <span className="lab-pad" />
            {ticks.map((t) => <span key={t} className="tick" style={{ left: x(t) }}>{fmtS(t)}</span>)}
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
            {plan.shots.slice(1).map((s, j) => (
              <i key={`c${j}`} className={`cut ${busy ? 'off' : ''}`} style={{ left: x(s.t_start) - 5 }} onPointerDown={(e) => startCut(e, j + 1)} />
            ))}
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
              return (
                <div key={i} className={`bar ${s.sticker_at || d ? 'manual' : 'auto'} ${selected === i ? 'sel' : ''}`}
                     style={{ left: x(s.t_start + tIn) + 1, width: Math.max(6, (tOut - tIn) * scale - 2) }}
                     onPointerDown={(e) => pick(i, e)} title={stickerName(s.sticker || id)}>
                  {d && <i className="edge l" onPointerDown={(e) => edge(e, i, 'in')} title="拖动改出现时间" />}
                  {img && <img src={img} alt="" />}
                  <span>{stickerName(s.sticker || id)}{s.sticker_at ? ' ✎' : ''}</span>
                  {(at?.keys || []).map((k, j) => (
                    <i key={j} className="diamond" style={{ left: (k.t - tIn) * scale - 5 }} title={`关键帧 · ${k.t.toFixed(2)} 秒`}
                       onPointerDown={(e) => { e.stopPropagation(); if (selected !== i) onSelect(i); onSeek(s.t_start + k.t + 0.001) }} />
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
                <i className="reveal" style={{ left: s.drawn.reveal_at * scale }} title="在这里变回实拍" />
              </div>
            ))}
          </div>

          <div className="track music">
            <label><Icon name="music" size={16} />音乐</label>
            <canvas ref={canvas} style={{ position: 'absolute', left: LABEL_W, top: 4 }} />
          </div>

          {cutDrag && (
            <div className="cut-ghost" style={{ left: x(cutDrag.t) }}><span>{cutDrag.t.toFixed(2)}s{cutDrag.snapped ? ' · 节拍' : ''}</span></div>
          )}
          <div className="playhead" style={{ left: x(time) }} onPointerDown={scrub} />
        </div>
      </div>
    </section>
  )
}
