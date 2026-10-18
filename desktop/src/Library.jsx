import React, { useEffect, useState } from 'react'
import Icon from './icons.jsx'
import { GRID_NAMES, LAYOUTS } from './pip.js'

const TABS = [['clips', '素材', 'film'], ['sticker', '贴纸', 'sticker'], ['music', '音乐', 'music']]
const fileName = (p) => (p ? p.split(/[\\/]/).pop() : '')

// Left library: footage (drag onto a 画中画 track, or click to add at the playhead), stickers (drag onto the picture, or click to add to the shot under the playhead),
// drawing styles (click = draw the selected shot), music (switch the song).
export default function Library({ tabRequest, looks, activeSticker, activeStyle, onAddSticker, onApplyStyle, music, songs, onPickSong,
                                  onBrowseMusic, busy, rail, clips = [], clipThumbs = {}, onAddOverlay, growTarget, onGrow,
                                  onGrowAll, growAllOn, onAddGrid, onAutoGrow, grownShots = [], onSeekShot }) {
  const [tab, setTab] = useState('sticker')
  useEffect(() => { if (tabRequest) { setTab(tabRequest.tab); if (rail) setOpen(true) } }, [tabRequest])
  const [open, setOpen] = useState(false)          // icon-rail mode: the panel flies out
  const show = !rail || open
  const pick = (t) => { setTab(t); if (rail) setOpen(open && tab === t ? false : true) }

  return (
    <aside className={`library ${rail ? 'rail' : ''} ${rail && open ? 'open' : ''}`}>
      <div className="lib-tabs" role="tablist">
        {TABS.map(([id, label, icon]) => (
          <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'on' : ''} onClick={() => pick(id)} title={label}>
            <Icon name={icon} size={18} /><span>{label}</span>
          </button>
        ))}
      </div>
      {show && (
        <div className="lib-body">
          {tab === 'clips' && (
            <>
              <div className="lib-group">
                <h3>宫格 / 分屏</h3>
                <div className="grid-tiles">
                  {Object.keys(LAYOUTS).map((id) => (
                    <button key={id} className="grid-tile" disabled={busy} draggable
                            onDragStart={(e) => { e.dataTransfer.setData('application/x-oddcut-grid', id); e.dataTransfer.effectAllowed = 'copy' }}
                            onClick={() => onAddGrid(id)} title={`${GRID_NAMES[id]}：点一下加到播放头这里（用后面几段素材填满），或拖到画中画轨道`}>
                      <i className="gt-ico" style={{ gridTemplateColumns: `repeat(${LAYOUTS[id][0]}, 1fr)`, gridTemplateRows: `repeat(${LAYOUTS[id][1]}, 1fr)` }}>
                        {Array.from({ length: LAYOUTS[id][0] * LAYOUTS[id][1] }, (_, k) => <b key={k} />)}
                      </i>
                      <span>{GRID_NAMES[id]}</span>
                    </button>
                  ))}
                </div>
              </div>
              <h3 className="lib-sub">视频素材</h3>
              <p className="hint">拖到时间轴的「画中画」轨道上，叠在画面上面播放；或点一下加到播放头这里</p>
              <div className="clip-tiles">
                {clips.map((c) => (
                  <button key={c.path} className="clip-tile" disabled={busy} draggable
                          onDragStart={(e) => { e.dataTransfer.setData('application/x-oddcut-clip', c.path); e.dataTransfer.effectAllowed = 'copy' }}
                          onClick={() => onAddOverlay(c.path)} title={`${c.name} · ${c.duration.toFixed(1)} 秒：拖到画中画轨道，或点一下添加`}>
                    <span className="ct-pic">{clipThumbs[c.path] ? <img src={clipThumbs[c.path]} alt="" draggable={false} /> : <i className="spin" />}
                      <small>{c.duration.toFixed(1)}s</small></span>
                    <span className="ct-name">{c.name}</span>
                  </button>
                ))}
              </div>
            </>
          )}
          {tab === 'sticker' && (
            <>
              <p className="hint">拖到画面上任意位置，或点一下加到播放头所在的镜头</p>
              {looks.sticker_groups.map((g) => {
                const items = looks.stickers.filter((s) => s.group === g.id)
                if (!items.length) return null
                return (
                  <div key={g.id} className="lib-group">
                    <h3>{g.id === 'persona' ? '表情' : g.name}</h3>
                    <div className="tiles">
                      {items.map((s) => (
                        <button key={s.id} className={`tile ${activeSticker === s.id ? 'on' : ''}`} disabled={busy}
                                draggable onDragStart={(e) => { e.dataTransfer.setData('application/x-oddcut-sticker', s.id); e.dataTransfer.effectAllowed = 'copy' }}
                                onClick={() => onAddSticker(s.id)} title={`${s.name}：拖到画面上，或点一下添加`}>
                          <img src={s.preview} alt="" draggable={false} />
                          <span>{s.name}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )
              })}
            </>
          )}
          {tab === 'music' && (
            <>
              <div className="song-card current">
                <Icon name="music" size={22} />
                <span><b>{music?.name || fileName(music?.path)}</b><small>{music?.tempo ? `${Math.round(music.tempo)} BPM · 正在用` : '正在用'}</small></span>
              </div>
              {songs.filter((s) => fileName(s) !== (music?.name || fileName(music?.path))).map((s) => (
                <button key={s} className="song-card" disabled={busy} onClick={() => onPickSong(s)}>
                  <Icon name="music" size={22} /><span><b>{fileName(s)}</b><small>换成这首</small></span>
                </button>
              ))}
              <button className="ghost wide" disabled={busy} onClick={onBrowseMusic}><Icon name="plus" size={18} />从电脑里选音乐…</button>
            </>
          )}
        </div>
      )}
    </aside>
  )
}
