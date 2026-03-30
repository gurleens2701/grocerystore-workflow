'use client'
import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { getToken } from '@/lib/api'

const FEATURES = [
  {
    icon: '📊',
    title: 'Daily Report — Automated',
    desc: 'Every morning your sales, lotto, tax, and GPI are pulled from NRS automatically and sent to your Telegram. No more manual entry.',
  },
  {
    icon: '📱',
    title: 'Run Everything From Telegram',
    desc: 'Check sales, log invoices, look up prices, ask questions — all by texting your bot. Works on any phone.',
  },
  {
    icon: '🗂️',
    title: 'Full Ledger — Invoices, Expenses, Payroll',
    desc: 'Log vendor invoices by photo. Track expenses and payroll. Everything goes to your Google Sheet automatically.',
  },
  {
    icon: '🏦',
    title: 'Bank Reconciliation',
    desc: 'Connect your bank account read-only. The system matches deposits to invoices, flags CC settlement mismatches, and learns your patterns.',
  },
  {
    icon: '💪',
    title: 'Weekly Health Score',
    desc: 'Every week get a score and breakdown — sales trend, shrink, over/short, top departments. Know how your store is doing at a glance.',
  },
  {
    icon: '📦',
    title: 'Order Builder',
    desc: 'Tell the bot what you need. It compares vendor prices and builds your order list automatically.',
  },
]

const STEPS = [
  {
    n: '1',
    title: 'You give us your store info',
    desc: 'Store name, your NRS credentials, a Telegram bot token, and a Google Sheet. Takes 5 minutes.',
  },
  {
    n: '2',
    title: 'We set it up on our server',
    desc: 'Your store gets its own private database, dashboard login, and Telegram bot. Nothing shared with other stores.',
  },
  {
    n: '3',
    title: 'Wake up to your daily report',
    desc: 'Every morning at 7AM your sales data is pulled automatically and sent to your phone. Done.',
  },
]

export default function LandingPage() {
  const router = useRouter()
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    if (getToken()) {
      router.replace('/dashboard')
    } else {
      setChecked(true)
    }
  }, [router])

  if (!checked) return null

  return (
    <div className="min-h-screen bg-gray-950 text-white">

      {/* Nav */}
      <nav className="flex items-center justify-between px-6 py-4 border-b border-gray-800 max-w-6xl mx-auto">
        <div className="flex items-center gap-2">
          <span className="text-2xl">⛽</span>
          <span className="font-bold text-lg tracking-tight">StoreAgent</span>
        </div>
        <a
          href="/login"
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm font-medium transition-colors"
        >
          Sign In
        </a>
      </nav>

      {/* Hero */}
      <section className="px-6 pt-20 pb-16 text-center max-w-3xl mx-auto">
        <div className="inline-block bg-blue-900/40 border border-blue-700/50 text-blue-300 text-xs font-medium px-3 py-1 rounded-full mb-6">
          Built for gas stations &amp; convenience stores
        </div>
        <h1 className="text-4xl sm:text-5xl font-bold leading-tight mb-6">
          Your store runs itself.<br />
          <span className="text-blue-400">You just check your phone.</span>
        </h1>
        <p className="text-gray-400 text-lg mb-8 leading-relaxed">
          StoreAgent connects to your POS, pulls your daily numbers automatically,
          manages your ledger, tracks invoices, and sends everything to your phone every morning.
          No spreadsheets. No manual entry.
        </p>
        <a
          href="https://t.me/gurleens2701"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block px-8 py-4 bg-blue-600 hover:bg-blue-500 text-white rounded-xl font-semibold text-lg transition-colors"
        >
          Get Started — Message Us on Telegram
        </a>
        <p className="text-gray-600 text-sm mt-4">
          Currently available for NRS Plus stores · More POS systems coming soon
        </p>
      </section>

      {/* Features */}
      <section className="px-6 py-16 max-w-6xl mx-auto">
        <h2 className="text-2xl font-bold text-center mb-10">Everything your store needs</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {FEATURES.map((f, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
              <div className="text-3xl mb-3">{f.icon}</div>
              <div className="font-semibold text-white mb-2">{f.title}</div>
              <div className="text-gray-400 text-sm leading-relaxed">{f.desc}</div>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section className="px-6 py-16 bg-gray-900/50">
        <div className="max-w-3xl mx-auto">
          <h2 className="text-2xl font-bold text-center mb-10">How it works</h2>
          <div className="space-y-6">
            {STEPS.map((s, i) => (
              <div key={i} className="flex gap-5">
                <div className="w-10 h-10 rounded-full bg-blue-600 flex items-center justify-center font-bold text-lg shrink-0">
                  {s.n}
                </div>
                <div>
                  <div className="font-semibold text-white mb-1">{s.title}</div>
                  <div className="text-gray-400 text-sm leading-relaxed">{s.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* What you need */}
      <section className="px-6 py-16 max-w-3xl mx-auto">
        <h2 className="text-2xl font-bold text-center mb-8">What you need to get started</h2>
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 space-y-4">
          {[
            ['📱', 'A Telegram account', 'Free — download from App Store or Google Play'],
            ['🖥️', 'NRS Plus back office access', 'Your NRS username and password'],
            ['📄', 'A Google account', 'We create a Google Sheet for your daily records'],
            ['⏱️', '5 minutes', "That's all the setup takes"],
          ].map(([icon, title, sub], i) => (
            <div key={i} className="flex gap-4 items-start">
              <span className="text-2xl shrink-0">{icon}</span>
              <div>
                <div className="text-white font-medium text-sm">{title}</div>
                <div className="text-gray-500 text-xs mt-0.5">{sub}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="px-6 py-16 text-center bg-blue-950/30 border-t border-blue-900/30">
        <h2 className="text-2xl font-bold mb-4">Ready to automate your store?</h2>
        <p className="text-gray-400 mb-8">
          Message us on Telegram and we will have your store running in under an hour.
        </p>
        <a
          href="https://t.me/gurleens2701"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block px-8 py-4 bg-blue-600 hover:bg-blue-500 text-white rounded-xl font-semibold text-lg transition-colors"
        >
          Message Us on Telegram
        </a>
      </section>

      {/* Footer */}
      <footer className="px-6 py-8 border-t border-gray-800 text-center text-gray-600 text-sm">
        <div className="flex items-center justify-center gap-2 mb-2">
          <span>⛽</span>
          <span className="font-semibold text-gray-400">StoreAgent</span>
        </div>
        <p>Built for independent store owners · Your data stays private on your own server</p>
      </footer>

    </div>
  )
}
