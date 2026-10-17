import React, { useEffect, useRef, useState } from 'react'
import Icon from './icons.jsx'

const fmt = (t) => {
  const m = Math.floor(t / 60)
  const s = t - m * 60
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`
}

// Ramer-Douglas-Peucker on [t, x, y] samples (x, y in 0..1): keeps the corners of a recorded drag
function rdp(pts, eps) {
  if (pts.length < 3) return pts
  const [a, b] = [pts[0], pts[pts.length - 1]]
  let best = -1, idx = 0
  for (let i = 1; i < pts.length - 1; i++) {
    const p = pts[i]
    const u = (p[0] - a[0]) / Math.max(1e-6, b[0] - a[0])
    const d = Math.hypot(p[1] - (a[1] + (b[1] - a[1]) * u), p[2] - (a[2] + (b[2] - a[2]) * u))
    if (d > best) { best = d; idx = i }
  }
  if (best <= eps) return [a, b]
  return [...rdp(pts.slice(0, idx + 1), eps).slice(0, -1), ...rdp(pts.slice(idx), eps)]
}

// Centre of the editor: the preview (no native controls), our transport bar, the beat indicator, and direct
// manipulation of the selected shot's sticker: drag it to move, a corner to resize (a keyframe is added at the
// playhead if there isn't one), keyframe dots on its path (click = select + jump there, drag = move,
// right-click = menu), and 边播边拖 recording.
export default function Viewer({ src, videoRef, time, onTime, playing, setPlaying, duration, fps, aspect, shot,
                                 draft, track, stickerImage, showImage, autoSticker, keySel, onSelectKey, onDraft,
                                 onMoveKey, onCommit, onDeleteKey, onStillKey, onPlaceAuto, onDropSticker, recordArmed,
                                 onRecorded, boxMode, onBox, status, wave, segment, loop, setLoop, overlays = [], ovSel,
                                 onSelectOverlay, onOverlayDraft, onOverlayCommit, clipThumbs = {}, ovShowImage }) {
  const boxRef = useRef(null)
  const [aw, ah] = aspect
  const keep = useRef({ t: 0, playing: false })
  const [hint, setHint] = useState(() => { try { return !localStorage.getItem('oddcut.hint.sticker2') } catch { return true } })
  const [boxDraft, setBoxDraft] = useState(null)
  const [menu, setMenu] = useState(null)          // right-click menu on a keyframe: {j, x, y}
  const [help, setHelp] = useState(false)
  const [rec, setRec] = useState(null)            // live recorded points while 边播边拖

  // ---- two stacked <video> layers: a new file loads behind, seeks to the same moment, then swaps in
  const [layers, setLayers] = useState([null, null])
  const [front, setFront] = useState(0)
  const vids = [useRef(null), useRef(null)]
  useEffect(() => {
    if (!src || layers[front] === src) return
    keep.current = { t: time, playing }
    const target = layers[front] ? 1 - front : front
    setLayers((l) => { const n = [...l]; n[target] = src; return n })
  }, [src])
  useEffect(() => { videoRef.current = vids[front].current })
  const onLoaded = (i) => (e) => {
    const v = e.currentTarget
    v.currentTime = Math.min(keep.current.t, Math.max(0, duration - 0.05))
    if (i === front && keep.current.playing) v.play().catch(() => {})
  }
  const onSeekedLayer = (i) => (e) => {
    const v = e.currentTarget
    if (i !== front && layers[i] === src) {
      const old = vids[front].current
      if (keep.current.playing || (old && !old.paused)) v.play().catch(() => {})
      if (old) old.pause()
      setFront(i)
      return
    }
    if (i === front && !playing) onTime(v.currentTime)
  }
  useEffect(() => {
    const v = videoRef.current
    if (v && !playing && Math.abs(v.currentTime - time) > 0.02) v.currentTime = time
  }, [time, playing])
  // a selected shot plays on its own: stop on its last frame, or go round again with 循环
  const seg = useRef(null)
  seg.current = segment ? { ...segment, loop } : null
  useEffect(() => {
    if (!playing) return
    let id
    const tick = () => {
      const v = videoRef.current
      const s = seg.current
      if (v && s && v.currentTime >= s.end - 0.5 / fps) {
        if (s.loop) v.currentTime = s.start + 0.001
        else { v.pause(); v.currentTime = s.end - 1 / fps }
      }
      if (v) onTime(v.currentTime)
      id = requestAnimationFrame(tick)
    }
    id = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(id)
  }, [playing])

  const toggle = () => {
    const v = videoRef.current
    if (!v) return
    if (!v.paused) return v.pause()
    if (segment && (v.currentTime < segment.start - 1e-3 || v.currentTime >= segment.end - 1.5 / fps)) v.currentTime = segment.start + 0.001
    v.play()
  }
  const step = (n) => {
    const v = videoRef.current
    if (v && !v.paused) v.pause()
    onTime(Math.min(duration - 0.01, Math.max(0, Math.round((time + n / fps) * fps) / fps)))
  }
  useEffect(() => {
    const onKey = (e) => {
      if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return
      if (e.key === 'Escape') setMenu(null)
      if (e.code === 'Space') { e.preventDefault(); toggle() }
      if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1) }
      if (e.key === 'ArrowRight') { e.preventDefault(); step(1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // ---- beat indicator: pulses on every beat, harder on downbeats
  const beats = wave?.grid?.t || []
  let lastBeat = -1
  for (let i = 0; i < beats.length && beats[i] <= time + 1e-3; i++) lastBeat = i
  const age = lastBeat >= 0 ? time - beats[lastBeat] : 9
  const strong = lastBeat >= 0 && wave.grid.downbeat?.[lastBeat]
  const pulse = Math.max(0, 1 - age / 0.22)

  // ---- sticker of the selected shot
  const local = shot ? time - shot.t_start : -1
  const inShot = shot && local >= 0 && local < shot.t_end - shot.t_start
  const f = track ? Math.min(track.frames - 1, Math.max(0, Math.round(local * track.fps))) : 0
  const tIn = draft?.in || 0
  const tOut = draft?.out ?? (shot ? shot.t_end - shot.t_start : 0)
  const visible = draft && track && inShot && local >= tIn && local < tOut
  const rect = () => boxRef.current.getBoundingClientRect()
  const norm = (e) => { const r = rect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))] }
  const minSidePx = () => { const r = rect(); return Math.min(r.width, r.height) }
  const dismissHint = () => { setHint(false); try { localStorage.setItem('oddcut.hint.sticker2', '1') } catch {} }

  const drag = (e, move, done = onCommit) => {
    e.preventDefault()
    e.stopPropagation()
    const mv = (ev) => move(ev)
    const up = (ev) => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); done(ev) }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', up)
  }
  const onStickerDown = (e) => {
    if (e.button !== 0) return
    if (recordArmed) return startRecording(e)
    const [x0, y0] = norm(e)
    const cx = track.x[f], cy = track.y[f]
    drag(e, (ev) => { const [x, y] = norm(ev); onDraft({ x: cx + (x - x0), y: cy + (y - y0) }) })
  }
  const resize = (e) => {
    const r = rect()
    const cx = r.left + track.x[f] * r.width, cy = r.top + track.y[f] * r.height
    drag(e, (ev) => {
      const side = 2 * Math.max(Math.abs(ev.clientX - cx), Math.abs(ev.clientY - cy))
      onDraft({ size: Math.min(0.6, Math.max(0.06, side / minSidePx())) })
    })
  }
  const onKeyDown = (e, j, k) => {
    setMenu(null)
    if (e.button === 2) return
    onSelectKey(j)
    onTime(shot.t_start + k.t + 0.001)
    let moved = false
    drag(e, (ev) => { moved = true; const [x, y] = norm(ev); onMoveKey(j, x, y) }, () => { if (moved) onCommit() })
  }

  // picture-in-picture: click to select, drag to move, corners resize (the box keeps its shape)
  const ovDown = (e, o) => {
    if (e.button !== 0) return
    if (o.id !== ovSel) onSelectOverlay(o.id)
    const [x0, y0] = norm(e)
    let moved = false
    drag(e, (ev) => { const [x, y] = norm(ev); moved = true; onOverlayDraft(o.id, { x: o.x + x - x0, y: o.y + y - y0 }) },
         () => { if (moved) onOverlayCommit() })
  }
  const ovResize = (e, o, c) => {
    const r = rect()
    const ratio = (o.w * r.width) / (o.h * r.height)            // box shape in pixels
    const ax = c.includes('w') ? o.x + o.w : o.x, ay = c.includes('n') ? o.y + o.h : o.y   // the opposite corner stays
    drag(e, (ev) => {
      const [x, y] = norm(ev)
      let wpx = Math.max(Math.abs(x - ax) * r.width, Math.abs(y - ay) * r.height * ratio, 24)
      let w = Math.min(1.5, wpx / r.width), h = (wpx / ratio) / r.height
      onOverlayDraft(o.id, { w, h, x: c.includes('w') ? ax - w : ax, y: c.includes('n') ? ay - h : ay })
    })
  }
  const frameW = boxRef.current ? boxRef.current.getBoundingClientRect().width : 0
  const frameH = boxRef.current ? boxRef.current.getBoundingClientRect().height : 0
  const liveOv = overlays.filter((o) => time >= o.t_start && time < o.t_end).sort((a, b) => a.track - b.track)

  // 边播边拖: hold the sticker, playback runs at half speed, the drag is recorded and thinned into keyframes
  const startRecording = (e) => {
    const v = videoRef.current
    if (!v) return
    e.preventDefault(); e.stopPropagation()
    const pts = []
    let last = norm(e)
    const sample = () => {
      const lt = v.currentTime - shot.t_start
      if (lt >= 0 && lt <= tOut) pts.push([lt, last[0], last[1]])
      setRec([...pts])
    }
    sample()
    v.playbackRate = 0.5
    v.play().catch(() => {})
    let id = requestAnimationFrame(function loop() {
      sample()
      if (v.currentTime - shot.t_start >= tOut - 0.01) return stop()
      id = requestAnimationFrame(loop)
    })
    const mv = (ev) => { last = norm(ev) }
    const stop = () => {
      cancelAnimationFrame(id)
      window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', stop)
      v.pause(); v.playbackRate = 1
      setRec(null)
      if (pts.length < 2) return
      // one point every ~0.25 s, then drop the ones a straight line already explains
      const t0 = pts[0][0], t1 = pts[pts.length - 1][0]
      const grid = []
      for (let t = t0; t < t1; t += 0.25) grid.push(pts.reduce((b, p) => (Math.abs(p[0] - t) < Math.abs(b[0] - t) ? p : b)))
      grid.push(pts[pts.length - 1])
      const keys = rdp(grid, 0.012).map(([t, x, y]) => ({ t: Math.round(t * fps) / fps, x, y }))
      onRecorded(keys, t0, t1)
    }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', stop)
  }

  const onSurface = (e) => {
    setMenu(null)
    if (e.button !== 0) return
    if (boxMode) {
      const [x0, y0] = norm(e)
      let b = [x0, y0, x0, y0]
      setBoxDraft(b)
      const mv = (ev) => { const [x1, y1] = norm(ev); b = [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]; setBoxDraft(b) }
      const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); setBoxDraft(null); if (b[2] - b[0] > 0.03 && b[3] - b[1] > 0.03) onBox(b) }
      window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
      return
    }
    if (autoSticker && !draft && inShot) onPlaceAuto(...norm(e))   // an auto sticker becomes a hand-placed one there
  }

  // path: whole shot, played part solid, the rest dashed; tracked paths (跟着物体) drawn thinner
  const pts = (a, b) => track.x.slice(a, b + 1).map((x, k) => `${(x * 100).toFixed(2)},${(track.y[a + k] * 100).toFixed(2)}`).join(' ')
  const showPath = draft && track && inShot && draft.mode !== 'still'
  const i0 = track ? Math.round(tIn * track.fps) : 0
  const i1 = track ? Math.min(track.frames - 1, Math.round(tOut * track.fps)) : 0
  const side = visible ? track.size[f] * Math.min(aw, ah) : 0

  return (
    <div className="viewer">
      <div className="stage-box">
        <div className={`beat-badge ${playing ? 'live' : ''}`}>
          <i className={`beat-dot ${strong ? 'strong' : ''}`} style={{ transform: `scale(${1 + pulse * (strong ? 0.9 : 0.45)})`, opacity: 0.45 + 0.55 * pulse }} />
          <span>{wave?.tempo ? `${Math.round(wave.tempo)} BPM` : '节拍'}</span>
          <button className="help" onClick={() => setHelp((h) => !h)} title="什么是卡点？">?</button>
        </div>
        {help && (
          <div className="beat-help" onPointerDown={(e) => e.stopPropagation()}>
            <div className="bh-head"><b>什么是卡点</b><button className="icon" onClick={() => setHelp(false)}><Icon name="close" size={14} /></button></div>
            <svg viewBox="0 0 240 70" className="bh-diagram">
              {Array.from({ length: 48 }, (_, i) => <rect key={i} x={i * 5} y={38 - (6 + ((i * 37) % 17))} width="3" height={2 * (6 + ((i * 37) % 17))} className="wv" />)}
              {Array.from({ length: 8 }, (_, i) => <rect key={`b${i}`} x={10 + i * 30} y="0" width={i % 4 === 0 ? 2.5 : 1.5} height={i % 4 === 0 ? 12 : 7} className={i % 4 === 0 ? 'down' : 'beat'} />)}
              <line className="cutline" x1="0" y1="2" x2="0" y2="68" />
            </svg>
            <p>音乐里有规律的鼓点叫<b>节拍</b>，每 4 拍里最重的那一下是<b>重拍</b>。</p>
            <p>Oddcut 会把镜头切换放在节拍上，重要的镜头（手绘变回实拍）放在重拍上。</p>
            <p>你拖切点时会自动吸到最近的节拍；时间轴上绿点 = 这个切点踩在拍上。</p>
          </div>
        )}

        <div className="frame" ref={boxRef} style={{ aspectRatio: `${aw} / ${ah}`, '--ar': aw / ah }}
             onContextMenu={(e) => e.preventDefault()}
             onDragOver={(e) => { if (e.dataTransfer.types.includes('application/x-oddcut-sticker')) e.preventDefault() }}
             onDrop={(e) => { const id = e.dataTransfer.getData('application/x-oddcut-sticker'); if (id) { e.preventDefault(); onDropSticker(id, ...norm(e)) } }}>
          {[0, 1].map((i) => layers[i] && (
            <video key={i} ref={vids[i]} src={layers[i]} className={i === front ? 'front' : 'back'} playsInline preload="auto"
                   onLoadedMetadata={onLoaded(i)} onSeeked={onSeekedLayer(i)}
                   onPlay={() => i === front && setPlaying(true)} onPause={() => i === front && setPlaying(false)}
                   onEnded={() => i === front && setPlaying(false)} />
          ))}
          {!layers[front] && <div className="frame-empty">{status || '正在准备预览…'}</div>}

          <div className={`surface ${boxMode ? 'box-mode' : autoSticker && !draft && inShot ? 'place-mode' : ''}`} onPointerDown={onSurface}>
            {showPath && (
              <svg viewBox="0 0 100 100" preserveAspectRatio="none" className={draft.mode === 'follow' ? 'tracked' : 'drawn-path'}>
                <polyline className="past" points={pts(i0, Math.max(i0, Math.min(f, i1)))} />
                <polyline className="future" points={pts(Math.max(i0, Math.min(f, i1)), i1)} />
              </svg>
            )}
            {rec && <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="recording"><polyline points={rec.map((p) => `${p[1] * 100},${p[2] * 100}`).join(' ')} /></svg>}
            {draft && inShot && draft.keys.map((k, j) => (
              <i key={j} className={`kdot ${keySel === j ? 'sel' : ''} ${Math.abs(k.t - local) < 0.5 / fps ? 'now' : ''}`}
                 style={{ left: `${k.x * 100}%`, top: `${k.y * 100}%` }} title={`第 ${j + 1} 个关键帧 · ${k.t.toFixed(2)} 秒（拖动调位置，右键更多）`}
                 onPointerDown={(e) => onKeyDown(e, j, k)}
                 onContextMenu={(e) => { e.preventDefault(); e.stopPropagation(); const r = rect(); onSelectKey(j); setMenu({ j, x: e.clientX - r.left, y: e.clientY - r.top }) }} />
            ))}
            {visible && !rec && (
              <div className={`sel-box ${recordArmed ? 'armed' : ''}`} onPointerDown={onStickerDown}
                   style={{ left: `${track.x[f] * 100}%`, top: `${track.y[f] * 100}%`, width: `${(side / aw) * 100}%`, height: `${(side / ah) * 100}%` }}>
                {showImage && stickerImage && <img src={stickerImage} alt="" draggable={false} />}
                {!recordArmed && ['nw', 'ne', 'sw', 'se'].map((c) => <i key={c} className={`corner ${c}`} onPointerDown={resize} />)}
              </div>
            )}
            {liveOv.map((o) => {
              const sel = o.id === ovSel
              if (!sel && shot) return null                  // while a shot is being edited, overlays stay out of the way
              return (
                <div key={o.id} className={`ov-box ${sel ? 'sel' : ''}`} onPointerDown={(e) => ovDown(e, o)}
                     title={sel ? '拖动移动，拖四角缩放' : `画中画 ${o.track + 1}：点一下选中`}
                     style={{ left: `${o.x * 100}%`, top: `${o.y * 100}%`, width: `${o.w * 100}%`, height: `${o.h * 100}%`,
                              borderRadius: o.radius * Math.min(o.w * frameW, o.h * frameH) }}>
                  {sel && ovShowImage && clipThumbs[o.clip] && <img src={clipThumbs[o.clip]} alt="" draggable={false} style={{ opacity: o.opacity }} />}
                  {sel && ['nw', 'ne', 'sw', 'se'].map((c) => <i key={c} className={`corner ${c}`} onPointerDown={(e) => ovResize(e, o, c)} />)}
                </div>
              )
            })}
            {boxDraft && (
              <i className="subject-box" style={{ left: `${boxDraft[0] * 100}%`, top: `${boxDraft[1] * 100}%`,
                                                 width: `${(boxDraft[2] - boxDraft[0]) * 100}%`, height: `${(boxDraft[3] - boxDraft[1]) * 100}%` }} />
            )}
            {menu && (
              <div className="kmenu" style={{ left: menu.x, top: menu.y }} onPointerDown={(e) => e.stopPropagation()}>
                <button onClick={() => { setMenu(null); onDeleteKey(menu.j) }}><Icon name="trash" size={16} />删除这个关键帧</button>
                <button onClick={() => { setMenu(null); onStillKey(menu.j) }}><Icon name="check" size={16} />设为停在这里</button>
              </div>
            )}
          </div>

          {boxMode && <span className="frame-tip">在画面上框出要画的东西</span>}
          {!boxMode && recordArmed && visible && <span className="frame-tip">按住贴纸拖动，视频会慢速播放并记录路线；松开结束</span>}
          {!boxMode && !recordArmed && autoSticker && !draft && inShot && <span className="frame-tip">自动加的贴纸：点画面上的位置，改成手动放在那里</span>}
          {!boxMode && !recordArmed && draft && inShot && hint && (
            <span className="frame-tip dismiss">拖贴纸移动、拖四角调大小，会在当前时刻自动加关键帧。白色菱形是关键帧，点它跳过去，拖它改位置，右键删除
              <button onClick={dismissHint} aria-label="知道了"><Icon name="close" size={14} /></button></span>
          )}
        </div>
      </div>
      <div className="transport">
        <button className="icon" onClick={() => step(-1)} title="上一帧（←）"><Icon name="prev" /></button>
        <button className="play" onClick={toggle} title={playing ? '暂停（空格）' : segment ? `播放片段 ${segment.n}（空格）` : '播放（空格）'} disabled={!src}>
          <Icon name={playing ? 'pause' : 'play'} size={24} fill />
        </button>
        <button className="icon" onClick={() => step(1)} title="下一帧（→）"><Icon name="next" /></button>
        {segment && (
          <button className={`toggle loop ${loop ? 'on' : ''}`} onClick={() => setLoop((l) => !l)} aria-pressed={loop}
                  title="循环播放选中的这一段"><Icon name="refresh" size={16} />循环</button>
        )}
        <span className={`tc ${segment ? 'seg' : ''}`}>
          {segment
            ? <>片段 {segment.n} · {Math.min(Math.max(0, time - segment.start), segment.end - segment.start).toFixed(1)} / {(segment.end - segment.start).toFixed(1)}s</>
            : <>{fmt(time)} / {fmt(duration)}</>}
        </span>
      </div>
    </div>
  )
}
