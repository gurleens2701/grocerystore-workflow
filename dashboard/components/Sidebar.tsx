'use client'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { clearToken } from '@/lib/api'

const nav = [
  { href: '/dashboard', label: 'Daily Sales', icon: '📊' },
  { href: '/prices', label: 'Price Database', icon: '🔍' },
  { href: '/order', label: 'Order Builder', icon: '📦' },
  { href: '/health', label: 'Health Score', icon: '💪' },
]

export default function Sidebar({ storeName }: { storeName?: string }) {
  const pathname = usePathname()
  const router = useRouter()

  function logout() {
    clearToken()
    router.push('/login')
  }

  return (
    <aside className="w-56 bg-gray-900 min-h-screen flex flex-col">
      <div className="px-6 py-5 border-b border-gray-700">
        <div className="text-xl">⛽</div>
        <div className="text-white font-semibold text-sm mt-1">{storeName || 'Gas Station'}</div>
        <div className="text-gray-400 text-xs">Dashboard</div>
      </div>

      <nav className="flex-1 px-3 py-4 space-y-1">
        {nav.map(item => (
          <Link
            key={item.href}
            href={item.href}
            className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition ${
              pathname === item.href
                ? 'bg-blue-600 text-white'
                : 'text-gray-300 hover:bg-gray-800 hover:text-white'
            }`}
          >
            <span>{item.icon}</span>
            {item.label}
          </Link>
        ))}
      </nav>

      <div className="px-3 py-4 border-t border-gray-700 space-y-1">
        <a
          href="#"
          onClick={async e => {
            e.preventDefault()
            const res = await fetch('/api/settings', {
              headers: { Authorization: `Bearer ${document.cookie.match(/token=([^;]+)/)?.[1] || ''}` }
            }).then(r => r.json())
            if (res?.google_sheet_url) window.open(res.google_sheet_url, '_blank')
          }}
          className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-gray-300 hover:bg-gray-800 hover:text-white transition"
        >
          <span>📄</span> Google Sheet
        </a>
        <button
          onClick={logout}
          className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-gray-300 hover:bg-gray-800 hover:text-white transition"
        >
          <span>🚪</span> Sign Out
        </button>
      </div>
    </aside>
  )
}
