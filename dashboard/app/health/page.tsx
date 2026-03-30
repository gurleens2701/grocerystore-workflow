'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

type TopItem = { vendor?: string; category?: string; name?: string; amount: number }
type DeptItem = { name: string; amount: number }

type HealthData = {
  period: string
  period_label: string
  days_logged: number
  days_in_period: number
  score: number
  label: string
  label_color: 'green' | 'yellow' | 'orange' | 'red'
  total_sales: number
  over_short_avg: number | null
  inventory_ordered: number
  inventory_pct_of_sales: number | null
  payroll_total: number
  other_expenses_total: number
  rebates_total: number
  top_rebates: TopItem[]
  top_departments: DeptItem[]
  top_vendors: TopItem[]
  top_expenses: TopItem[]
  top_payroll: TopItem[]
  days_missing: number
}

const PERIODS = [
  { key: 'this_week',  label: 'This Week' },
  { key: 'last_week',  label: 'Last Week' },
  { key: 'this_month', label: 'This Month' },
  { key: 'last_month', label: 'Last Month' },
]

const RING_COLOR: Record<string, string> = {
  green: '#22c55e', yellow: '#eab308', orange: '#f97316', red: '#ef4444',
}
const SCORE_TEXT: Record<string, string> = {
  green: 'text-green-600', yellow: 'text-yellow-500', orange: 'text-orange-500', red: 'text-red-500',
}
const DEPT_COLORS = ['#6366f1', '#f59e0b', '#10b981', '#ec4899', '#3b82f6']

function fmt(n: number) {
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function pct(part: number, total: number) {
  return total > 0 ? ((part / total) * 100).toFixed(1) + '%' : '—'
}

function ScoreRing({ score, color }: { score: number; color: string }) {
  const r = 48, circ = 2 * Math.PI * r
  return (
    <div className="relative w-28 h-28 flex items-center justify-center flex-shrink-0">
      <svg className="absolute inset-0 -rotate-90" width="112" height="112">
        <circle cx="56" cy="56" r={r} fill="none" stroke="#f1f5f9" strokeWidth="10" />
        <circle cx="56" cy="56" r={r} fill="none" stroke={RING_COLOR[color] ?? '#94a3b8'} strokeWidth="10"
          strokeDasharray={`${(score / 100) * circ} ${circ}`} strokeLinecap="round"
          style={{ transition: 'stroke-dasharray 0.7s ease' }} />
      </svg>
      <div className="text-center z-10">
        <div className="text-3xl font-black text-gray-900 leading-none">{score}</div>
        <div className="text-xs text-gray-400 mt-0.5">/ 100</div>
      </div>
    </div>
  )
}

function MiniBar({ value, max, color }: { value: number; max: number; color: string }) {
  const w = max > 0 ? Math.min((value / max) * 100, 100) : 0
  return (
    <div className="w-full bg-gray-100 rounded-full h-1.5">
      <div className="h-1.5 rounded-full transition-all duration-700" style={{ width: `${w}%`, backgroundColor: color }} />
    </div>
  )
}

function FinCard({ label, value, sub, icon, barValue, barMax, barColor }: {
  label: string; value: string; sub?: string; icon: string
  barValue?: number; barMax?: number; barColor?: string
}) {
  return (
    <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-5 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-bold text-gray-400 uppercase tracking-widest">{label}</span>
        <span className="text-xl">{icon}</span>
      </div>
      <div>
        <div className="text-2xl font-black text-gray-900">{value}</div>
        {sub && <div className="text-xs text-gray-400 mt-0.5">{sub}</div>}
      </div>
      {barValue != null && barMax != null && barColor && (
        <MiniBar value={barValue} max={barMax} color={barColor} />
      )}
    </div>
  )
}

function RankCard({ title, items, nameKey, icon }: {
  title: string; items: TopItem[]; nameKey: 'vendor' | 'category' | 'name'; icon: string
}) {
  const medals = ['#F59E0B', '#9CA3AF', '#CD7C3A']
  return (
    <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-5">
      <div className="flex items-center gap-2 mb-4">
        <span className="text-base">{icon}</span>
        <span className="text-sm font-bold text-gray-700">{title}</span>
      </div>
      {items.length === 0 ? (
        <div className="text-xs text-gray-300 py-2">No data</div>
      ) : (
        <div className="space-y-3">
          {items.map((item, i) => (
            <div key={i}>
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-xs font-black w-4" style={{ color: medals[i] ?? '#94a3b8' }}>#{i+1}</span>
                  <span className="text-sm font-semibold text-gray-800 truncate">{item[nameKey] ?? '—'}</span>
                </div>
                <span className="text-sm font-black text-gray-900 ml-3 flex-shrink-0">{fmt(item.amount)}</span>
              </div>
              <MiniBar value={item.amount} max={items[0].amount} color={medals[i] ?? '#94a3b8'} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function HealthPage() {
  const [period, setPeriod] = useState('this_week')
  const [data, setData] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    setData(null)
    api.health(period).then(d => {
      if (d?.score !== undefined) setData(d)
      setLoading(false)
    })
  }, [period])

  const os = data?.over_short_avg
  const osColor = os == null ? 'text-gray-400' : os <= 2 ? 'text-green-600' : os <= 5 ? 'text-yellow-500' : 'text-red-500'
  const osLabel = os == null ? 'not filled yet' : os <= 2 ? 'excellent ✅' : os <= 5 ? 'acceptable' : os <= 10 ? 'high ⚠️' : 'very high 🔴'

  return (
    <AuthGuard>
      <div className="min-h-screen bg-gray-50 p-6 lg:p-8 space-y-5">

        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div>
            <h1 className="text-2xl font-black text-gray-900">Store Health</h1>
            {data && <p className="text-sm text-gray-400 mt-0.5">{data.period_label}</p>}
          </div>
          <div className="flex gap-2 flex-wrap">
            {PERIODS.map(p => (
              <button key={p.key} onClick={() => setPeriod(p.key)}
                className={`px-4 py-2 rounded-xl text-sm font-semibold transition-all ${
                  period === p.key
                    ? 'bg-gray-900 text-white shadow-sm'
                    : 'bg-white border border-gray-200 text-gray-500 hover:border-gray-400'
                }`}>
                {p.label}
              </button>
            ))}
          </div>
        </div>

        {loading && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-20 text-center text-gray-300 text-sm">
            Calculating...
          </div>
        )}

        {!loading && data && (() => {
          const invPct  = data.inventory_pct_of_sales ?? 0
          const payPct  = data.total_sales > 0 ? (data.payroll_total / data.total_sales) * 100 : 0
          const expPct  = data.total_sales > 0 ? (data.other_expenses_total / data.total_sales) * 100 : 0
          const rebPct  = data.total_sales > 0 ? (data.rebates_total / data.total_sales) * 100 : 0
          const topDept = data.top_departments

          return (
            <>
              {/* ── Row 1: Score + Over/Short ── */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
                <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6 flex items-center gap-6">
                  <ScoreRing score={data.score} color={data.label_color} />
                  <div>
                    <div className="text-xs font-bold text-gray-400 uppercase tracking-widest mb-1">Health Score</div>
                    <div className={`text-2xl font-black ${SCORE_TEXT[data.label_color]}`}>{data.label}</div>
                    <div className="mt-2 text-sm text-gray-500">
                      {data.days_logged}/{data.days_in_period} days logged
                      {data.days_missing > 0 && (
                        <span className="ml-2 text-yellow-500 font-semibold">⚠️ {data.days_missing} missing</span>
                      )}
                    </div>
                  </div>
                </div>

                <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6 flex flex-col justify-between">
                  <div className="text-xs font-bold text-gray-400 uppercase tracking-widest">Over / Short Avg</div>
                  <div>
                    <div className={`text-3xl font-black mt-2 ${osColor}`}>
                      {os != null ? `$${os.toFixed(2)}` : 'N/A'}
                    </div>
                    <div className={`text-sm mt-1 font-medium ${osColor}`}>{osLabel}</div>
                  </div>
                  <MiniBar
                    value={os ?? 0} max={20}
                    color={os == null ? '#94a3b8' : os <= 2 ? '#22c55e' : os <= 5 ? '#eab308' : '#ef4444'}
                  />
                </div>
              </div>

              {/* ── Row 2: Total Sales (with dept breakdown) ── */}
              <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-6">
                <div className="flex flex-col lg:flex-row lg:items-start gap-6">

                  {/* Left: big number */}
                  <div className="flex-shrink-0 lg:w-56">
                    <div className="text-xs font-bold text-gray-400 uppercase tracking-widest mb-2">Total Sales</div>
                    <div className="text-4xl font-black text-gray-900">{fmt(data.total_sales)}</div>
                    <div className="text-sm text-gray-400 mt-1">{data.days_logged} days</div>
                  </div>

                  {/* Divider */}
                  <div className="hidden lg:block w-px bg-gray-100 self-stretch" />

                  {/* Right: top departments */}
                  <div className="flex-1">
                    <div className="text-xs font-bold text-gray-400 uppercase tracking-widest mb-3">Top Departments</div>
                    {topDept.length === 0 ? (
                      <div className="text-sm text-gray-300">No department data yet</div>
                    ) : (
                      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-x-8 gap-y-3">
                        {topDept.map((d, i) => (
                          <div key={d.name}>
                            <div className="flex items-center justify-between mb-1">
                              <span className="text-sm font-semibold text-gray-700">{d.name}</span>
                              <span className="text-sm font-black text-gray-900">{fmt(d.amount)}</span>
                            </div>
                            <div className="flex items-center gap-2">
                              <div className="flex-1 bg-gray-100 rounded-full h-1.5">
                                <div className="h-1.5 rounded-full transition-all duration-700"
                                  style={{
                                    width: `${topDept[0].amount > 0 ? (d.amount / topDept[0].amount) * 100 : 0}%`,
                                    backgroundColor: DEPT_COLORS[i] ?? '#94a3b8',
                                  }} />
                              </div>
                              <span className="text-xs text-gray-400 w-10 text-right">
                                {pct(d.amount, data.total_sales)}
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* ── Row 3: Inventory / Payroll / Expenses / Rebates ── */}
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-5">
                <FinCard label="Inventory Ordered" value={fmt(data.inventory_ordered)} icon="📦"
                  sub={invPct > 0 ? `${invPct.toFixed(1)}% of sales` : undefined}
                  barValue={invPct} barMax={100} barColor="#f59e0b" />
                <FinCard label="Payroll" value={fmt(data.payroll_total)} icon="👤"
                  sub={`${payPct.toFixed(1)}% of sales`}
                  barValue={payPct} barMax={100} barColor="#8b5cf6" />
                <FinCard label="Other Expenses" value={fmt(data.other_expenses_total)} icon="💸"
                  sub={`${expPct.toFixed(1)}% of sales`}
                  barValue={expPct} barMax={100} barColor="#ec4899" />
                <FinCard label="Rebates" value={fmt(data.rebates_total)} icon="🏷️"
                  sub={rebPct > 0 ? `${rebPct.toFixed(1)}% of sales` : 'no rebates this period'}
                  barValue={rebPct} barMax={100} barColor="#10b981" />
              </div>

              {/* ── Row 4: Rank cards ── */}
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-5">
                <RankCard title="Top Vendors" items={data.top_vendors} nameKey="vendor" icon="🏪" />
                <RankCard title="Top Expenses" items={data.top_expenses} nameKey="category" icon="💸" />
                <RankCard title="Top Payroll" items={data.top_payroll} nameKey="name" icon="👤" />
                <RankCard title="Top Rebates" items={data.top_rebates} nameKey="vendor" icon="🏷️" />
              </div>
            </>
          )
        })()}

        {!loading && !data && (
          <div className="bg-white rounded-2xl border border-gray-100 shadow-sm p-20 text-center text-gray-300 text-sm">
            No data for this period yet.
          </div>
        )}
      </div>
    </AuthGuard>
  )
}
