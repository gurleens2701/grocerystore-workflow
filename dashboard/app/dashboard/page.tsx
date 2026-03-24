'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

type Sale = {
  date: string
  day_of_week: string
  product_sales: number
  grand_total: number
  cash_drop: number
  card: number
  total_transactions: number
  over_short: number | null
}

export default function DashboardPage() {
  const [sales, setSales] = useState<Sale[]>([])
  const [loading, setLoading] = useState(true)
  const [days, setDays] = useState(7)

  useEffect(() => {
    setLoading(true)
    api.sales(days).then(data => {
      setSales(data || [])
      setLoading(false)
    })
  }, [days])

  const totalSales = sales.reduce((s, r) => s + r.grand_total, 0)

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
            <div className="text-2xl font-bold mt-1">${totalSales.toLocaleString('en-US', { minimumFractionDigits: 2 })}</div>
          </div>
          <div className="bg-white rounded-xl border p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">Days Logged</div>
            <div className="text-2xl font-bold mt-1">{sales.length}</div>
          </div>
          <div className="bg-white rounded-xl border p-4">
            <div className="text-xs text-gray-500 uppercase tracking-wide">Avg Daily</div>
            <div className="text-2xl font-bold mt-1">
              ${sales.length ? (totalSales / sales.length).toLocaleString('en-US', { minimumFractionDigits: 2 }) : '0.00'}
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
                <tr key={row.date} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="font-medium">{row.day_of_week}</div>
                    <div className="text-xs text-gray-400">{row.date}</div>
                  </td>
                  <td className="px-4 py-3 text-right">${row.product_sales.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right font-medium">${row.grand_total.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right">${row.cash_drop.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right">${row.card.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right">{row.total_transactions}</td>
                  <td className="px-4 py-3 text-right">
                    {row.over_short === null ? (
                      <span className="text-gray-300">—</span>
                    ) : (
                      <span className={row.over_short >= 0 ? 'text-green-600' : 'text-red-500'}>
                        {row.over_short >= 0 ? '+' : ''}${row.over_short.toFixed(2)}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </AuthGuard>
  )
}
