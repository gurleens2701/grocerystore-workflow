import Cookies from 'js-cookie'

const API = '/api'

// ── Token ──────────────────────────────────────────────────────────────────

export function getToken(): string | undefined {
  return Cookies.get('token')
}

export function setToken(token: string) {
  Cookies.set('token', token, { expires: 30, sameSite: 'strict' })
}

export function clearToken() {
  Cookies.remove('token')
}

// ── Store management (localStorage) ────────────────────────────────────────

export function getStoreIds(): string[] {
  if (typeof window === 'undefined') return []
  try {
    return JSON.parse(localStorage.getItem('store_ids') || '[]')
  } catch {
    return []
  }
}

export function setStoreIds(ids: string[]) {
  localStorage.setItem('store_ids', JSON.stringify(ids))
}

export function getActiveStore(): string {
  if (typeof window === 'undefined') return ''
  return localStorage.getItem('active_store') || getStoreIds()[0] || ''
}

export function setActiveStore(id: string) {
  localStorage.setItem('active_store', id)
}

export function formatStoreName(id: string): string {
  return id.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

export function clearStoreData() {
  localStorage.removeItem('store_ids')
  localStorage.removeItem('active_store')
}

// ── HTTP client ─────────────────────────────────────────────────────────────

async function request(path: string, options: RequestInit = {}) {
  const token = getToken()
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  })
  if (res.status === 401) {
    clearToken()
    clearStoreData()
    window.location.href = '/login'
    return null
  }
  return res.json()
}

// ── API calls ────────────────────────────────────────────────────────────────

export const api = {
  login: (username: string, password: string) =>
    request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  me: () => request('/auth/me'),

  stores: () => request('/stores'),

  sales: (days = 7, storeId?: string) => {
    const sid = storeId || getActiveStore()
    return request(`/sales?days=${days}${sid ? `&store_id=${sid}` : ''}`)
  },

  health: (storeId?: string) => {
    const sid = storeId || getActiveStore()
    return request(`/health${sid ? `?store_id=${sid}` : ''}`)
  },

  prices: (q = '', storeId?: string) => {
    const sid = storeId || getActiveStore()
    return request(`/prices?q=${encodeURIComponent(q)}${sid ? `&store_id=${sid}` : ''}`)
  },

  order: (items: { item: string; qty: number }[], storeId?: string) => {
    const sid = storeId || getActiveStore()
    return request('/order', {
      method: 'POST',
      body: JSON.stringify({ items, store_id: sid || undefined }),
    })
  },

  settings: (storeId?: string) => {
    const sid = storeId || getActiveStore()
    return request(`/settings${sid ? `?store_id=${sid}` : ''}`)
  },

  messages: (limit = 50, since?: string, storeId?: string) => {
    const sid = storeId || getActiveStore()
    const params = new URLSearchParams({ limit: String(limit) })
    if (since) params.set('since', since)
    if (sid) params.set('store_id', sid)
    return request(`/messages?${params}`)
  },

  chat: (message: string, senderName: string, storeId?: string) => {
    const sid = storeId || getActiveStore()
    return request('/chat', {
      method: 'POST',
      body: JSON.stringify({ message, sender_name: senderName, store_id: sid || undefined }),
    })
  },

  chatInvoice: (file: File, senderName: string, storeId?: string) => {
    const sid = storeId || getActiveStore()
    const form = new FormData()
    form.append('file', file)
    form.append('sender_name', senderName)
    if (sid) form.append('store_id', sid)
    const token = getToken()
    return fetch('/api/chat/invoice', {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    }).then(r => r.json()).catch((err: any) => ({ error: String(err) }))
  },
}
