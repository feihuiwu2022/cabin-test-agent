export type BotState = 'idle' | 'talking' | 'listening' | 'thinking'

export function Bot({ state, className = '' }: { state: BotState; className?: string }) {
  return (
    <svg className={`bot ${state} ${className}`} viewBox="0 0 200 200" aria-label="机器人">
      <circle className="halo" cx="100" cy="104" r="92" />
      <line className="ant" x1="100" y1="40" x2="100" y2="18" />
      <circle className="bulb" cx="100" cy="14" r="7" />
      <rect className="head" x="28" y="40" width="144" height="124" rx="50" />
      <rect className="visor" x="46" y="66" width="108" height="62" rx="31" />
      <rect className="eye" x="72" y="84" width="14" height="24" rx="7" />
      <rect className="eye" x="114" y="84" width="14" height="24" rx="7" />
      <rect className="mouth" x="88" y="140" width="24" height="6" rx="3" />
    </svg>
  )
}

export function Wave({ on, mic, n = 24, style }: { on: boolean; mic?: boolean; n?: number; style?: React.CSSProperties }) {
  return (
    <div className={`wave ${on ? 'on' : ''} ${mic ? 'mic' : ''}`} style={style}>
      {Array.from({ length: n }, (_, i) => <i key={i} style={{ animationDelay: `${(i * 37) % 300}ms` }} />)}
    </div>
  )
}
