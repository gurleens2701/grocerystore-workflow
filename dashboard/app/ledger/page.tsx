'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'

// ── Constants ────────────────────────────────────────────────────────────────

const DAILY_SALES_COLS: { key: string; label: string; auto?: boolean }[] = [
  { key: 'product_sales', label: 'Product Sales', auto: true },
  { key: 'lotto_in', label: 'Lotto In', auto: true },
  { key: 'lotto_online', label: 'Lotto Online', auto: true },
  { key: 'sales_tax', label: 'Sales Tax', auto: true },
  { key: 'gpi', label: 'GPI', auto: true },
  { key: 'grand_total', label: 'Grand Total', auto: true },
  { key: 'cash_drop', label: 'Cash Drop' },
  { key: 'card', label: 'Card' },
  { key: 'check_amount', label: 'Check' },
  { key: 'lotto_po', label: 'Lotto PO' },
  { key: 'lotto_cr', label: 'Lotto CR' },
  { key: 'atm', label: 'ATM' },
  { key: 'pull_tab', label: 'Pull Tab' },
  { key: 'coupon', label: 'Coupon' },
  { key: 'food_stamp', label: 'Food Stamp' },
  { key: 'loyalty', label: 'Loyalty' },
  { key: 'vendor_payout', label: 'Payout' },
]

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined) {
  if (n === null || n === undefined) return ''
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

function todayISO() {
  return new Date().toISOString().slice(0, 10)
}

// ── Cell state for inline edits ──────────────────────────────────────────────

type CellStatus = 'idle' | 'saving' | 'ok' | 'err'

// ── Daily Sales Tab ──────────────────────────────────────────────────────────

type SalesRow = Record<string, number | null | undefined> & {
  date: string
  day_of_week: string
  over_short: number | null
}

function EditableCell({
  value,
  onSave,
  dimmed,
}: {
  value: number | null | undefined
  onSave: (v: number) => Promise<void>
  dimmed?: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState('')
  const [status, setStatus] = useState<CellStatus>('idle')
  const inputRef = useRef<HTMLInputElement>(null)

  function startEdit() {
    setText(value != null ? String(value) : '')
    setEditing(true)
    setStatus('idle')
    setTimeout(() => inputRef.current?.select(), 0)
  }

  async function commit() {
    const num = parseFloat(text)
    if (isNaN(num)) {
      setEditing(false)
      return
    }
    setEditing(false)
    setStatus('saving')
    try {
      await onSave(num)
      setStatus('ok')
      setTimeout(() => setStatus('idle'), 1500)
    } catch {
      setStatus('err')
      setTimeout(() => setStatus('idle'), 2000)
    }
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={text}
        onChange={e => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={e => {
          if (e.key === 'Enter') commit()
          if (e.key === 'Escape') setEditing(false)
        }}
        className="w-24 text-right border border-blue-400 rounded px-1 py-0.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-400"
        autoFocus
      />
    )
  }

  return (
    <span
      onClick={startEdit}
      className={`cursor-pointer px-1 rounded hover:bg-blue-50 text-sm select-none ${dimmed ? 'text-gray-400' : 'text-gray-800'}`}
      title="Click to edit"
    >
      {status === 'saving' && <span className="text-blue-400 text-xs mr-1">...</span>}
      {status === 'ok' && <span className="text-green-500 text-xs mr-1">✓</span>}
      {status === 'err' && <span className="text-red-500 text-xs mr-1">✗</span>}
      {value != null ? fmt(value) : <span className="text-gray-300">—</span>}
    </span>
  )
}

function DailySalesTab({ month }: { month: string }) {
  const [rows, setRows] = useState<SalesRow[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    api.ledger.getSales(month).then((data: SalesRow[] | null) => {
      setRows(data || [])
      setLoading(false)
    })
  }, [month])

  async function handleSave(date: string, field: string, value: number) {
    const updated = await api.ledger.putSales({ date, field, value })
    if (updated) {
      setRows(prev => prev.map(r => r.date === date ? { ...r, ...updated } : r))
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead>
          <tr className="bg-gray-50 border-b border-gray-200">
            <th className="text-left px-3 py-2 font-semibold text-gray-500 uppercase tracking-widest sticky left-0 bg-gray-50 z-10">Date</th>
            {DAILY_SALES_COLS.map(c => (
              <th key={c.key} className={`text-right px-3 py-2 font-semibold uppercase tracking-widest whitespace-nowrap ${c.auto ? 'text-gray-400' : 'text-gray-500'}`}>
                {c.label}
              </th>
            ))}
            <th className="text-right px-3 py-2 font-semibold text-gray-500 uppercase tracking-widest">Over/Short</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {loading ? (
            <tr><td colSpan={DAILY_SALES_COLS.length + 2} className="text-center py-8 text-gray-400">Loading...</td></tr>
          ) : rows.length === 0 ? (
            <tr><td colSpan={DAILY_SALES_COLS.length + 2} className="text-center py-8 text-gray-400">No data for this month</td></tr>
          ) : rows.map(row => (
            <tr key={row.date} className="hover:bg-gray-50 group">
              <td className="px-3 py-2 sticky left-0 bg-white group-hover:bg-gray-50 z-10 whitespace-nowrap">
                <div className="font-medium text-gray-800">{row.date}</div>
                <div className="text-gray-400 text-xs">{row.day_of_week}</div>
              </td>
              {DAILY_SALES_COLS.map(c => (
                <td key={c.key} className="px-3 py-1.5 text-right">
                  <EditableCell
                    value={row[c.key] as number | null | undefined}
                    onSave={v => handleSave(row.date, c.key, v)}
                    dimmed={c.auto}
                  />
                </td>
              ))}
              <td className="px-3 py-1.5 text-right">
                {row.over_short === null || row.over_short === undefined ? (
                  <span className="text-gray-300">—</span>
                ) : (
                  <span className={`text-sm font-medium ${row.over_short >= 0 ? 'text-green-600' : 'text-red-500'}`}>
                    {row.over_short >= 0 ? '+' : ''}{fmt(row.over_short)}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Generic add-row / list tab ────────────────────────────────────────────────

type LedgerRow = { id: number; date: string; amount: number } & Record<string, string | number>

interface ListTabProps {
  rows: LedgerRow[]
  loading: boolean
  columns: { key: string; label: string; type: 'text' | 'combo' | 'date' | 'amount'; options?: string[] }[]
  onAdd: (row: Record<string, string | number>) => Promise<void>
  onDelete: (id: number) => Promise<void>
}

// ComboInput: text input with datalist suggestions (accepts any value + shows known ones)
function ComboInput({
  id, value, options, label, onChange,
}: {
  id: string; value: string; options: string[]; label: string
  onChange: (v: string) => void
}) {
  const listId = `combo-${id}`
  return (
    <>
      <input
        list={listId}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={label}
        className="border border-gray-300 rounded px-2 py-1 text-sm w-full focus:outline-none focus:ring-1 focus:ring-blue-400"
      />
      <datalist id={listId}>
        {options.map(o => <option key={o} value={o} />)}
      </datalist>
    </>
  )
}

function ListTab({ rows, loading, columns, onAdd, onDelete }: ListTabProps) {
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [deleteId, setDeleteId] = useState<number | null>(null)

  function initDraft() {
    const d: Record<string, string> = {}
    columns.forEach(c => {
      if (c.type === 'date') d[c.key] = todayISO()
      else d[c.key] = ''
    })
    return d
  }

  function startAdd() {
    setDraft(initDraft())
    setAdding(true)
  }

  async function commitAdd() {
    const payload: Record<string, string | number> = {}
    for (const c of columns) {
      if (c.type === 'amount') {
        const num = parseFloat(draft[c.key] || '0')
        if (isNaN(num) || num <= 0) return
        payload[c.key] = num
      } else {
        if (!draft[c.key]) return
        payload[c.key] = draft[c.key]
      }
    }
    setSaving(true)
    try {
      await onAdd(payload)
      setAdding(false)
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(id: number) {
    setDeleteId(id)
    try {
      await onDelete(id)
    } finally {
      setDeleteId(null)
    }
  }

  const allCols = [...columns, { key: '_actions', label: '', type: 'text' as const }]

  return (
    <div>
      <div className="flex justify-end px-4 py-3 border-b border-gray-100">
        <button
          onClick={startAdd}
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition"
        >
          + Add
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead>
            <tr className="bg-gray-50 border-b border-gray-200">
              {columns.map(c => (
                <th key={c.key} className="text-left px-4 py-2 text-xs font-semibold text-gray-500 uppercase tracking-widest">
                  {c.label}
                </th>
              ))}
              <th className="px-4 py-2 w-16" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {loading ? (
              <tr><td colSpan={allCols.length} className="text-center py-8 text-gray-400">Loading...</td></tr>
            ) : (
              <>
                {rows.map(row => (
                  <tr key={row.id} className="hover:bg-gray-50">
                    {columns.map(c => (
                      <td key={c.key} className="px-4 py-2 text-gray-700">
                        {c.type === 'amount' ? fmt(row[c.key] as number) : String(row[c.key] ?? '')}
                      </td>
                    ))}
                    <td className="px-4 py-2 text-right">
                      <button
                        onClick={() => handleDelete(row.id)}
                        disabled={deleteId === row.id}
                        className="text-gray-400 hover:text-red-500 transition text-base leading-none"
                        title="Delete"
                      >
                        {deleteId === row.id ? '...' : '🗑'}
                      </button>
                    </td>
                  </tr>
                ))}
                {adding && (
                  <tr className="bg-blue-50">
                    {columns.map(c => (
                      <td key={c.key} className="px-4 py-1.5">
                        {c.type === 'combo' ? (
                          <ComboInput
                            id={`${c.key}-new`}
                            value={draft[c.key] || ''}
                            options={c.options || []}
                            label={c.label}
                            onChange={v => setDraft(prev => ({ ...prev, [c.key]: v }))}
                          />
                        ) : (
                          <input
                            type={c.type === 'amount' ? 'number' : c.type === 'date' ? 'date' : 'text'}
                            value={draft[c.key] || ''}
                            onChange={e => setDraft(prev => ({ ...prev, [c.key]: e.target.value }))}
                            step={c.type === 'amount' ? '0.01' : undefined}
                            min={c.type === 'amount' ? '0' : undefined}
                            placeholder={c.type === 'amount' ? '0.00' : c.label}
                            className="border border-gray-300 rounded px-2 py-1 text-sm w-full focus:outline-none focus:ring-1 focus:ring-blue-400"
                          />
                        )}
                      </td>
                    ))}
                    <td className="px-4 py-1.5 text-right">
                      <div className="flex gap-1 justify-end">
                        <button
                          onClick={commitAdd}
                          disabled={saving}
                          className="px-2 py-1 text-xs bg-green-600 text-white rounded hover:bg-green-700 transition"
                        >
                          {saving ? '...' : 'Save'}
                        </button>
                        <button
                          onClick={() => setAdding(false)}
                          className="px-2 py-1 text-xs bg-gray-200 text-gray-600 rounded hover:bg-gray-300 transition"
                        >
                          Cancel
                        </button>
                      </div>
                    </td>
                  </tr>
                )}
                {rows.length === 0 && !adding && (
                  <tr><td colSpan={allCols.length} className="text-center py-8 text-gray-400">No entries for this month</td></tr>
                )}
              </>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Invoices Tab ─────────────────────────────────────────────────────────────

function InvoicesTab({ month }: { month: string }) {
  const [rows, setRows] = useState<LedgerRow[]>([])
  const [loading, setLoading] = useState(true)
  const [suggestions, setSuggestions] = useState<string[]>([])

  useEffect(() => {
    api.ledger.suggestVendors().then((d: string[] | null) => setSuggestions(d || []))
  }, [])

  const load = useCallback(() => {
    setLoading(true)
    api.ledger.getInvoices(month).then((data: LedgerRow[] | null) => {
      setRows(data || [])
      setLoading(false)
    })
  }, [month])

  useEffect(() => { load() }, [load])

  async function handleAdd(payload: Record<string, string | number>) {
    const res = await api.ledger.putInvoice(payload as any)
    if (res) {
      load()
      // Refresh suggestions in case new vendor was added
      api.ledger.suggestVendors().then((d: string[] | null) => setSuggestions(d || []))
    }
  }

  async function handleDelete(id: number) {
    await api.ledger.deleteInvoice(id)
    setRows(prev => prev.filter(r => r.id !== id))
  }

  return (
    <ListTab
      rows={rows}
      loading={loading}
      columns={[
        { key: 'date', label: 'Date', type: 'date' },
        { key: 'vendor', label: 'Vendor', type: 'combo', options: suggestions },
        { key: 'amount', label: 'Amount', type: 'amount' },
      ]}
      onAdd={handleAdd}
      onDelete={handleDelete}
    />
  )
}

// ── Expenses Tab ──────────────────────────────────────────────────────────────

function ExpensesTab({ month }: { month: string }) {
  const [rows, setRows] = useState<LedgerRow[]>([])
  const [loading, setLoading] = useState(true)
  const [suggestions, setSuggestions] = useState<string[]>([])

  useEffect(() => {
    api.ledger.suggestExpenses().then((d: string[] | null) => setSuggestions(d || []))
  }, [])

  const load = useCallback(() => {
    setLoading(true)
    api.ledger.getExpenses(month).then((data: LedgerRow[] | null) => {
      setRows(data || [])
      setLoading(false)
    })
  }, [month])

  useEffect(() => { load() }, [load])

  async function handleAdd(payload: Record<string, string | number>) {
    const res = await api.ledger.putExpense(payload as any)
    if (res) {
      load()
      api.ledger.suggestExpenses().then((d: string[] | null) => setSuggestions(d || []))
    }
  }

  async function handleDelete(id: number) {
    await api.ledger.deleteExpense(id)
    setRows(prev => prev.filter(r => r.id !== id))
  }

  return (
    <ListTab
      rows={rows}
      loading={loading}
      columns={[
        { key: 'date', label: 'Date', type: 'date' },
        { key: 'category', label: 'Category', type: 'combo', options: suggestions },
        { key: 'amount', label: 'Amount', type: 'amount' },
      ]}
      onAdd={handleAdd}
      onDelete={handleDelete}
    />
  )
}

// ── Payroll Tab ───────────────────────────────────────────────────────────────

function PayrollTab({ month }: { month: string }) {
  const [rows, setRows] = useState<LedgerRow[]>([])
  const [loading, setLoading] = useState(true)
  const [suggestions, setSuggestions] = useState<string[]>([])

  useEffect(() => {
    api.ledger.suggestEmployees().then((d: string[] | null) => setSuggestions(d || []))
  }, [])

  const load = useCallback(() => {
    setLoading(true)
    api.ledger.getPayroll(month).then((data: LedgerRow[] | null) => {
      setRows(data || [])
      setLoading(false)
    })
  }, [month])

  useEffect(() => { load() }, [load])

  async function handleAdd(payload: Record<string, string | number>) {
    const res = await api.ledger.putPayroll(payload as any)
    if (res) {
      load()
      api.ledger.suggestEmployees().then((d: string[] | null) => setSuggestions(d || []))
    }
  }

  async function handleDelete(id: number) {
    await api.ledger.deletePayroll(id)
    setRows(prev => prev.filter(r => r.id !== id))
  }

  return (
    <ListTab
      rows={rows}
      loading={loading}
      columns={[
        { key: 'date', label: 'Date', type: 'date' },
        { key: 'employee', label: 'Employee', type: 'combo', options: suggestions },
        { key: 'amount', label: 'Amount', type: 'amount' },
      ]}
      onAdd={handleAdd}
      onDelete={handleDelete}
    />
  )
}

// ── Rebates Tab ───────────────────────────────────────────────────────────────

function RebatesTab({ month }: { month: string }) {
  const [rows, setRows] = useState<LedgerRow[]>([])
  const [loading, setLoading] = useState(true)
  const [suggestions, setSuggestions] = useState<string[]>([])

  useEffect(() => {
    api.ledger.suggestRebates().then((d: string[] | null) => setSuggestions(d || []))
  }, [])

  const load = useCallback(() => {
    setLoading(true)
    api.ledger.getRebates(month).then((data: LedgerRow[] | null) => {
      setRows(data || [])
      setLoading(false)
    })
  }, [month])

  useEffect(() => { load() }, [load])

  async function handleAdd(payload: Record<string, string | number>) {
    const res = await api.ledger.putRebate(payload as any)
    if (res) {
      load()
      api.ledger.suggestRebates().then((d: string[] | null) => setSuggestions(d || []))
    }
  }

  async function handleDelete(id: number) {
    await api.ledger.deleteRebate(id)
    setRows(prev => prev.filter(r => r.id !== id))
  }

  return (
    <ListTab
      rows={rows}
      loading={loading}
      columns={[
        { key: 'date', label: 'Date', type: 'date' },
        { key: 'vendor', label: 'Vendor', type: 'combo', options: suggestions },
        { key: 'amount', label: 'Amount', type: 'amount' },
      ]}
      onAdd={handleAdd}
      onDelete={handleDelete}
    />
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

type TabKey = 'sales' | 'invoices' | 'expenses' | 'payroll' | 'rebates'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'sales', label: 'Daily Sales' },
  { key: 'invoices', label: 'Invoices' },
  { key: 'expenses', label: 'Expenses' },
  { key: 'payroll', label: 'Payroll' },
  { key: 'rebates', label: 'Rebates' },
]

export default function LedgerPage() {
  const [month, setMonth] = useState(currentMonth)
  const [tab, setTab] = useState<TabKey>('sales')

  return (
    <AuthGuard>
      <div className="p-6">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-xl font-bold text-gray-900">Ledger</h1>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setMonth(prevMonth(month))}
              className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-100 transition text-gray-600"
              title="Previous month"
            >
              ←
            </button>
            <span className="text-sm font-semibold text-gray-700 min-w-[130px] text-center">
              {monthLabel(month)}
            </span>
            <button
              onClick={() => setMonth(nextMonth(month))}
              className="p-1.5 rounded-lg border border-gray-200 hover:bg-gray-100 transition text-gray-600"
              title="Next month"
            >
              →
            </button>
            <button
              onClick={() => setMonth(currentMonth())}
              className="px-3 py-1.5 text-xs border border-gray-200 rounded-lg hover:bg-gray-100 transition text-gray-500"
            >
              Today
            </button>
          </div>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
          {/* Tab bar */}
          <div className="flex border-b border-gray-100 px-4 pt-1">
            {TABS.map(t => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`px-4 py-3 text-sm font-medium transition border-b-2 -mb-px ${
                  tab === t.key
                    ? 'border-blue-600 text-blue-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* Tab content */}
          {tab === 'sales' && <DailySalesTab month={month} />}
          {tab === 'invoices' && <InvoicesTab month={month} />}
          {tab === 'expenses' && <ExpensesTab month={month} />}
          {tab === 'payroll' && <PayrollTab month={month} />}
          {tab === 'rebates' && <RebatesTab month={month} />}
        </div>

        {/* Legend for Daily Sales */}
        {tab === 'sales' && (
          <p className="text-xs text-gray-400 mt-3">
            Gray values are auto-filled by NRS nightly sync. All cells are editable — click to change.
          </p>
        )}
      </div>
    </AuthGuard>
  )
}
