'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

export default function HealthPage() {
  const [report, setReport] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.health().then(data => {
      setReport(data?.report || 'No data yet.')
      setLoading(false)
    })
  }, [])

  return (
    <AuthGuard>
      <div className="p-6 max-w-xl">
        <h1 className="text-xl font-bold mb-6">Weekly Health Score</h1>
        <div className="bg-white border rounded-xl p-6">
          {loading ? (
            <div className="text-gray-400 text-sm">Loading...</div>
          ) : (
            <pre className="text-sm whitespace-pre-wrap font-mono text-gray-800 leading-relaxed">{report}</pre>
          )}
        </div>
      </div>
    </AuthGuard>
  )
}
