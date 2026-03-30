'use client'
import Link from 'next/link'
import { usePathname, useRouter } from 'next/navigation'
import { clearToken, clearStoreData, setActiveStore, formatStoreName, getActiveStore } from '@/lib/api'

const nav = [
  { href: '/dashboard', label: 'Daily Sales', icon: '📊' },
  { href: '/daily', label: 'Log Daily Report', icon: '📋' },
  { href: '/ledger', label: 'Ledger', icon: '🗂️' },
  { href: '/chat', label: 'Store Chat', icon: '💬' },
  { href: '/prices', label: 'Price Database', icon: '🔍' },
  { href: '/order', label: 'Order Builder', icon: '📦' },
  { href: '/health', label: 'Health Score', icon: '💪' },
  { href: '/bank', label: 'Bank Account', icon: '🏦' },
]

interface SidebarProps {
  storeName?: string
  storeIds?: string[]
  activeStore?: string
}

export default function Sidebar({ storeName, storeIds = [], activeStore = '' }: SidebarProps) {
  const pathname = usePathname()
  const router = useRouter()

  function logout() {
    clearToken()
    clearStoreData()
    router.push('/login')
  }

  function switchStore(id: string) {
    setActiveStore(id)
    // Reload so all data re-fetches for the new store
    window.location.reload()
  }

  const displayName = storeName || (activeStore ? formatStoreName(activeStore) : 'Gas Station')

  return (
    <aside className="w-56 bg-gray-900 min-h-screen flex flex-col">
      <div className="px-6 py-5 border-b border-gray-700">
        <div className="text-xl">⛽</div>
        {storeIds.length > 1 ? (
          <select
            value={activeStore}
            onChange={e => switchStore(e.target.value)}
            className="mt-1 w-full bg-gray-800 text-white text-sm rounded px-2 py-1 border border-gray-600 focus:outline-none focus:border-blue-500"
          >
            {storeIds.map(id => (
              <option key={id} value={id}>{formatStoreName(id)}</option>
            ))}
          </select>
        ) : (
          <>
            <div className="text-white font-semibold text-sm mt-1">{displayName}</div>
            <div className="text-gray-400 text-xs">Dashboard</div>
          </>
        )}
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
            const token = document.cookie.match(/token=([^;]+)/)?.[1] || ''
            const sid = getActiveStore()
            const url = `/api/settings${sid ? `?store_id=${sid}` : ''}`
            const res = await fetch(url, {
              headers: { Authorization: `Bearer ${token}` }
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
