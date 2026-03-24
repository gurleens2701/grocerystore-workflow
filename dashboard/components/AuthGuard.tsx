'use client'
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { getToken, api } from '@/lib/api'
import Sidebar from './Sidebar'

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const [ready, setReady] = useState(false)
  const [storeName, setStoreName] = useState('')

  useEffect(() => {
    if (!getToken()) {
      router.replace('/login')
      return
    }
    api.me().then(res => {
      if (!res) return
      setStoreName(res.store_id?.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase()) || '')
      setReady(true)
    })
  }, [router])

  if (!ready) return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="text-gray-400 text-sm">Loading...</div>
    </div>
  )

  return (
    <div className="flex min-h-screen">
      <Sidebar storeName={storeName} />
      <main className="flex-1 overflow-auto">{children}</main>
    </div>
  )
}
