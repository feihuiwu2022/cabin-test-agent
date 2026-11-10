import { lazy, StrictMode, Suspense } from 'react'
import { createRoot } from 'react-dom/client'

// 页面样式各自独立，按路径只加载其中一个
const path = location.pathname
const Page = lazy(() => (path.startsWith('/board') ? import('./Board') : path.startsWith('/settings') ? import('./Settings') : path.startsWith('/touch') ? import('./Touch') : path.startsWith('/skills') ? import('./Skills') : import('./Cases')))

createRoot(document.getElementById('root')!).render(<StrictMode><Suspense fallback={null}><Page /></Suspense></StrictMode>)
