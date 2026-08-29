export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message)
  }
}

async function requestWithHeaders<T>(path: string, init?: RequestInit): Promise<{ data: T; headers: Headers }> {
  const headers = new Headers(init?.headers)
  if (init?.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  const response = await fetch(path, { ...init, headers, credentials: 'include' })
  if (!response.ok) {
    let message = `請求失敗 (${response.status})`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') message = body.detail
      else if (body.detail && typeof body.detail.message === 'string') message = body.detail.message
    } catch {
      // Keep the HTTP fallback when the server did not return JSON.
    }
    throw new ApiError(message, response.status)
  }
  if (response.status === 204) return { data: undefined as T, headers: response.headers }
  return { data: await response.json() as T, headers: response.headers }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return (await requestWithHeaders<T>(path, init)).data
}

export const api = {
  get<T>(path: string) {
    return request<T>(path)
  },
  async getPage<T>(path: string) {
    const result = await requestWithHeaders<T>(path)
    return {
      data: result.data,
      total: Number(result.headers.get('X-Total-Count') || 0),
    }
  },
  post<T>(path: string, body?: unknown) {
    return request<T>(path, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  },
  patch<T>(path: string, body: unknown) {
    return request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
  },
  delete(path: string) {
    return request<void>(path, { method: 'DELETE' })
  },
}
