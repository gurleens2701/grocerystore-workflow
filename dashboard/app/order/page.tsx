'use client'
import { useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

export default function OrderPage() {
  const [input, setInput] = useState('')
  const [result, setResult] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  function parseInput(text: string) {
    return text.split('\n')
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => {
        // Extract quantity: "item x5", "5 item", "item - 5", "item (5)"
        let qty = 1
        let item = line

        let m = line.match(/\s*[x×]\s*(\d+)\s*$/i)
        if (m) { qty = parseInt(m[1]); item = line.slice(0, m.index).trim() }
        else if ((m = line.match(/\s*\((\d+)\)\s*$/))) { qty = parseInt(m[1]); item = line.slice(0, m.index).trim() }
        else if ((m = line.match(/\s*-\s*(\d+)\s*$/))) { qty = parseInt(m[1]); item = line.slice(0, m.index).trim() }
        else if ((m = line.match(/^(\d+)\s+/))) { qty = parseInt(m[1]); item = line.slice(m[0].length).trim() }

        return { item, qty }
      })
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!input.trim()) return
    setLoading(true)
    setError('')
    setResult('')

    const items = parseInput(input)
    const res = await api.order(items)
    setLoading(false)

    if (res?.summary) {
      setResult(res.summary)
    } else {
      setError('Failed to compile order. Make sure items are in the price database.')
    }
  }

  return (
    <AuthGuard>
      <div className="p-6 max-w-3xl">
        <h1 className="text-xl font-bold mb-6">Order Builder</h1>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Order List — one item per line with quantity
            </label>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              rows={10}
              placeholder={`marlboro red short x5\ncoke 20oz x10\ndoritos nacho x3\nblack mild ft sweet x2`}
              className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            />
            <div className="text-xs text-gray-400 mt-1">
              Formats: "item x5" · "5 item" · "item (5)" · "item - 5"
            </div>
          </div>

          <button
            type="submit"
            disabled={loading || !input.trim()}
            className="bg-blue-600 hover:bg-blue-700 text-white px-6 py-2 rounded-lg text-sm font-medium transition disabled:opacity-50"
          >
            {loading ? 'Finding best prices...' : 'Compile Order'}
          </button>
        </form>

        {error && (
          <div className="mt-4 bg-red-50 border border-red-200 rounded-lg p-4 text-sm text-red-600">{error}</div>
        )}

        {result && (
          <div className="mt-6 bg-white border rounded-xl p-5">
            <h2 className="font-semibold text-sm text-gray-700 mb-3">Results</h2>
            <pre className="text-sm whitespace-pre-wrap font-mono text-gray-800">{result}</pre>
          </div>
        )}
      </div>
    </AuthGuard>
  )
}
