import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'

// 页面样式各自独立，按路径只加载其中一个
const path = import.meta.env.VITE_DEMO ? `/${import.meta.env.VITE_DEMO_PAGE}` : location.pathname
const Page = lazy(() => (path.startsWith('/board') ? import('./Board') : path.startsWith('/settings') ? import('./Settings') : path.startsWith('/touch') ? import('./Touch') : path.startsWith('/skills') ? import('./Skills') : import('./Cases')))

const render = () => createRoot(document.getElementById('root')!).render(<StrictMode><Suspense fallback={null}><Page /></Suspense></StrictMode>)
// 静态演示（GitHub Pages）：不连服务器，回放一次真实运行的录像
if (import.meta.env.VITE_DEMO) import('./demo').then(m => m.install()).then(render)
else render()
