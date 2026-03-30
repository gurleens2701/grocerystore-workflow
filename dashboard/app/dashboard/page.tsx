'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api, getActiveStore } from '@/lib/api'

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
  check_amount: number
  lotto_po: number
  lotto_cr: number
  atm: number
  pull_tab: number
  coupon: number
  food_stamp: number
  loyalty: number
  vendor_payout: number
  total_transactions: number
  over_short: number | null
  departments: Dept[]
}

function fmt(n: number) {
  return '$' + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function currentMonth() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

function monthLabel(ym: string) {
  const [y, m] = ym.split('-').map(Number)
  return new Date(y, m - 1, 1).toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}

function prevMonth(ym: string) {
  const [y, m] = ym.split('-').map(Number)
  const d = new Date(y, m - 2, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function nextMonth(ym: string) {
  const [y, m] = ym.split('-').map(Number)
  const d = new Date(y, m, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function Row({ label, value, bold }: { label: string; value: number | null | undefined; bold?: boolean }) {
  const display = value == null || value === 0 ? <span className="text-gray-300">—</span> : fmt(value)
  return (
    <div className={`flex justify-between ${bold ? 'font-semibold text-gray-800' : ''}`}>
      <span className="text-gray-500">{label}</span>
      <span>{display}</span>
    </div>
  )
}

function ExpandedRow({ row }: { row: Sale }) {
  const depts = (row.departments || []).filter(d => d.name !== 'TOTAL' && d.sales > 0).sort((a, b) => b.sales - a.sales)
  const deptTotal = depts.reduce((s, d) => s + d.sales, 0)

  const rightTotal = (row.cash_drop || 0) + (row.card || 0) + (row.check_amount || 0)
    + (row.lotto_po || 0) + (row.lotto_cr || 0) + (row.atm || 0)
    + (row.pull_tab || 0) + (row.coupon || 0) + (row.food_stamp || 0)
    + (row.loyalty || 0) + (row.vendor_payout || 0)

  return (
    <tr className="bg-blue-50">
      <td colSpan={7} className="px-6 py-4">
        <div className="grid grid-cols-2 gap-8 text-sm">

          {/* LEFT — departments + other + grand total */}
          <div className="space-y-1">
            <div className="font-semibold text-gray-700 mb-2 uppercase tracking-wide text-xs">Left Side</div>

            {/* Departments */}
            {depts.length === 0
              ? <div className="text-gray-400 text-xs">No department data</div>
              : depts.map(d => (
                <div key={d.name} className="flex justify-between">
                  <span className="text-gray-500 capitalize">{d.name.toLowerCase()}</span>
                  <span>{fmt(d.sales)}</span>
                </div>
              ))
            }

            {/* Dept subtotal */}
            <div className="flex justify-between border-t pt-1 mt-1 font-semibold text-gray-700">
              <span>Product Sales Total</span>
              <span>{fmt(deptTotal || row.product_sales)}</span>
            </div>

            {/* Other items */}
            <div className="pt-1 space-y-1">
              <Row label="Lotto In"     value={row.lotto_in} />
              <Row label="Lotto Online" value={row.lotto_online} />
              <Row label="Sales Tax"    value={row.sales_tax} />
              <Row label="GPI"          value={row.gpi} />
            </div>

            {/* Grand total */}
            <div className="flex justify-between border-t pt-1 mt-1 font-bold text-gray-900 text-base">
              <span>Grand Total</span>
              <span>{fmt(row.grand_total)}</span>
            </div>
          </div>

          {/* RIGHT — payments + total + over/short */}
          <div className="space-y-1">
            <div className="font-semibold text-gray-700 mb-2 uppercase tracking-wide text-xs">Right Side</div>

            <Row label="Cash Drop"    value={row.cash_drop} />
            <Row label="C. Card"      value={row.card} />
            <Row label="Check"        value={row.check_amount} />
            <Row label="Lotto P.O."   value={row.lotto_po} />
            <Row label="Lotto CR."    value={row.lotto_cr} />
            <Row label="ATM"          value={row.atm} />
            <Row label="Pull Tab"     value={row.pull_tab} />
            <Row label="Coupon"       value={row.coupon} />
            <Row label="Food Stamp"   value={row.food_stamp} />
            <Row label="Loyalty"      value={row.loyalty} />
            <Row label="Vendor Payout" value={row.vendor_payout} />

            {/* Right total */}
            <div className="flex justify-between border-t pt-1 mt-1 font-bold text-gray-900 text-base">
              <span>Right Total</span>
              <span>{fmt(rightTotal)}</span>
            </div>

            {/* Over / Short */}
            <div className="flex justify-between pt-1 font-semibold">
              <span className="text-gray-700">Over / Short</span>
              <span className={
                row.over_short === null ? 'text-gray-300' :
                row.over_short >= 0 ? 'text-green-600' : 'text-red-500'
              }>
                {row.over_short === null ? '—' : `${row.over_short >= 0 ? '+' : ''}${fmt(row.over_short)}`}
              </span>
            </div>
          </div>

        </div>
      </td>
    </tr>
  )
}

export default function DashboardPage() {
  const [sales, setSales]     = useState<Sale[]>([])
  const [loading, setLoading] = useState(true)
  const [month, setMonth]     = useState(currentMonth)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setExpanded(null)
    const sid = getActiveStore()
    const params = new URLSearchParams({ month })
    if (sid) params.set('store_id', sid)
    fetch(`/api/ledger/sales?${params}`, {
      headers: { Authorization: `Bearer ${document.cookie.match(/token=([^;]+)/)?.[1] || ''}` },
    })
      .then(r => r.json())
      .then(data => { setSales(Array.isArray(data) ? data : []); setLoading(false) })
      .catch(() => setLoading(false))
  }, [month])

  const totalSales = sales.reduce((s, r) => s + r.grand_total, 0)
  const isCurrentMonth = month === currentMonth()

  function toggle(date: string) {
    setExpanded(prev => prev === date ? null : date)
  }

  return (
    <AuthGuard>
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-bold">Daily Sales</h1>

          {/* Month navigator */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => setMonth(prevMonth)}
              className="px-3 py-1.5 rounded-lg border border-gray-300 text-sm hover:bg-gray-50 transition"
            >
              ‹
            </button>
            <span className="text-sm font-medium w-36 text-center">{monthLabel(month)}</span>
            <button
              onClick={() => setMonth(nextMonth)}
              disabled={isCurrentMonth}
              className="px-3 py-1.5 rounded-lg border border-gray-300 text-sm hover:bg-gray-50 transition disabled:opacity-30 disabled:cursor-not-allowed"
            >
              ›
            </button>
          </div>
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
                <tr><td colSpan={7} className="text-center py-8 text-gray-400">No sales data for {monthLabel(month)}</td></tr>
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
