'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

type Dept = { name: string; sales: number; items?: number }

type Sale = {
  date: string
  day_of_week: string
  product_sales: number
  lotto_in: number
  lotto_online: number
  sales_tax: number
  gpi: number
  grand_total: number
  cash_drop: number
  card: number
  lotto_po: number
  lotto_cr: number
  food_stamp: number
  total_transactions: number
  over_short: number | null
  departments: Dept[]
}

function fmt(n: number) {
  return '$' + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function ExpandedRow({ row }: { row: Sale }) {
  const depts = (row.departments || []).filter(d => d.name !== 'TOTAL').sort((a, b) => b.sales - a.sales)

  return (
    <tr className="bg-blue-50">
      <td colSpan={7} className="px-6 py-4">
        <div className="grid grid-cols-2 gap-6 text-sm">

          {/* Left — department breakdown */}
          <div>
            <div className="font-semibold text-gray-700 mb-2 uppercase tracking-wide text-xs">Sales by Department</div>
            <div className="space-y-1">
              {depts.length === 0 ? (
                <div className="text-gray-400 text-xs">No department data</div>
              ) : depts.map(d => (
                <div key={d.name} className="flex justify-between">
                  <span className="text-gray-500 capitalize">{d.name.toLowerCase()}</span>
                  <span className="font-medium">{fmt(d.sales)}</span>
                </div>
              ))}
              <div className="flex justify-between border-t pt-1 mt-1">
                <span className="text-gray-500">Lotto In</span>
                <span>{fmt(row.lotto_in)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Lotto Online</span>
                <span>{fmt(row.lotto_online)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Sales Tax</span>
                <span>{fmt(row.sales_tax)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">GPI</span>
                <span>{fmt(row.gpi)}</span>
              </div>
              <div className="flex justify-between border-t pt-1 mt-1 font-semibold">
                <span className="text-gray-700">Grand Total</span>
                <span>{fmt(row.grand_total)}</span>
              </div>
            </div>
          </div>

          {/* Right — payments */}
          <div>
            <div className="font-semibold text-gray-700 mb-2 uppercase tracking-wide text-xs">Payments</div>
            <div className="space-y-1">
              <div className="flex justify-between">
                <span className="text-gray-500">Cash Drop</span>
                <span>{fmt(row.cash_drop)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Credit/Debit</span>
                <span>{fmt(row.card)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Lotto P.O.</span>
                <span>{row.lotto_po ? fmt(row.lotto_po) : <span className="text-gray-300">—</span>}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Lotto CR.</span>
                <span>{row.lotto_cr ? fmt(row.lotto_cr) : <span className="text-gray-300">—</span>}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Food Stamp</span>
                <span>{row.food_stamp ? fmt(row.food_stamp) : <span className="text-gray-300">—</span>}</span>
              </div>
              <div className="flex justify-between border-t pt-1 mt-1 font-semibold">
                <span className="text-gray-700">Over / Short</span>
                <span className={
                  row.over_short === null ? 'text-gray-300' :
                  row.over_short >= 0 ? 'text-green-600' : 'text-red-500'
                }>
                  {row.over_short === null ? '—' :
                    `${row.over_short >= 0 ? '+' : ''}${fmt(row.over_short)}`}
                </span>
              </div>
            </div>
          </div>

        </div>
      </td>
    </tr>
  )
}

export default function DashboardPage() {
  const [sales, setSales] = useState<Sale[]>([])
  const [loading, setLoading] = useState(true)
  const [days, setDays] = useState(7)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    api.sales(days).then(data => {
      setSales(data || [])
      setLoading(false)
    })
  }, [days])

  const totalSales = sales.reduce((s, r) => s + r.grand_total, 0)

  function toggle(date: string) {
    setExpanded(prev => prev === date ? null : date)
  }

  return (
    <AuthGuard>
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-bold">Daily Sales</h1>
          <select
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm"
          >
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
          </select>
        </div>

        {/* Summary cards */}
        <div className="grid grid-cols-3 gap-4 mb-6">
          <div className="bg-white rounded-xl border p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">Total Sales</div>
            <div className="text-2xl font-bold mt-1">{fmt(totalSales)}</div>
          </div>
          <div className="bg-white rounded-xl border p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">Days Logged</div>
            <div className="text-2xl font-bold mt-1">{sales.length}</div>
          </div>
          <div className="bg-white rounded-xl border p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">Avg Daily</div>
            <div className="text-2xl font-bold mt-1">
              {sales.length ? fmt(totalSales / sales.length) : '$0.00'}
            </div>
          </div>
        </div>

        {/* Sales table */}
        <div className="bg-white rounded-xl border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Date</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Product Sales</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Grand Total</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Cash Drop</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Card</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Txns</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Over/Short</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {loading ? (
                <tr><td colSpan={7} className="text-center py-8 text-gray-400">Loading...</td></tr>
              ) : sales.length === 0 ? (
                <tr><td colSpan={7} className="text-center py-8 text-gray-400">No sales data yet</td></tr>
              ) : sales.map(row => (
                <>
                  <tr
                    key={row.date}
                    onClick={() => toggle(row.date)}
                    className="hover:bg-gray-50 cursor-pointer select-none"
                  >
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <span className="text-gray-400 text-xs">{expanded === row.date ? '▼' : '▶'}</span>
                        <div>
                          <div className="font-medium">{row.day_of_week}</div>
                          <div className="text-xs text-gray-400">{row.date}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right">{fmt(row.product_sales)}</td>
                    <td className="px-4 py-3 text-right font-medium">{fmt(row.grand_total)}</td>
                    <td className="px-4 py-3 text-right">{fmt(row.cash_drop)}</td>
                    <td className="px-4 py-3 text-right">{fmt(row.card)}</td>
                    <td className="px-4 py-3 text-right">{row.total_transactions}</td>
                    <td className="px-4 py-3 text-right">
                      {row.over_short === null ? (
                        <span className="text-gray-300">—</span>
                      ) : (
                        <span className={row.over_short >= 0 ? 'text-green-600' : 'text-red-500'}>
                          {row.over_short >= 0 ? '+' : ''}{fmt(row.over_short)}
                        </span>
                      )}
                    </td>
                  </tr>
                  {expanded === row.date && <ExpandedRow key={`${row.date}-exp`} row={row} />}
                </>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </AuthGuard>
  )
}
