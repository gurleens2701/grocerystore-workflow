'use client'

import { useCallback, useEffect, useState } from 'react'
import { usePlaidLink } from 'react-plaid-link'
import { api } from '@/lib/api'

interface Account {
  account_id: string
  name: string
  official_name: string
  type: string
  subtype: string
  current: number
  available: number | null
  currency: string
}

interface Transaction {
  id: number
  date: string
  amount: number
  description: string
  category: string
  type: string
  is_matched: boolean
  matched_invoice_id: number | null
  review_status: string
  reconcile_type: string | null
  reconcile_subcategory: string | null
  confidence: number
  ai_guess: string
}

interface CCMismatch {
  bank_txn_id: number
  bank_date: string
  bank_amount: number
  bank_desc: string
  sale_date: string
  sale_card: number
  diff: number
}

const DATA_ITEMS = [
  { icon: '💰', what: 'Account balances',                    why: 'Show your current checking balance at a glance.' },
  { icon: '📋', what: 'Transaction history (last 90 days)', why: 'Match bank debits to your logged vendor invoices automatically.' },
  { icon: '✅', what: 'Transaction descriptions & amounts', why: 'Detect paid invoices, flag CC mismatches, and auto-categorize expenses.' },
]

const NEVER_ITEMS = [
  'Your bank login username or password',
  'Ability to move, send, or initiate any transactions',
  'Routing or account numbers',
  'Any data from other accounts you don\'t select',
]

// ── Plaid Link button ─────────────────────────────────────────────────────────

function PlaidButton({ onSuccess, onExit }: { onSuccess: (t: string) => void; onExit: () => void }) {
  const [linkToken, setLinkToken] = useState<string | null>(null)
  const [loading, setLoading]     = useState(true)

  useEffect(() => {
    api.bank.linkToken().then((d: any) => {
      if (d?.link_token) setLinkToken(d.link_token)
      setLoading(false)
    })
  }, [])

  const receivedRedirectUri = typeof window !== 'undefined' && window.location.href.includes('oauth_state_id')
    ? window.location.href : undefined

  const { open, ready } = usePlaidLink({
    token: linkToken ?? '',
    onSuccess: (public_token) => onSuccess(public_token),
    onExit: () => onExit(),
    receivedRedirectUri,
  })

  if (loading) return (
    <button disabled className="w-full py-3 bg-gray-200 text-gray-400 rounded-xl font-semibold">Loading...</button>
  )
  if (!linkToken) return (
    <div className="text-red-600 text-sm text-center bg-red-50 border border-red-200 rounded-lg p-3">
      Could not load Plaid. Check your PLAID_CLIENT_ID and PLAID_SECRET in your .env file.
    </div>
  )
  return (
    <button
      onClick={() => open()}
      disabled={!ready}
      className="w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-xl font-semibold transition-colors"
    >
      Connect Bank Account (Read-Only)
    </button>
  )
}

// ── Review card ───────────────────────────────────────────────────────────────

const RECONCILE_TYPES = [
  { label: 'Vendor Invoice', value: 'invoice',       needsSub: true,  subLabel: 'Vendor name' },
  { label: 'Expense',        value: 'expense',       needsSub: true,  subLabel: 'Category (e.g. Rent)' },
  { label: 'CC Settlement',  value: 'cc_settlement', needsSub: false, subLabel: '' },
  { label: 'Rebate',         value: 'rebate',        needsSub: true,  subLabel: 'Vendor name' },
  { label: 'Payroll',        value: 'payroll',       needsSub: true,  subLabel: 'Employee name' },
  { label: 'Skip / Fee',     value: 'skip',          needsSub: false, subLabel: '' },
]

function ReviewCard({ txn, onConfirm, onSkip }: {
  txn: Transaction
  onConfirm: (txnId: number, type: string, sub: string | null) => Promise<void>
  onSkip: (txnId: number) => Promise<void>
}) {
  const [selected, setSelected] = useState('')
  const [subcat, setSubcat]     = useState('')
  const [saving, setSaving]     = useState(false)
  const selectedType = RECONCILE_TYPES.find(t => t.value === selected)

  async function handleConfirm() {
    if (!selected) return
    setSaving(true)
    await onConfirm(txn.id, selected, selectedType?.needsSub ? subcat || null : null)
    setSaving(false)
  }

  const isOut = txn.amount > 0

  return (
    <div className="bg-white border border-yellow-200 rounded-xl p-4 space-y-3 shadow-sm">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="text-gray-900 font-medium truncate">{txn.description}</div>
          <div className="text-gray-500 text-xs mt-0.5">{txn.date} · {isOut ? 'OUT' : 'IN'}</div>
          {txn.ai_guess && txn.confidence > 0 && (
            <div className="text-gray-400 text-xs mt-0.5">
              AI guess: {txn.ai_guess} ({Math.round(txn.confidence * 100)}%)
            </div>
          )}
        </div>
        <div className={`font-bold text-lg shrink-0 ${isOut ? 'text-red-600' : 'text-green-600'}`}>
          {isOut ? '-' : '+'}${Math.abs(txn.amount).toFixed(2)}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {RECONCILE_TYPES.map(t => (
          <button
            key={t.value}
            onClick={() => { setSelected(t.value); setSubcat('') }}
            className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
              selected === t.value
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {selectedType?.needsSub && (
        <input
          type="text"
          placeholder={selectedType.subLabel}
          value={subcat}
          onChange={e => setSubcat(e.target.value)}
          className="w-full border border-gray-300 text-gray-900 text-sm rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
      )}

      <div className="flex gap-2">
        <button
          onClick={handleConfirm}
          disabled={!selected || saving || (selectedType?.needsSub && !subcat)}
          className="flex-1 py-1.5 bg-green-600 hover:bg-green-700 disabled:opacity-40 text-white rounded-lg text-sm font-medium transition-colors"
        >
          {saving ? 'Saving...' : '✓ Confirm'}
        </button>
        <button
          onClick={() => onSkip(txn.id)}
          className="px-4 py-1.5 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-lg text-sm transition-colors"
        >
          Skip
        </button>
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function BankPage() {
  const [connected, setConnected]       = useState(false)
  const [accounts, setAccounts]         = useState<Account[]>([])
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [pendingReviews, setPendingReviews] = useState<Transaction[]>([])
  const [ccMismatches, setCCMismatches] = useState<CCMismatch[]>([])
  const [loading, setLoading]           = useState(true)
  const [syncing, setSyncing]           = useState(false)
  const [exchanging, setExchanging]     = useState(false)
  const [showPlaid, setShowPlaid]       = useState(false)
  const [showDisconnect, setShowDisconnect] = useState(false)
  const [disconnecting, setDisconnecting]   = useState(false)
  const [error, setError]               = useState('')
  const [syncResult, setSyncResult]     = useState<{ added: number; matched: number } | null>(null)
  const [paidInvoices, setPaidInvoices] = useState<{ vendor: string; amount: number; invoice_date: string; bank_date: string }[]>([])

  const loadStatus = useCallback(async () => {
    try {
      const data: any = await api.bank.status()
      setConnected(data?.connected ?? false)
      setAccounts(data?.accounts ?? [])
    } finally {
      setLoading(false)
    }
  }, [])

  const loadTransactions = useCallback(async () => {
    const data: any = await api.bank.transactions(30)
    if (Array.isArray(data)) setTransactions(data)
  }, [])

  const loadReviews = useCallback(async () => {
    const data: any = await api.bank.pendingReviews()
    if (Array.isArray(data)) setPendingReviews(data)
    const mm: any = await api.bank.ccMismatches()
    if (Array.isArray(mm)) setCCMismatches(mm)
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])
  useEffect(() => {
    if (connected) { loadTransactions(); loadReviews() }
  }, [connected, loadTransactions, loadReviews])

  const handlePlaidSuccess = async (publicToken: string) => {
    setExchanging(true); setShowPlaid(false); setError('')
    try {
      const res: any = await api.bank.exchange(publicToken)
      if (res?.status === 'connected') {
        setConnected(true)
        await loadStatus(); await loadTransactions(); await loadReviews()
      } else {
        setError(res?.detail || 'Failed to connect bank.')
      }
    } catch (e: any) { setError(String(e)) }
    finally { setExchanging(false) }
  }

  const handleSync = async () => {
    setSyncing(true); setError('')
    try {
      const res: any = await api.bank.sync()
      if (res?.error) { setError(res.error); return }
      setSyncResult({ added: res.added, matched: res.matched })
      setAccounts(res.accounts ?? accounts)
      if (Array.isArray(res.paid_invoices) && res.paid_invoices.length > 0) setPaidInvoices(res.paid_invoices)
      await loadTransactions(); await loadReviews()
    } catch (e: any) { setError(String(e)) }
    finally { setSyncing(false) }
  }

  const handleDisconnect = async () => {
    setDisconnecting(true)
    try {
      await api.bank.disconnect()
      setConnected(false); setAccounts([]); setTransactions([])
      setPendingReviews([]); setCCMismatches([]); setPaidInvoices([])
      setSyncResult(null); setShowDisconnect(false)
    } finally { setDisconnecting(false) }
  }

  const handleConfirm = async (txnId: number, type: string, sub: string | null) => {
    const res: any = await api.bank.confirm(txnId, type, sub)
    if (res?.review_status === 'confirmed') {
      setPendingReviews(prev => prev.filter(t => t.id !== txnId))
      await loadTransactions()
    }
  }

  const handleSkip = async (txnId: number) => {
    await api.bank.skip(txnId)
    setPendingReviews(prev => prev.filter(t => t.id !== txnId))
  }

  if (loading) return <div className="p-6 text-gray-500">Loading bank status...</div>

  // ── Not connected ─────────────────────────────────────────────────────────

  if (!connected && !showPlaid && !exchanging) {
    return (
      <div className="p-6 max-w-2xl mx-auto space-y-5">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Bank Account</h1>
          <p className="text-gray-500 text-sm mt-1">
            Optional — connect your business checking account to automate bookkeeping.
          </p>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg px-4 py-3 text-sm">{error}</div>
        )}

        <div className="flex items-center gap-3 bg-blue-50 border border-blue-200 rounded-xl px-4 py-3">
          <span className="text-blue-600 text-lg">🔒</span>
          <div>
            <p className="text-blue-800 font-semibold text-sm">Read-only access</p>
            <p className="text-blue-600 text-xs mt-0.5">We can never move money, initiate transfers, or change anything at your bank.</p>
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-4">
          <p className="text-gray-800 font-semibold text-sm">What we access and why</p>
          {DATA_ITEMS.map((item, i) => (
            <div key={i} className="flex gap-3">
              <span className="text-xl shrink-0 mt-0.5">{item.icon}</span>
              <div>
                <p className="text-gray-700 text-sm font-medium">{item.what}</p>
                <p className="text-gray-500 text-xs mt-0.5">{item.why}</p>
              </div>
            </div>
          ))}
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-2">
          <p className="text-gray-800 font-semibold text-sm">We never access</p>
          {NEVER_ITEMS.map((item, i) => (
            <div key={i} className="flex items-center gap-2 text-gray-500 text-sm">
              <span className="text-red-400 text-xs font-bold">✕</span>{item}
            </div>
          ))}
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-5 space-y-2">
          <p className="text-gray-800 font-semibold text-sm">How it works</p>
          {[
            'Click Connect — a secure Plaid popup opens (not our app)',
            'Log in to your bank directly on Plaid\'s servers — we never see your password',
            'Plaid gives us a read-only token stored in your private database',
            'You can revoke access anytime by clicking Disconnect',
          ].map((step, i) => (
            <div key={i} className="flex gap-3 text-sm text-gray-500">
              <span className="text-gray-400 font-mono shrink-0">{i + 1}.</span>{step}
            </div>
          ))}
        </div>

        <p className="text-center text-gray-400 text-xs">
          This is entirely optional. All other features work without a bank connection.
        </p>

        <button
          onClick={() => setShowPlaid(true)}
          className="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-xl font-semibold transition-colors"
        >
          Connect Bank Account (Read-Only)
        </button>
      </div>
    )
  }

  if (showPlaid) {
    return (
      <div className="p-6 max-w-md mx-auto space-y-4 mt-10">
        <div className="text-center space-y-2">
          <div className="text-4xl">🔒</div>
          <h2 className="text-gray-900 font-semibold text-lg">Read-only connection</h2>
          <p className="text-gray-500 text-sm">You'll log in to your bank directly through Plaid's secure popup. We never see your username or password.</p>
        </div>
        <PlaidButton onSuccess={handlePlaidSuccess} onExit={() => setShowPlaid(false)} />
        <button onClick={() => setShowPlaid(false)} className="w-full py-2 text-gray-400 text-sm hover:text-gray-600 transition-colors">
          Cancel — keep bank disconnected
        </button>
      </div>
    )
  }

  if (exchanging) {
    return <div className="p-6 text-center text-gray-500 mt-20">Connecting your bank...</div>
  }

  // ── Connected ─────────────────────────────────────────────────────────────

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-5">

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Bank Account</h1>
          <div className="flex items-center gap-2 mt-1">
            <span className="w-2 h-2 bg-green-500 rounded-full" />
            <span className="text-green-700 text-sm font-medium">Connected</span>
            <span className="text-gray-300 text-sm">·</span>
            <span className="text-gray-500 text-xs">🔒 Read-only — we cannot move money</span>
          </div>
        </div>
        <button
          onClick={() => setShowDisconnect(true)}
          className="shrink-0 px-3 py-1.5 bg-white hover:bg-red-50 border border-gray-200 hover:border-red-200 text-gray-500 hover:text-red-600 rounded-lg text-xs font-medium transition-colors"
        >
          Disconnect / Revoke
        </button>
      </div>

      {/* Disconnect confirmation */}
      {showDisconnect && (
        <div className="bg-white border border-red-200 rounded-xl p-5 space-y-3 shadow-sm">
          <p className="text-gray-900 font-semibold text-sm">Disconnect your bank?</p>
          <p className="text-gray-500 text-sm">
            This immediately revokes our read-only access token. No data is deleted — only the connection is removed. You can reconnect anytime.
          </p>
          <div className="flex gap-3">
            <button onClick={handleDisconnect} disabled={disconnecting}
              className="flex-1 py-2 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white rounded-lg text-sm font-semibold transition-colors">
              {disconnecting ? 'Disconnecting...' : 'Yes, disconnect and revoke access'}
            </button>
            <button onClick={() => setShowDisconnect(false)}
              className="px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-600 rounded-lg text-sm transition-colors">
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg px-4 py-3 text-sm">{error}</div>}

      {syncResult && (
        <div className="bg-blue-50 border border-blue-200 text-blue-700 rounded-lg px-4 py-3 text-sm">
          ✅ Synced — {syncResult.added} new transactions, {syncResult.matched} matched to records.
        </div>
      )}

      {/* Paid invoices */}
      {paidInvoices.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-green-700 font-semibold text-sm">✅ Invoices Confirmed Paid</h2>
          {paidInvoices.map((inv, i) => (
            <div key={i} className="bg-green-50 border border-green-200 rounded-lg px-4 py-2.5 flex items-center justify-between text-sm">
              <div>
                <span className="text-gray-900 font-medium">{inv.vendor}</span>
                <span className="text-gray-500 text-xs ml-2">invoiced {inv.invoice_date} · cleared {inv.bank_date}</span>
              </div>
              <span className="text-green-700 font-semibold">${inv.amount.toLocaleString('en-US', { minimumFractionDigits: 2 })}</span>
            </div>
          ))}
        </div>
      )}

      {/* Accounts */}
      {accounts.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-gray-700 font-semibold text-sm">Accounts</h2>
          {accounts.map(a => (
            <div key={a.account_id} className="bg-white border border-gray-200 rounded-xl p-4 flex items-center justify-between shadow-sm">
              <div>
                <div className="text-gray-900 font-semibold">{a.official_name}</div>
                <div className="text-gray-500 text-sm capitalize">{a.type} · {a.subtype}</div>
              </div>
              <div className="text-right">
                <div className="text-gray-900 font-bold text-lg">${a.current.toLocaleString('en-US', { minimumFractionDigits: 2 })}</div>
                {a.available != null && a.available !== a.current && (
                  <div className="text-gray-400 text-xs">${a.available.toLocaleString('en-US', { minimumFractionDigits: 2 })} available</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Sync button */}
      <button onClick={handleSync} disabled={syncing}
        className="w-full py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg font-semibold transition-colors text-sm">
        {syncing ? 'Syncing...' : '🔄 Sync Transactions'}
      </button>

      {/* CC Mismatches */}
      {ccMismatches.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-yellow-700 font-semibold text-sm">⚠️ CC Settlement Mismatches ({ccMismatches.length})</h2>
          {ccMismatches.map(mm => (
            <div key={mm.bank_txn_id} className="bg-yellow-50 border border-yellow-200 rounded-xl p-4 shadow-sm">
              <div className="flex justify-between items-start">
                <div>
                  <div className="text-gray-900 text-sm font-medium">{mm.bank_desc}</div>
                  <div className="text-gray-500 text-xs mt-1">Bank deposit: ${mm.bank_amount.toFixed(2)} on {mm.bank_date}</div>
                  <div className="text-gray-500 text-xs">Daily card total: ${mm.sale_card.toFixed(2)} for {mm.sale_date}</div>
                </div>
                <div className={`text-sm font-bold ${mm.diff > 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {mm.diff > 0 ? '+' : ''}${mm.diff.toFixed(2)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pending Reviews */}
      {pendingReviews.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-gray-900 font-semibold text-sm flex items-center gap-2">
            ❓ Needs Your Review
            <span className="bg-yellow-500 text-white text-xs px-2 py-0.5 rounded-full">{pendingReviews.length}</span>
          </h2>
          <p className="text-gray-500 text-xs">These couldn't be auto-categorized. Once you classify them I'll remember for next time.</p>
          {pendingReviews.map(txn => (
            <ReviewCard key={txn.id} txn={txn} onConfirm={handleConfirm} onSkip={handleSkip} />
          ))}
        </div>
      )}

      {/* Transactions */}
      {transactions.length > 0 && (
        <div>
          <h2 className="text-gray-700 font-semibold text-sm mb-3">Recent Transactions (30 days)</h2>
          <div className="bg-white border border-gray-200 rounded-xl overflow-hidden shadow-sm">
            {transactions.map((t, i) => (
              <div key={t.id}
                className={`flex items-center justify-between px-4 py-3 text-sm ${i > 0 ? 'border-t border-gray-100' : ''} ${
                  t.is_matched ? 'bg-green-50' : t.review_status === 'needs_review' ? 'bg-yellow-50' : ''
                }`}
              >
                <div className="flex-1 min-w-0">
                  <span className="text-gray-800 truncate block">{t.description}</span>
                  <span className="text-gray-400 text-xs">
                    {t.date}{t.reconcile_type && ` · ${t.reconcile_type}${t.reconcile_subcategory ? ` (${t.reconcile_subcategory})` : ''}`}
                  </span>
                </div>
                <div className="flex items-center gap-3 shrink-0 ml-4">
                  <span className={t.amount > 0 ? 'text-red-600 font-medium' : 'text-green-600 font-medium'}>
                    {t.amount > 0 ? '-' : '+'}${Math.abs(t.amount).toFixed(2)}
                  </span>
                  {t.is_matched
                    ? <span className="text-green-600 text-xs">✓ matched</span>
                    : t.review_status === 'needs_review'
                    ? <span className="text-yellow-600 text-xs">? review</span>
                    : t.review_status === 'auto'
                    ? <span className="text-blue-500 text-xs">auto</span>
                    : <span className="text-gray-400 text-xs">pending</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {transactions.length === 0 && !syncing && (
        <div className="text-center text-gray-400 py-8">No transactions yet — click Sync to pull the latest.</div>
      )}
    </div>
  )
}
