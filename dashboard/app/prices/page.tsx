'use client'
import { useEffect, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

type PriceRow = {
  vendor: string
  item_name: string
  canonical_name: string
  unit_price: number
  category: string
  invoice_date: string
}

export default function PricesPage() {
  const [rows, setRows] = useState<PriceRow[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    api.prices(query).then(data => {
      setRows(data || [])
      setLoading(false)
    })
  }, [query])

  return (
    <AuthGuard>
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-bold">Price Database</h1>
          <div className="text-sm text-gray-400">{rows.length} items</div>
        </div>

        <input
          type="text"
          placeholder="Search items... (e.g. marlboro red, coke 20oz)"
          value={query}
          onChange={e => setQuery(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-4 py-2 text-sm mb-4 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />

        <div className="bg-white rounded-xl border overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Item</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Vendor</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Category</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Unit Price</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600">Last Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {loading ? (
                <tr><td colSpan={5} className="text-center py-8 text-gray-400">Searching...</td></tr>
              ) : rows.length === 0 ? (
                <tr><td colSpan={5} className="text-center py-8 text-gray-400">
                  {query ? 'No items found' : 'Upload vendor invoices to populate the price database'}
                </td></tr>
              ) : rows.map((row, i) => (
                <tr key={i} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <div className="font-medium">{row.canonical_name || row.item_name}</div>
                    {row.canonical_name && row.item_name !== row.canonical_name && (
                      <div className="text-xs text-gray-400">{row.item_name}</div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-600">{row.vendor}</td>
                  <td className="px-4 py-3">
                    {row.category && (
                      <span className="bg-gray-100 text-gray-600 text-xs px-2 py-0.5 rounded-full">{row.category}</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right font-medium">${row.unit_price.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right text-gray-400 text-xs">{row.invoice_date}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </AuthGuard>
  )
}
