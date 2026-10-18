import React from 'react'

// Inline stroke icons (24px grid, currentColor), so they follow the text colour and stay crisp.
const P = {
  back: 'M15 18l-6-6 6-6',
  undo: 'M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3',
  redo: 'M15 14l5-5-5-5M20 9H10a6 6 0 000 12h3',
  chevronDown: 'M6 9l6 6 6-6',
  play: 'M8 5v14l11-7z',
  pause: 'M7 5h3v14H7zM14 5h3v14h-3z',
  prev: 'M18 6l-8 6 8 6M7 6v12',
  next: 'M6 6l8 6-8 6M17 6v12',
  close: 'M6 6l12 12M18 6L6 18',
  folder: 'M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z',
  music: 'M9 18V5l11-2v13M9 18a3 3 0 11-6 0 3 3 0 016 0zm11-2a3 3 0 11-6 0 3 3 0 016 0z',
  film: 'M4 4h16v16H4zM8 4v16M16 4v16M4 8h4M4 12h4M4 16h4M16 8h4M16 12h4M16 16h4',
  sticker: 'M4 4h11l5 5v11H4zM15 4v5h5M9 14c1 1 4 1 5 0M9.5 10.5h.01M14.5 10.5h.01',
  brush: 'M14 4l6 6-9 9H5v-6zM12 6l6 6',
  magnet: 'M6 4v7a6 6 0 0012 0V4M6 8h3M15 8h3',
  trash: 'M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  swap: 'M7 7h11l-3-3M17 17H6l3 3',
  plus: 'M12 5v14M5 12h14',
  check: 'M5 12l5 5 9-10',
  alert: 'M12 8v5M12 16h.01M10.3 3.9L2.5 17.5A2 2 0 004.2 20.5h15.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z',
  export: 'M12 3v12M7 10l5 5 5-5M5 21h14',
  box: 'M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4',
  sparkle: 'M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z',
  refresh: 'M4 4v6h6M20 20v-6h-6M5 15a7 7 0 0012 3l3-3M19 9a7 7 0 00-12-3L4 9',
  scissors: 'M6 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12',
  cutout: 'M3 7V3h4M17 3h4v4M21 17v4h-4M7 21H3v-4M12 11a3 3 0 100-6 3 3 0 000 6zM7 19a5 5 0 0110 0',
  layers: 'M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5',
  diamond: 'M12 3l9 9-9 9-9-9z',
  brush2: 'M18.4 2.6a2 2 0 012.9 2.9L11 15.8 8 16l.2-3zM7 17c-2 0-3 1.5-3 3.5V21h3.5c1.5 0 3-1 3-3',
}

export default function Icon({ name, size = 20, fill = false, style }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={fill ? 'currentColor' : 'none'} stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style}>
      <path d={P[name]} />
    </svg>
  )
}
