import React, { useEffect, useMemo, useRef, useState } from 'react'

// Editor-style timeline: ruler + playhead, and tracks for video (one block per shot), stickers, drawn shots
// and music (waveform + beats). Click a block to select its shot; click / drag the ruler to scrub;
// ctrl + wheel zooms around the pointer, the panel scrolls sideways.

const ROLE_COLORS = {
  hook: '#7aa2ff', drawn: '#ff9f6b', body: '#5c6378', breath: '#5fc9a8', sticker: '#ffd166', outro: '#b18cff',
}
const LABEL_W = 56

export default function Timeline({ plan, thumbs, wave, time, onSeek, selected, onSelect, stickerName, styleName,
                                   draft, onDraft, onKeySelect }) {
  const scroller = useRef(null)
  const canvas = useRef(null)
  const [pps, setPps] = useState(null)          // pixels per second; null = fit the width
  const [width, setWidth] = useState(800)
  const dragging = useRef(false)
  const duration = plan.duration

  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const ro = new ResizeObserver(() => setWidth(el.clientWidth))
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  const fit = Math.max(20, (width - LABEL_W - 16) / Math.max(1, duration))
  const scale = pps ?? fit
  const total = LABEL_W + duration * scale + 16
  const x = (t) => LABEL_W + t * scale

  // ctrl + wheel: zoom around the pointer
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    const onWheel = (e) => {
      if (!e.ctrlKey) return
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const px = e.clientX - rect.left + el.scrollLeft
      const t = (px - LABEL_W) / scale
      const next = Math.min(400, Math.max(fit, scale * (e.deltaY < 0 ? 1.2 : 1 / 1.2)))
      setPps(next <= fit * 1.001 ? null : next)
      requestAnimationFrame(() => { el.scrollLeft = LABEL_W + t * next - (e.clientX - rect.left) })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [scale, fit])

  // keep the playhead in view while playing
  useEffect(() => {
    const el = scroller.current
    if (!el || dragging.current) return
    const px = x(time)
    if (px < el.scrollLeft + LABEL_W || px > el.scrollLeft + el.clientWidth - 20) el.scrollLeft = px - LABEL_W - 40
  }, [time, scale])

  // music track: waveform + beat ticks, drawn on a canvas
  useEffect(() => {
    const c = canvas.current
    if (!c || !wave) return
    const w = Math.ceil(duration * scale), h = 44
    const dpr = window.devicePixelRatio || 1
    c.width = w * dpr
    c.height = h * dpr
    c.style.width = `${w}px`
    c.style.height = `${h}px`
    const g = c.getContext('2d')
    g.scale(dpr, dpr)
    g.clearRect(0, 0, w, h)
    g.fillStyle = '#4a5368'
    const step = wave.step
    for (let i = 0; i < wave.peaks.length; i++) {
      const px = i * step * scale
      const ph = Math.max(1, wave.peaks[i] * (h - 8))
      g.fillRect(px, (h - ph) / 2, Math.max(1, step * scale - 0.5), ph)
    }
    const { t = [], strength = [], on_beat = [] } = wave.grid || {}
    t.forEach((bt, i) => {
      const strong = strength[i] > 0.6
      g.fillStyle = strong ? '#ffb38f' : on_beat[i] ? '#8a93a8' : '#5a6275'
      g.fillRect(bt * scale, strong ? 0 : on_beat[i] ? h - 10 : h - 6, strong ? 2 : 1, strong ? h : on_beat[i] ? 10 : 6)
    })
  }, [wave, scale, duration])

  const ticks = useMemo(() => {
    const every = scale > 120 ? 0.5 : scale > 50 ? 1 : scale > 25 ? 2 : 5
    const out = []
    for (let t = 0; t <= duration + 1e-6; t += every) out.push(Math.round(t * 100) / 100)
    return out
  }, [scale, duration])

  const seekFromEvent = (e) => {
    const el = scroller.current
    const rect = el.getBoundingClientRect()
    const t = (e.clientX - rect.left + el.scrollLeft - LABEL_W) / scale
    onSeek(Math.min(duration - 0.01, Math.max(0, t)))
  }
  const startScrub = (e) => {
    dragging.current = true
    seekFromEvent(e)
    const move = (ev) => seekFromEvent(ev)
    const up = () => { dragging.current = false; window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const select = (i, e) => {
    e.stopPropagation()
    onSelect(i)
    const s = plan.shots[i]
    if (time < s.t_start || time >= s.t_end) onSeek(s.t_start + 0.01)
  }

  return (
    <div className="timeline-panel">
      <div className="tl-head">
        <span>时间线</span>
        <small>点镜头编辑 · 拖时间尺定位 · 空格播放 / 暂停 · Ctrl + 滚轮缩放</small>
        <button className="mini" onClick={() => setPps(null)} disabled={pps == null}>适应宽度</button>
      </div>
      <div className="tl-scroll" ref={scroller}>
        <div className="tl-inner" style={{ width: total }}>
          <div className="tl-ruler" onPointerDown={startScrub}>
            {ticks.map((t) => (
              <span key={t} className="tick" style={{ left: x(t) }}>{Number.isInteger(t) ? `${t}s` : ''}</span>
            ))}
          </div>

          <div className="tl-track">
            <label>视频</label>
            {plan.shots.map((s, i) => (
              <div key={i} className={`blk video ${selected === i ? 'sel' : ''}`}
                   style={{ left: x(s.t_start), width: Math.max(2, (s.t_end - s.t_start) * scale - 2),
                            borderTopColor: ROLE_COLORS[s.role] || '#5c6378',
                            backgroundImage: thumbs?.[i] ? `url(${thumbs[i]})` : undefined }}
                   title={`${i + 1} · ${s.clip} · ${(s.t_end - s.t_start).toFixed(2)}s`}
                   onPointerDown={(e) => select(i, e)}>
                <span className="num">{i + 1}</span>
              </div>
            ))}
          </div>

          <div className="tl-track">
            <label>贴纸</label>
            {plan.shots.map((s, i) => {
              const d = draft && draft.index === i ? draft : null      // being edited: show the draft
              const at = d || s.sticker_at
              if (!d && !s.sticker) return null
              const len = s.t_end - s.t_start
              const tIn = at?.in || 0
              const tOut = at?.out ?? len
              const edge = (e, which) => {
                e.stopPropagation()
                e.preventDefault()
                const el = scroller.current
                const move = (ev) => {
                  const r = el.getBoundingClientRect()
                  const t = Math.min(len, Math.max(0, (ev.clientX - r.left + el.scrollLeft - LABEL_W) / scale - s.t_start))
                  if (which === 'in') onDraft({ ...d, in: Math.min(t, tOut - 0.2) })
                  else onDraft({ ...d, out: t >= len - 0.02 ? null : Math.max(t, tIn + 0.2) })
                }
                const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
                window.addEventListener('pointermove', move)
                window.addEventListener('pointerup', up)
              }
              return (
                <div key={i} className={`blk bar sticker ${at ? 'manual' : ''} ${selected === i ? 'sel' : ''} ${d ? 'editing' : ''}`}
                     style={{ left: x(s.t_start + tIn), width: Math.max(4, (tOut - tIn) * scale - 2) }}
                     onPointerDown={(e) => select(i, e)} title={d ? '拖两端改出现 / 消失的时间' : stickerName(s.sticker)}>
                  {d && <i className="edge l" onPointerDown={(e) => edge(e, 'in')} />}
                  <span>{d ? (s.sticker && s.sticker.split('@')[0] === d.id ? stickerName(s.sticker) : '新贴纸') : stickerName(s.sticker)}{at ? ' ✎' : ''}</span>
                  {(at?.keys || []).map((k, j) => (
                    <i key={j} className="key" style={{ left: (k.t - tIn) * scale - 4 }} title={`关键帧 ${k.t.toFixed(2)}s`}
                       onPointerDown={(e) => { e.stopPropagation(); if (selected !== i) onSelect(i); onKeySelect?.(j); onSeek(s.t_start + k.t + 0.001) }} />
                  ))}
                  {d && <i className="edge r" onPointerDown={(e) => edge(e, 'out')} />}
                </div>
              )
            })}
          </div>

          <div className="tl-track">
            <label>手绘</label>
            {plan.shots.map((s, i) => s.drawn && (
              <div key={i} className={`blk bar drawn ${s.drawn.manual ? 'manual' : ''} ${selected === i ? 'sel' : ''}`}
                   style={{ left: x(s.t_start), width: Math.max(4, (s.t_end - s.t_start) * scale - 2) }}
                   onPointerDown={(e) => select(i, e)} title={`手绘 · ${styleName(s.drawn.style)}`}>
                <span>{styleName(s.drawn.style)}{s.drawn.manual ? ' ✎' : ''}</span>
                <i className="reveal" style={{ left: s.drawn.reveal_at * scale }} title="变回实拍" />
              </div>
            ))}
          </div>

          <div className="tl-track music">
            <label>音乐</label>
            <canvas ref={canvas} style={{ position: 'absolute', left: LABEL_W, top: 2 }} />
          </div>

          <div className="playhead" style={{ left: x(time) }} onPointerDown={startScrub} />
        </div>
      </div>
    </div>
  )
}
