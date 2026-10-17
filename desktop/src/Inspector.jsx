import React, { useEffect, useRef, useState } from 'react'

// Shot inspector: what the selected shot shows, its sticker (placed by clicking on the frame) and whether
// it's drawn. Edits are drafts until 应用; the parent turns them into a "shot" job.

const ROLE_NAMES = { hook: '开场', drawn: '手绘', body: '快切', breath: '呼吸镜头', sticker: '贴纸', outro: '结尾' }

export default function Inspector({ plan, index, looks, usableClips, time, busy, onSwap, onApply, onCheck, getStill,
                                    stickerName, onClose }) {
  const s = plan.shots[index]
  const local = Math.min(Math.max(0, time - s.t_start), s.t_end - s.t_start - 0.02)
  const [still, setStill] = useState(null)
  const [draft, setDraft] = useState(null)          // sticker being placed
  const [drawnOn, setDrawnOn] = useState(!!s.drawn)
  const [style, setStyle] = useState(s.drawn?.style || 'lineart')
  const [check, setCheck] = useState(null)          // {ok, found, reason, box, image} or {pending}
  const [box, setBox] = useState(s.drawn?.box || null)
  const [boxMode, setBoxMode] = useState(false)
  const frameRef = useRef(null)
  const [aw, ah] = plan.aspect.split(':').map(Number)

  // a fresh shot: reset the drafts to what the plan has
  useEffect(() => {
    setDraft(s.sticker_at ? { id: s.sticker.split('@')[0], ...s.sticker_at } : null)
    setDrawnOn(!!s.drawn)
    setStyle(s.drawn?.style || 'lineart')
    setBox(s.drawn?.box || null)
    setCheck(null)
    setBoxMode(false)
  }, [plan.plan, index])

  // the frame under the playhead (debounced while scrubbing)
  useEffect(() => {
    const t = Math.round(local * 10) / 10
    const id = setTimeout(() => getStill(index, t).then(setStill).catch(() => {}), 120)
    return () => clearTimeout(id)
  }, [plan.plan, index, Math.round(local * 10)])

  const point = (e) => {
    const r = frameRef.current.getBoundingClientRect()
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]
  }

  // click / drag on the frame: place the sticker, or draw the subject box
  const onFrameDown = (e) => {
    if (busy) return
    e.preventDefault()
    const [x0, y0] = point(e)
    if (boxMode) {
      let b = [x0, y0, x0, y0]
      setBox(b)
      const move = (ev) => { const [x1, y1] = point(ev); b = [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)]; setBox(b) }
      const up = () => {
        window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up)
        setBoxMode(false)
        if (b[2] - b[0] > 0.03 && b[3] - b[1] > 0.03) runCheck(b)
      }
      window.addEventListener('pointermove', move); window.addEventListener('pointerup', up)
      return
    }
    if (!draft) return
    const place = (x, y) => setDraft((d) => {
      const size = d.keys[0]?.size ?? 0.22
      return { ...d, keys: [{ t: Math.round(local * 100) / 100, x, y, size }] }
    })
    place(x0, y0)
    const move = (ev) => place(...point(ev))
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up) }
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up)
  }

  const pickSticker = (v) => {
    if (v === '') return
    if (v === 'none') { setDraft(null); return onApply(index, { sticker: null }) }
    setDraft((d) => ({ id: v, mode: d?.mode || 'follow', in: d?.in || 0,
                       keys: d?.keys?.length ? d.keys : [{ t: Math.round(local * 100) / 100, x: 0.5, y: 0.45, size: 0.22 }] }))
  }
  const setSize = (v) => setDraft((d) => ({ ...d, keys: d.keys.map((k) => ({ ...k, size: v })) }))

  const runCheck = async (b) => {
    setCheck({ pending: true })
    try { setCheck(await onCheck(index, b ?? box)) } catch (e) { setCheck({ ok: false, reason: String(e.message || e) }) }
  }
  const toggleDrawn = (on) => {
    setDrawnOn(on)
    if (on && !s.drawn) runCheck(null)
    if (!on && s.drawn) onApply(index, { drawn: null })
  }
  const applyDrawn = () => onApply(index, { drawn: { style, box, force: check ? !check.ok : false, label: check?.label } })

  const k = draft?.keys?.[0]
  const minSide = Math.min(aw, ah)
  const stickerPreview = draft && looks.stickers.find((x) => x.id === draft.id)?.preview

  return (
    <div className="inspector">
      <div className="insp-head">
        <b>镜头 {index + 1}</b>
        <span>{ROLE_NAMES[s.role] || s.role} · {s.t_start.toFixed(2)}–{s.t_end.toFixed(2)}s · {s.clip}</span>
        <button className="link" onClick={onClose}>关闭</button>
      </div>

      <div className="insp-frame" ref={frameRef} style={{ aspectRatio: `${aw} / ${ah}` }} onPointerDown={onFrameDown}
           data-mode={boxMode ? 'box' : draft ? 'place' : ''}>
        {still?.image && <img src={check?.image && drawnOn && !draft ? check.image : still.image} alt="" draggable={false} />}
        {draft && k && stickerPreview && (
          <img className="sticker-ghost" src={stickerPreview} alt="" draggable={false}
               style={{ left: `${k.x * 100}%`, top: `${k.y * 100}%`, width: `${(k.size * minSide / aw) * 100}%` }} />
        )}
        {box && drawnOn && (
          <i className="subject-box" style={{ left: `${box[0] * 100}%`, top: `${box[1] * 100}%`,
                                             width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` }} />
        )}
        <span className="frame-time">{local.toFixed(2)}s</span>
      </div>
      <p className="hint">{boxMode ? '在画面上拖一个框，圈出要画的东西'
        : draft ? '在画面上点一下（可以拖）放贴纸；拖下面的时间线换到别的时刻再放' : '拖时间线可以看这个镜头的其他时刻'}</p>

      <section>
        <h4>素材</h4>
        <div className="swap">
          <button className="mini" disabled={busy} onClick={() => onSwap(index)}>换一个</button>
          <select disabled={busy} value="" onChange={(e) => e.target.value && onSwap(index, e.target.value)}>
            <option value="">指定素材…</option>
            {usableClips.map((c) => <option key={c.path} value={c.path}>{c.name}</option>)}
          </select>
        </div>
      </section>

      <section>
        <h4>贴纸 <small>{s.sticker ? `现在：${stickerName(s.sticker)}${s.sticker_at ? '（手动）' : '（自动）'}` : '现在：无'}</small></h4>
        <select disabled={busy} value={draft?.id || ''} onChange={(e) => pickSticker(e.target.value)}>
          <option value="">{draft ? '' : '选一个贴纸放到这个镜头…'}</option>
          {s.sticker && <option value="none">去掉贴纸</option>}
          {looks.sticker_groups.map((g) => (
            <optgroup key={g.id} label={g.name}>
              {looks.stickers.filter((x) => x.group === g.id).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </optgroup>
          ))}
        </select>
        {draft && (
          <>
            <div className="seg">
              <button className={draft.mode === 'follow' ? 'on' : ''} onClick={() => setDraft({ ...draft, mode: 'follow' })}
                      title="贴纸跟着你点的那个位置的画面一起动">跟着走</button>
              <button className={draft.mode === 'fixed' ? 'on' : ''} onClick={() => setDraft({ ...draft, mode: 'fixed' })}
                      title="贴纸停在屏幕上这个位置不动">固定</button>
            </div>
            <label className="row"><span>大小</span>
              <input type="range" min="0.06" max="0.6" step="0.01" value={k?.size ?? 0.22} onChange={(e) => setSize(Number(e.target.value))} />
            </label>
            <div className="actions">
              <button className="primary" disabled={busy} onClick={() => onApply(index, { sticker: draft })}>应用贴纸</button>
              <button disabled={busy} onClick={() => setDraft(s.sticker_at ? { id: s.sticker.split('@')[0], ...s.sticker_at } : null)}>取消</button>
            </div>
          </>
        )}
      </section>

      <section>
        <h4>手绘</h4>
        <label className="row"><input type="checkbox" checked={drawnOn} disabled={busy} onChange={(e) => toggleDrawn(e.target.checked)} />
          这个镜头用手绘（画线 → 上色 → 变回实拍）</label>
        {drawnOn && (
          <>
            <select value={style} disabled={busy} onChange={(e) => setStyle(e.target.value)}>
              {looks.drawn_styles.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
            {check?.pending && <p className="hint">正在检查这个镜头能不能画…</p>}
            {check && !check.pending && (
              <p className={`check ${check.ok ? 'ok' : 'warn'}`}>{check.ok ? '✓ ' : '⚠ '}{check.reason}
                {check.found && check.label && check.label !== 'thing' ? `（${check.label}）` : ''}</p>
            )}
            <div className="actions">
              <button disabled={busy} onClick={() => setBoxMode(true)}>框选要画的东西</button>
              <button className="primary" disabled={busy || check?.pending || (check && !check.found)} onClick={applyDrawn}>
                {check && check.found && !check.ok ? '仍然画这个镜头' : s.drawn ? '更新手绘' : '应用手绘'}
              </button>
            </div>
            <p className="hint">镜头不够长时会自动拉长到下一个重拍，后面的镜头相应缩短，总时长不变。</p>
          </>
        )}
      </section>
    </div>
  )
}
