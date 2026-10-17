import React, { useState } from 'react'
import Icon from './icons.jsx'

// Right panel, only while a shot is selected. Every change applies right away (one undo step each).
export default function Properties({ shot, index, thumb, looks, usableClips, draft, check, busy, onClose, onSwap,
                                     onRemoveSticker, onMode, onSize, onSizeCommit, onDrawn, onStyle, onForceDrawn,
                                     onBoxMode, stickerName }) {
  const [pickClip, setPickClip] = useState(false)
  const len = shot.t_end - shot.t_start
  const sid = shot.sticker ? shot.sticker.split('@')[0] : null
  const manual = !!shot.sticker_at
  const keys = draft?.keys || []
  const local = draft?.cur
  const curSize = draft?.curSize ?? 0.22
  const drawnOn = !!shot.drawn
  const style = shot.drawn?.style || 'lineart'

  return (
    <aside className="props">
      <header className="props-head">
        {thumb ? <img src={thumb} alt="" /> : <i className="thumb-ph" />}
        <span><b>镜头 {index + 1}</b><small>{shot.t_start.toFixed(1)} – {shot.t_end.toFixed(1)} 秒 · 共 {len.toFixed(1)} 秒</small></span>
        <button className="icon" onClick={onClose} title="关闭（Esc）"><Icon name="close" /></button>
      </header>

      <section>
        <h3>画面</h3>
        <div className="row-btns">
          <button disabled={busy} onClick={() => onSwap()}><Icon name="refresh" size={16} />换一个素材</button>
          <button disabled={busy} onClick={() => setPickClip((v) => !v)}>从素材里挑…</button>
        </div>
        {pickClip && (
          <div className="clip-pick">
            {usableClips.map((c) => (
              <button key={c.path} disabled={busy} onClick={() => { setPickClip(false); onSwap(c.path) }}>{c.name}</button>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>贴纸</h3>
        {sid ? (
          <>
            <div className="chip-row">
              <span className="chip sticker">
                <img src={looks.stickers.find((s) => s.id === sid)?.preview} alt="" />
                {stickerName(shot.sticker)}<small>{manual ? '手动放的' : '自动加的'}</small>
              </span>
              <button className="icon" disabled={busy} onClick={onRemoveSticker} title="删掉这个贴纸"><Icon name="trash" size={18} /></button>
            </div>
            {manual ? (
              <>
                <div className="segmented">
                  <button className={draft?.mode !== 'fixed' ? 'on' : ''} disabled={busy} onClick={() => onMode('follow')}
                          title="贴纸跟着画面里那个位置一起动">跟随画面</button>
                  <button className={draft?.mode === 'fixed' ? 'on' : ''} disabled={busy} onClick={() => onMode('fixed')}
                          title="贴纸停在屏幕上这个位置">固定位置</button>
                </div>
                <label className="slider-row">
                  <span>大小</span>
                  <input type="range" min="0.06" max="0.6" step="0.01" value={curSize} disabled={busy}
                         onChange={(e) => onSize(Number(e.target.value))} onPointerUp={onSizeCommit} onKeyUp={onSizeCommit} />
                  <b>{Math.round(curSize * 100)}%</b>
                </label>
                <p className="caption">{keys.length} 个关键帧（时间轴上的菱形）{local != null ? '' : ''}</p>
              </>
            ) : (
              <p className="caption">自动加的贴纸会跟着画面里的东西走。点预览画面上的位置，可以改成手动放在那里。</p>
            )}
          </>
        ) : (
          <p className="empty">还没有贴纸。从左边贴纸库拖一个到画面上，或点一下加到这个镜头。</p>
        )}
      </section>

      <section>
        <h3>手绘</h3>
        <label className="switch-row">
          <span><b>把这个镜头变成手绘</b></span>
          <input type="checkbox" className="switch" checked={drawnOn || !!check?.pending} disabled={busy || check?.pending}
                 onChange={(e) => onDrawn(e.target.checked)} />
        </label>
        {(drawnOn || check) && (
          <>
            <div className="style-chips">
              {looks.drawn_styles.map((d) => (
                <button key={d.id} className={style === d.id ? 'on' : ''} disabled={busy || !drawnOn} onClick={() => onStyle(d.id)}>{d.name}</button>
              ))}
            </div>
            {check?.pending && <p className="caption">正在看这个镜头能不能画…</p>}
            {check && !check.pending && !check.ok && (
              <div className="warn-box">
                <Icon name="alert" size={18} />
                <span>{check.reason}</span>
                {check.found && <button disabled={busy} onClick={onForceDrawn}>仍然画这个镜头</button>}
              </div>
            )}
            <p className="caption">画的是画面里的主体。不对的话，<button className="link inline" disabled={busy} onClick={onBoxMode}>在预览上框一下</button>要画的东西。</p>
          </>
        )}
        {!drawnOn && !check && <p className="caption">打开后先画线稿再上色，在重拍上变回实拍。镜头太短会自动拉长到下一个重拍。</p>}
      </section>
    </aside>
  )
}
