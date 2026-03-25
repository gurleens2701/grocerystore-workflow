'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

type Metric = {
  name: string
  value: string
  detail: string
  score: number
  max: number
  status: 'good' | 'warn' | 'bad'
}

type HealthData = {
  week: string
  score: number
  label: string
  label_color: 'green' | 'yellow' | 'orange' | 'red'
  metrics: Metric[]
  financials: { sales: number; inventory: number; payroll: number; other_expenses: number }
  days_missing: number
}

const statusColors = {
  good: 'text-green-600 bg-green-50 border-green-200',
  warn: 'text-yellow-700 bg-yellow-50 border-yellow-200',
  bad: 'text-red-600 bg-red-50 border-red-200',
}

const scoreRingColor = {
  green: '#22c55e',
  yellow: '#eab308',
  orange: '#f97316',
  red: '#ef4444',
}

function ScoreRing({ score, color }: { score: number; color: string }) {
  const r = 54
  const circ = 2 * Math.PI * r
  const dash = (score / 100) * circ
  const ringColor = scoreRingColor[color as keyof typeof scoreRingColor] ?? '#94a3b8'

  return (
    <div className="relative w-40 h-40 flex items-center justify-center">
      <svg className="absolute inset-0 -rotate-90" width="160" height="160">
        <circle cx="80" cy="80" r={r} fill="none" stroke="#e5e7eb" strokeWidth="12" />
        <circle
          cx="80" cy="80" r={r} fill="none"
          stroke={ringColor} strokeWidth="12"
          strokeDasharray={`${dash} ${circ}`}
          strokeLinecap="round"
          style={{ transition: 'stroke-dasharray 0.6s ease' }}
        />
      </svg>
      <div className="text-center z-10">
        <div className="text-4xl font-bold text-gray-900">{score}</div>
        <div className="text-xs text-gray-500 mt-0.5">out of 100</div>
      </div>
    </div>
  )
}

function MetricBar({ score, max, color }: { score: number; max: number; color: string }) {
  const pct = Math.round((score / max) * 100)
  return (
    <div className="w-full bg-gray-100 rounded-full h-1.5 mt-2">
      <div
        className="h-1.5 rounded-full transition-all duration-500"
        style={{ width: `${pct}%`, backgroundColor: scoreRingColor[color as keyof typeof scoreRingColor] ?? '#94a3b8' }}
      />
    </div>
  )
}

function fmt(n: number) {
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

export default function HealthPage() {
  const [data, setData] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.health().then(d => {
      if (d?.score !== undefined) setData(d)
      setLoading(false)
    })
  }, [])

  const labelColor = data ? {
    green: 'text-green-600',
    yellow: 'text-yellow-600',
    orange: 'text-orange-500',
    red: 'text-red-600',
  }[data.label_color] : ''

  return (
    <AuthGuard>
      <div className="p-6 max-w-2xl space-y-6">
        <div>
          <h1 className="text-xl font-bold">Weekly Health Score</h1>
          {data && <p className="text-sm text-gray-400 mt-0.5">{data.week}</p>}
        </div>

        {loading && (
          <div className="bg-white border rounded-xl p-8 text-center text-gray-400 text-sm">
            Calculating score...
          </div>
        )}

        {!loading && data && (
          <>
            {/* Score card */}
            <div className="bg-white border rounded-xl p-6 flex items-center gap-8">
              <ScoreRing score={data.score} color={data.label_color} />
              <div>
                <div className={`text-2xl font-bold ${labelColor}`}>{data.label}</div>
                <div className="text-sm text-gray-500 mt-1">Overall store health this week</div>
                {data.days_missing > 0 && (
                  <div className="mt-3 text-xs text-yellow-700 bg-yellow-50 border border-yellow-200 rounded-lg px-3 py-1.5">
                    ⚠️ {data.days_missing} day{data.days_missing > 1 ? 's' : ''} not logged this week
                  </div>
                )}
              </div>
            </div>

            {/* Metrics */}
            <div className="grid grid-cols-1 gap-3">
              {data.metrics.map(m => (
                <div key={m.name} className={`border rounded-xl p-4 ${statusColors[m.status]}`}>
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{m.name}</span>
                    <span className="text-lg font-bold">{m.value}</span>
                  </div>
                  <div className="text-xs opacity-70 mt-0.5">{m.detail}</div>
                  <MetricBar score={m.score} max={m.max} color={
                    m.status === 'good' ? 'green' : m.status === 'warn' ? 'yellow' : 'red'
                  } />
                </div>
              ))}
            </div>

            {/* Financials */}
            <div className="bg-white border rounded-xl p-5">
              <h2 className="text-sm font-semibold text-gray-700 mb-3">This Week's Financials</h2>
              <div className="grid grid-cols-2 gap-y-3 gap-x-6">
                <div>
                  <div className="text-xs text-gray-400">Total Sales</div>
                  <div className="text-base font-semibold text-gray-900">{fmt(data.financials.sales)}</div>
                </div>
                <div>
                  <div className="text-xs text-gray-400">Inventory Bought</div>
                  <div className="text-base font-semibold text-gray-900">{fmt(data.financials.inventory)}</div>
                </div>
                {data.financials.payroll > 0 && (
                  <div>
                    <div className="text-xs text-gray-400">Payroll</div>
                    <div className="text-base font-semibold text-gray-900">{fmt(data.financials.payroll)}</div>
                  </div>
                )}
                {data.financials.other_expenses > 0 && (
                  <div>
                    <div className="text-xs text-gray-400">Other Expenses</div>
                    <div className="text-base font-semibold text-gray-900">{fmt(data.financials.other_expenses)}</div>
                  </div>
                )}
              </div>
            </div>
          </>
        )}

        {!loading && !data && (
          <div className="bg-white border rounded-xl p-8 text-center text-gray-400 text-sm">
            No data available yet.
          </div>
        )}
      </div>
    </AuthGuard>
  )
}
