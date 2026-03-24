import Cookies from 'js-cookie'

const API = '/api'

export function getToken(): string | undefined {
  return Cookies.get('token')
}

export function setToken(token: string) {
  // 30 day expiry, auto-login
  Cookies.set('token', token, { expires: 30, sameSite: 'strict' })
}

export function clearToken() {
  Cookies.remove('token')
}

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
    window.location.href = '/login'
    return null
  }
  return res.json()
}

export const api = {
  login: (username: string, password: string) =>
    request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  me: () => request('/auth/me'),

  sales: (days = 7) => request(`/sales?days=${days}`),

  health: () => request('/health'),

  prices: (q = '') => request(`/prices?q=${encodeURIComponent(q)}`),

  order: (items: { item: string; qty: number }[]) =>
    request('/order', {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),

  settings: () => request('/settings'),
}
