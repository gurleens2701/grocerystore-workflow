'use client'
import { useEffect, useRef, useState } from 'react'
import AuthGuard from '@/components/AuthGuard'
import { api } from '@/lib/api'
import Cookies from 'js-cookie'

type Message = {
  id: number
  source: 'telegram' | 'web'
  role: 'user' | 'bot'
  sender_name: string
  content: string
  created_at: string
}


function ChatBubble({ msg }: { msg: Message }) {
  const isBot = msg.role === 'bot'
  return (
    <div className={`flex gap-2 ${isBot ? 'justify-start' : 'justify-end'}`}>
      {isBot && (
        <div className="w-8 h-8 rounded-full bg-gray-200 flex items-center justify-center text-sm flex-shrink-0">
          🤖
        </div>
      )}
      <div className={`max-w-[75%] ${isBot ? '' : 'items-end flex flex-col'}`}>
        <div className="flex items-center gap-1 mb-0.5">
          <span className="text-xs text-gray-400">{msg.sender_name}</span>
          <span className="text-xs text-gray-300">
            {new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        </div>
        <div className={`px-3 py-2 rounded-2xl text-sm whitespace-pre-wrap break-words ${
          isBot
            ? 'bg-white border text-gray-800 rounded-tl-sm'
            : 'bg-blue-600 text-white rounded-tr-sm'
        }`}>
          {msg.content}
        </div>
      </div>
      {!isBot && (
        <div className="w-8 h-8 rounded-full bg-blue-600 flex items-center justify-center text-white text-xs flex-shrink-0">
          {msg.sender_name.charAt(0).toUpperCase()}
        </div>
      )}
    </div>
  )
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [senderName, setSenderName] = useState('Employee')
  const [lastTs, setLastTs] = useState<string | null>(null)
  const [invoiceResult, setInvoiceResult] = useState<{vendor: string; total: number | null; items: any[]; summary: string} | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // Load saved name from cookie (client-only)
  useEffect(() => {
    const saved = Cookies.get('chat_name')
    if (saved) setSenderName(saved)
  }, [])

  // Initial load
  useEffect(() => {
    api.messages(100).then(data => {
      if (data) {
        setMessages(data)
        if (data.length) setLastTs(data[data.length - 1].created_at)
      }
    })
  }, [])

  // Poll for new messages every 3s
  useEffect(() => {
    const interval = setInterval(() => {
      if (!lastTs) return
      api.messages(50, lastTs).then(data => {
        if (data && data.length > 0) {
          setMessages(prev => [...prev, ...data])
          setLastTs(data[data.length - 1].created_at)
        }
      })
    }, 3000)
    return () => clearInterval(interval)
  }, [lastTs])

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function send() {
    if (!input.trim() || sending) return
    setSending(true)
    const text = input.trim()
    setInput('')

    // Optimistically add user message
    const optimistic: Message = {
      id: Date.now(),
      source: 'web',
      role: 'user',
      sender_name: senderName,
      content: text,
      created_at: new Date().toISOString(),
    }
    setMessages(prev => [...prev, optimistic])

    const res = await api.chat(text, senderName)
    setSending(false)

    if (res?.reply) {
      const botMsg: Message = {
        id: Date.now() + 1,
        source: 'web',
        role: 'bot',
        sender_name: 'Bot',
        content: res.reply,
        created_at: new Date().toISOString(),
      }
      setMessages(prev => [...prev, botMsg])
      setLastTs(botMsg.created_at)
    }
  }

  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setSending(true)
    setInvoiceResult(null)

    const uploadMsg: Message = {
      id: Date.now(),
      source: 'web',
      role: 'user',
      sender_name: senderName,
      content: `📎 Uploading ${file.name}...`,
      created_at: new Date().toISOString(),
    }
    setMessages(prev => [...prev, uploadMsg])

    let res: any = null
    try {
      res = await api.chatInvoice(file, senderName)
    } catch (err) {
      res = { error: `Upload failed: ${err}` }
    }
    setSending(false)
    e.target.value = ''

    const botContent = res?.error || res?.detail
      ? (res.error || `Server error: ${res.detail}`)
      : res?.items
        ? null
        : 'No response from server — check your connection.'

    if (res?.items) {
      setInvoiceResult(res)
      setMessages(prev => [...prev, {
        id: Date.now(), source: 'web', role: 'bot', sender_name: 'Bot',
        content: res.summary, created_at: new Date().toISOString(),
      }])
    } else {
      setMessages(prev => [...prev, {
        id: Date.now(), source: 'web', role: 'bot', sender_name: 'Bot',
        content: botContent || '⚠️ Invoice upload failed.',
        created_at: new Date().toISOString(),
      }])
    }
  }

  function saveName(name: string) {
    setSenderName(name)
    Cookies.set('chat_name', name, { expires: 365 })
  }

  return (
    <AuthGuard>
      <div className="flex flex-col h-screen">

        {/* Header */}
        <div className="px-6 py-4 border-b bg-white flex items-center justify-between">
          <div>
            <h1 className="text-lg font-bold">Store Chat</h1>
            <p className="text-xs text-gray-400">Shared with owner via Telegram</p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Your name:</span>
            <input
              value={senderName}
              onChange={e => saveName(e.target.value)}
              className="border border-gray-300 rounded px-2 py-1 text-sm w-28"
              placeholder="Your name"
            />
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4 bg-gray-50">
          {messages.length === 0 && (
            <div className="text-center text-gray-400 text-sm mt-20">
              No messages yet. Start the conversation or send an invoice photo.
            </div>
          )}
          {messages.map(m => <ChatBubble key={m.id} msg={m} />)}
          <div ref={bottomRef} />
        </div>

        {/* Invoice preview */}
        {invoiceResult && (
          <div className="px-6 py-3 bg-yellow-50 border-t border-yellow-200">
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium text-yellow-800">
                Invoice extracted — {invoiceResult.items.length} items from {invoiceResult.vendor}
              </span>
              <button onClick={() => setInvoiceResult(null)} className="text-yellow-600 text-xs">Dismiss</button>
            </div>
            <div className="text-xs text-yellow-700 max-h-24 overflow-y-auto space-y-0.5">
              {invoiceResult.items.slice(0, 8).map((item: any, i: number) => (
                <div key={i}>{item.item_name || item.name} — ${item.unit_price?.toFixed(2)}</div>
              ))}
              {invoiceResult.items.length > 8 && <div>…and {invoiceResult.items.length - 8} more</div>}
            </div>
          </div>
        )}

        {/* Input bar */}
        <div className="px-4 py-3 bg-white border-t flex items-center gap-2">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleFile}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={sending}
            className="p-2 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition disabled:opacity-40"
            title="Upload invoice photo"
          >
            📎
          </button>
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
            placeholder="Type /price marlboro, /order ..., or ask a question"
            className="flex-1 border border-gray-300 rounded-xl px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            disabled={sending}
          />
          <button
            onClick={send}
            disabled={sending || !input.trim()}
            className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-xl text-sm font-medium transition disabled:opacity-40"
          >
            {sending ? '...' : 'Send'}
          </button>
        </div>

      </div>
    </AuthGuard>
  )
}
