import React, { useState } from 'react'
import Icon from './icons.jsx'

const TABS = [['clips', '素材', 'film'], ['sticker', '贴纸', 'sticker'], ['drawn', '手绘', 'brush'], ['music', '音乐', 'music']]
const fileName = (p) => (p ? p.split(/[\\/]/).pop() : '')

// Left library: footage (drag onto a 画中画 track, or click to add at the playhead), stickers (drag onto the picture, or click to add to the shot under the playhead),
// drawing styles (click = draw the selected shot), music (switch the song).
export default function Library({ looks, activeSticker, activeStyle, onAddSticker, onApplyStyle, music, songs, onPickSong,
                                  onBrowseMusic, busy, rail, clips = [], clipThumbs = {}, onAddOverlay }) {
  const [tab, setTab] = useState('sticker')
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
          {tab === 'drawn' && (
            <>
              <p className="hint">点一个风格，把选中的镜头变成手绘：先画线稿、上色，再在重拍上变回实拍</p>
              <div className="style-grid">
                {looks.drawn_styles.map((d) => (
                  <button key={d.id} className={`style-card ${activeStyle === d.id ? 'on' : ''}`} disabled={busy} onClick={() => onApplyStyle(d.id)}>
                    <img src={d.preview} alt="" draggable={false} />
                    <b>{d.name}</b>
                    <small>{d.description}</small>
                  </button>
                ))}
              </div>
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
