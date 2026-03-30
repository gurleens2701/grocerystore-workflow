'use client'

import { useCallback, useRef, useState } from 'react'
import { api } from '@/lib/api'

// ── Types ─────────────────────────────────────────────────────────────────────

interface ExtractedFields {
  product_sales: number | null
  lotto_in: number | null
  lotto_online: number | null
  sales_tax: number | null
  gpi: number | null
  cash_drop: number | null
  card: number | null
  check: number | null
  lotto_po: number | null
  lotto_cr: number | null
  food_stamp: number | null
  atm: number | null
  pull_tab: number | null
  coupon: number | null
  loyalty: number | null
}

interface Department { name: string; sales: number }

interface OcrResult {
  extracted: ExtractedFields
  departments: Department[]
  must_ask: string[]
  report_date: string | null
  error?: string
}

interface SubmitResult {
  date: string
  product_sales: number
  grand_total: number
  over_short: number
  departments: Department[]
  lotto_po: number
  lotto_cr: number
  food_stamp: number
  cash_drop: number
  card: number
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const FIELD_LABELS: Record<string, string> = {
  product_sales: 'Product Sales (TOTAL)',
  lotto_in:      'Instant Lotto Sales',
  lotto_online:  'Online Lotto Sales',
  sales_tax:     'Sales Tax',
  gpi:           'GPI / Fee Buster',
  cash_drop:     'Cash Drop (Safe)',
  card:          'Credit / Debit Card',
  check:         'Check',
  lotto_po:      'Lotto Payout (paid to winners)',
  lotto_cr:      'Lotto Credit (net lottery)',
  food_stamp:    'Food Stamp / EBT',
  atm:           'ATM',
  pull_tab:      'Pull Tab',
  coupon:        'Coupon',
  loyalty:       'Loyalty / Altria',
}

const LEFT_FIELDS  = ['product_sales', 'lotto_in', 'lotto_online', 'sales_tax', 'gpi']
const RIGHT_FIELDS = ['cash_drop', 'card', 'check', 'lotto_po', 'lotto_cr', 'food_stamp', 'atm', 'pull_tab', 'coupon', 'loyalty']

function fmt(n: number | null | undefined) {
  if (n == null) return ''
  return n.toFixed(2)
}

function overShortColor(v: number) {
  if (v > 0) return 'text-green-400'
  if (v < 0) return 'text-red-400'
  return 'text-gray-300'
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function DailyReportPage() {
  const fileRef = useRef<HTMLInputElement>(null)
  const [step, setStep]             = useState<'upload' | 'review' | 'done'>('upload')
  const [uploading, setUploading]   = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [ocr, setOcr]               = useState<OcrResult | null>(null)
  const [result, setResult]         = useState<SubmitResult | null>(null)
  const [fields, setFields]         = useState<Record<string, string>>({})
  const [reportDate, setReportDate] = useState(new Date().toISOString().slice(0, 10))
  const [dragOver, setDragOver]     = useState(false)
  const [error, setError]           = useState('')

  const handleFile = useCallback(async (file: File) => {
    setUploading(true)
    setError('')
    try {
      const data: OcrResult = await api.daily.uploadReport(file)
      if (data.error) { setError(data.error); return }

      // Pre-fill form from OCR extracted values
      const init: Record<string, string> = {}
      const all = [...LEFT_FIELDS, ...RIGHT_FIELDS]
      for (const f of all) {
        const val = (data.extracted as any)[f]
        init[f] = val != null ? String(val) : ''
      }
      setFields(init)
      if (data.report_date) setReportDate(data.report_date)
      setOcr(data)
      setStep('review')
    } catch (e: any) {
      setError(String(e))
    } finally {
      setUploading(false)
    }
  }, [])

  const onFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (f) handleFile(f)
  }

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files[0]
    if (f) handleFile(f)
  }

  const handleSubmit = async () => {
    setSubmitting(true)
    setError('')
    try {
      const payload: Record<string, any> = { date: reportDate }
      for (const f of [...LEFT_FIELDS, ...RIGHT_FIELDS]) {
        payload[f === 'check' ? 'check_amount' : f] = parseFloat(fields[f] || '0') || 0
      }
      if (ocr?.departments?.length) payload.departments = ocr.departments
      const data: SubmitResult = await api.daily.submit(payload)
      setResult(data)
      setStep('done')
    } catch (e: any) {
      setError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const isMustAsk = (f: string) => ocr?.must_ask?.includes(f)

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">Daily Report</h1>
        <p className="text-gray-400 text-sm mt-1">
          Upload your POS printout — the AI reads all the numbers automatically.
        </p>
      </div>

      {error && (
        <div className="bg-red-900/40 border border-red-700 text-red-300 rounded-lg px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {/* ── Step 1: Upload ── */}
      {step === 'upload' && (
        <div
          className={`border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-colors
            ${dragOver ? 'border-blue-400 bg-blue-900/20' : 'border-gray-600 hover:border-gray-400'}`}
          onClick={() => fileRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={onFileInput} />
          {uploading ? (
            <div className="text-gray-300 space-y-3">
              <div className="text-4xl animate-pulse">📋</div>
              <p>Reading your POS report...</p>
            </div>
          ) : (
            <div className="text-gray-400 space-y-3">
              <div className="text-5xl">📄</div>
              <p className="text-lg text-white">Drop your daily report here</p>
              <p className="text-sm">or click to browse — photos or scanned files work</p>
            </div>
          )}
        </div>
      )}

      {/* ── Step 2: Review & fill missing ── */}
      {step === 'review' && ocr && (
        <div className="space-y-6">
          {/* Date */}
          <div className="flex items-center gap-3">
            <label className="text-gray-400 text-sm w-28">Report Date</label>
            <input
              type="date"
              value={reportDate}
              onChange={e => setReportDate(e.target.value)}
              className="bg-gray-800 border border-gray-600 rounded px-3 py-1.5 text-white text-sm"
            />
          </div>

          {/* Departments from OCR */}
          {ocr.departments.length > 0 && (
            <div>
              <h3 className="text-gray-300 text-sm font-semibold mb-2">Department Breakdown (from report)</h3>
              <div className="bg-gray-800/50 rounded-lg p-4 font-mono text-sm space-y-1">
                {ocr.departments.map((d, i) => (
                  <div key={i} className="flex justify-between text-gray-300">
                    <span>{d.name}</span>
                    <span>${d.sales.toFixed(2)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Sales fields */}
          <div>
            <h3 className="text-gray-300 text-sm font-semibold mb-3">Sales Numbers</h3>
            <div className="space-y-2">
              {LEFT_FIELDS.map(f => (
                <FieldRow
                  key={f}
                  label={FIELD_LABELS[f]}
                  value={fields[f] || ''}
                  onChange={v => setFields(p => ({ ...p, [f]: v }))}
                  mustAsk={isMustAsk(f)}
                />
              ))}
            </div>
          </div>

          {/* Payment fields */}
          <div>
            <h3 className="text-gray-300 text-sm font-semibold mb-3">Payments / Cash</h3>
            <div className="space-y-2">
              {RIGHT_FIELDS.map(f => (
                <FieldRow
                  key={f}
                  label={FIELD_LABELS[f]}
                  value={fields[f] || ''}
                  onChange={v => setFields(p => ({ ...p, [f]: v }))}
                  mustAsk={isMustAsk(f)}
                />
              ))}
            </div>
          </div>

          {ocr.must_ask.length > 0 && (
            <div className="bg-yellow-900/30 border border-yellow-700/50 rounded-lg px-4 py-3 text-sm text-yellow-300">
              ⚠️ Fields highlighted in yellow were not found on your report — please fill them in.
            </div>
          )}

          <div className="flex gap-3">
            <button
              onClick={handleSubmit}
              disabled={submitting}
              className="flex-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white font-semibold py-3 rounded-lg transition-colors"
            >
              {submitting ? 'Logging...' : 'Log Daily Report →'}
            </button>
            <button
              onClick={() => { setStep('upload'); setOcr(null); setFields({}) }}
              className="px-4 py-3 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg transition-colors"
            >
              Retake
            </button>
          </div>
        </div>
      )}

      {/* ── Step 3: Complete sheet ── */}
      {step === 'done' && result && (
        <div className="space-y-6">
          <div className="bg-green-900/30 border border-green-700/50 rounded-lg px-4 py-3 text-green-300 text-sm">
            ✅ Logged to database and Google Sheets.
          </div>

          <div className="bg-gray-800/60 rounded-xl p-6 font-mono text-sm">
            <div className="text-gray-400 text-xs mb-4">
              {new Date(result.date + 'T00:00:00').toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}
            </div>

            {result.departments?.length > 0 && (
              <>
                <div className="text-gray-500 text-xs mb-1">DEPARTMENTS</div>
                {result.departments.map((d, i) => (
                  <div key={i} className="flex justify-between text-gray-300 py-0.5">
                    <span>{d.name}</span><span>${d.sales.toFixed(2)}</span>
                  </div>
                ))}
                <div className="border-t border-gray-700 my-2" />
              </>
            )}

            <div className="flex justify-between text-white font-bold">
              <span>PRODUCT SALES</span><span>${result.product_sales.toFixed(2)}</span>
            </div>
            <div className="flex justify-between text-gray-300 py-0.5">
              <span>GRAND TOTAL</span><span>${result.grand_total.toFixed(2)}</span>
            </div>

            <div className="border-t border-gray-700 my-3" />

            <div className="flex justify-between text-gray-300 py-0.5">
              <span>CASH DROP</span><span>${result.cash_drop.toFixed(2)}</span>
            </div>
            <div className="flex justify-between text-gray-300 py-0.5">
              <span>C.CARD</span><span>${result.card.toFixed(2)}</span>
            </div>
            {result.lotto_po > 0 && (
              <div className="flex justify-between text-gray-300 py-0.5">
                <span>LOTTO P.O</span><span>${result.lotto_po.toFixed(2)}</span>
              </div>
            )}
            {result.lotto_cr > 0 && (
              <div className="flex justify-between text-gray-300 py-0.5">
                <span>LOTTO CR.</span><span>${result.lotto_cr.toFixed(2)}</span>
              </div>
            )}
            {result.food_stamp > 0 && (
              <div className="flex justify-between text-gray-300 py-0.5">
                <span>FOOD STAMP</span><span>${result.food_stamp.toFixed(2)}</span>
              </div>
            )}

            <div className="border-t border-gray-700 my-3" />

            <div className={`flex justify-between font-bold text-lg ${overShortColor(result.over_short)}`}>
              <span>{result.over_short >= 0 ? 'OVER' : 'SHORT'}</span>
              <span>{result.over_short >= 0 ? '+' : ''}{result.over_short.toFixed(2)}</span>
            </div>
          </div>

          <button
            onClick={() => { setStep('upload'); setOcr(null); setFields({}); setResult(null) }}
            className="w-full py-3 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg transition-colors"
          >
            Log Another Day
          </button>
        </div>
      )}
    </div>
  )
}

// ── Field row component ───────────────────────────────────────────────────────

function FieldRow({
  label,
  value,
  onChange,
  mustAsk,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  mustAsk?: boolean
}) {
  return (
    <div className={`flex items-center gap-3 rounded-lg px-3 py-2
      ${mustAsk ? 'bg-yellow-900/20 border border-yellow-700/40' : 'bg-gray-800/40'}`}>
      <div className="flex-1 text-sm text-gray-300 min-w-0">
        {label}
        {mustAsk && <span className="ml-2 text-yellow-400 text-xs">required</span>}
      </div>
      <div className="flex items-center gap-1 shrink-0">
        <span className="text-gray-500 text-sm">$</span>
        <input
          type="number"
          step="0.01"
          min="0"
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder="0.00"
          className={`w-28 bg-gray-900 border rounded px-2 py-1 text-right text-sm text-white
            focus:outline-none focus:border-blue-500
            ${mustAsk ? 'border-yellow-600' : 'border-gray-600'}`}
        />
      </div>
    </div>
  )
}
