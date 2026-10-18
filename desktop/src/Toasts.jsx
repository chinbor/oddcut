import React, { useCallback, useState } from 'react'
import Icon from './icons.jsx'

// Short messages in the corner: at most 4, gone after 5 s (9 s for an error or one with a button).
export function useToasts() {
  const [toasts, setToasts] = useState([])
  const toast = useCallback((text, kind = 'info', action = null) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((t) => [...t.slice(-3), { id, text, kind, action }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'error' || action ? 9000 : 5000)
  }, [])
  return { toasts, setToasts, toast }
}

export default function Toasts({ toasts, setToasts }) {
  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <Icon name={t.kind === 'error' ? 'alert' : 'check'} size={18} /><span>{t.text}</span>
          {t.action && <button onClick={() => { t.action.run(); setToasts((x) => x.filter((y) => y.id !== t.id)) }}>{t.action.label}</button>}
          <button className="icon" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))} aria-label="关闭"><Icon name="close" size={14} /></button>
        </div>
      ))}
    </div>
  )
}
