import React, { useState } from 'react'
import Icon from './icons.jsx'

const MODES = [
  ['follow', '跟着物体', '贴纸粘在它下面的东西上，跟着一起动。在别的时刻拖它，就是纠正位置。'],
  ['path', '按我画的路线', '贴纸沿着你放的关键帧走一条平滑的路线，不看画面内容。'],
  ['still', '停在原地', '贴纸一直停在一个位置，不动。'],
]

// Right panel, only while a shot is selected. Every change applies right away (one undo step each).
export default function Properties({ shot, index, thumb, looks, usableClips, draft, check, busy, onClose, onSwap,
                                     onRemoveSticker, onMode, onSize, onSizeCommit, onDrawn, onStyle, onForceDrawn,
                                     onBoxMode, stickerName, keyAt, onPrevKey, onToggleKey, onNextKey, recordArmed,
                                     onRecordArm, onMoveShot, shotCount, preset, onApplyPreset, onRemovePreset, onSelectLayer,
                                     drawPick, onDrawPick, onDrawAuto, drawnLayers = [], onSelectLayer2, onDeleteLayer }) {
  const [pickClip, setPickClip] = useState(false)
  const [more, setMore] = useState(false)
  const len = shot.t_end - shot.t_start
  const sid = shot.sticker ? shot.sticker.split('@')[0] : null
  const manual = !!shot.sticker_at
  const keys = draft?.keys || []
  const local = draft?.cur
  const curSize = draft?.curSize ?? 0.22
  const mode = draft?.mode === 'fixed' ? 'path' : draft?.mode || 'follow'


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
        <div className="row-btns order">
          <button disabled={busy || index === 0} onClick={() => onMoveShot(-1)} title="和前一个镜头换位置（切点会重新对齐节拍）"><Icon name="prev" size={16} />左移</button>
          <span className="order-pos">第 {index + 1} / {shotCount} 个</span>
          <button disabled={busy || index === shotCount - 1} onClick={() => onMoveShot(1)} title="和后一个镜头换位置（切点会重新对齐节拍）">右移<Icon name="next" size={16} /></button>
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
                <label className="slider-row">
                  <span>大小</span>
                  <input type="range" min="0.06" max="0.6" step="0.01" value={curSize} disabled={busy}
                         onChange={(e) => onSize(Number(e.target.value))} onPointerUp={onSizeCommit} onKeyUp={onSizeCommit} />
                  <b>{Math.round(curSize * 100)}%</b>
                </label>
                <button className={`more-toggle inline ${more ? 'open' : ''}`} onClick={() => setMore((m) => !m)}>更多<Icon name="chevronDown" size={16} /></button>
                {more && (
                  <div className="more-body">
                    <div className="field">
                      <span className="field-label">怎么动</span>
                      <div className="segmented three">
                        {MODES.map(([id, label]) => (
                          <button key={id} className={mode === id ? 'on' : ''} disabled={busy} onClick={() => onMode(id)}>{label}</button>
                        ))}
                      </div>
                      <p className="caption">{MODES.find(([id]) => id === mode)[2]}</p>
                    </div>
                    {mode !== 'still' && (
                      <div className="field">
                        <span className="field-label">关键帧</span>
                        <div className="keybar">
                          <button className="icon" disabled={busy || keyAt.prev == null} onClick={onPrevKey} title="上一个关键帧"><Icon name="prev" size={18} /></button>
                          <button className={`kbtn ${keyAt.here != null ? 'on' : ''}`} disabled={busy} onClick={onToggleKey}
                                  title={keyAt.here != null ? '删除播放头这里的关键帧' : '在播放头这里加一个关键帧'}><i /></button>
                          <button className="icon" disabled={busy || keyAt.next == null} onClick={onNextKey} title="下一个关键帧"><Icon name="next" size={18} /></button>
                          <span className="keybar-text">{keyAt.here != null ? `第 ${keyAt.here + 1} 个，共 ${keys.length} 个` : `共 ${keys.length} 个关键帧`}</span>
                        </div>
                        <button className={`record ${recordArmed ? 'on' : ''}`} disabled={busy} onClick={onRecordArm}
                                title="按下后在画面上按住贴纸拖动：视频慢速播放，你拖的路线会变成关键帧">
                          <i className="rec-dot" />{recordArmed ? '在画面上按住贴纸拖动…（再点取消）' : '边播边拖，录一条路线'}
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </>
            ) : (
              <p className="caption">自动加的贴纸会跟着画面里的东西走。点预览画面上的位置，可以改成手动放在那里。</p>
            )}
          </>
        ) : (
          <p className="caption">从左边贴纸库拖一个到画面上</p>
        )}
      </section>

      {(preset || drawnLayers.length > 0) && (
        <section>
          <h3>这个镜头的效果</h3>
          {preset && (
            <div className="chip-row">
              <button className="chip fx-chip" onClick={() => onSelectLayer(preset.id)}>万物生长 · {preset.layers} 层<small>{preset.source === 'auto' ? '自动加的' : '你加的'}</small></button>
              <button className="icon" disabled={busy} onClick={() => onRemovePreset(preset.id)} title="去掉万物生长"><Icon name="trash" size={18} /></button>
            </div>
          )}
          {drawnLayers.map((o) => (
            <div key={o.id} className="chip-row">
              <button className="chip drawn-chip" onClick={() => onSelectLayer2(o.id)}>手绘 · {o.group?.label || '手绘'}</button>
              <button className="icon" disabled={busy} onClick={() => onDeleteLayer(o.id)} title="删掉这个手绘"><Icon name="trash" size={18} /></button>
            </div>
          ))}
        </section>
      )}
    </aside>
  )
}
