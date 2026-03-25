'use client'
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { getToken, getStoreIds, getActiveStore, formatStoreName } from '@/lib/api'
import Sidebar from './Sidebar'

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [ready, setReady] = useState(false)
  const [storeIds, setStoreIds] = useState<string[]>([])
  const [activeStore, setActiveStore] = useState('')

  useEffect(() => {
    if (!getToken()) {
      router.replace('/login')
      return
    }
    const ids = getStoreIds()
    const active = getActiveStore()
    setStoreIds(ids)
    setActiveStore(active)
    setReady(true)
  }, [router])

  if (!ready) return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="text-gray-400 text-sm">Loading...</div>
    </div>
  )

  return (
    <div className="flex min-h-screen">
      <Sidebar
        storeName={activeStore ? formatStoreName(activeStore) : undefined}
        storeIds={storeIds}
        activeStore={activeStore}
      />
      <main className="flex-1 overflow-auto">{children}</main>
    </div>
  )
}
