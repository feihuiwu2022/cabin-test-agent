import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'

const render = () => createRoot(document.getElementById('root')!).render(<App />)
// 静态演示（GitHub Pages）：不连服务器，回放一次真实运行的录像
if (import.meta.env.VITE_DEMO) import('./demo').then(m => m.install()).then(render)
else render()
