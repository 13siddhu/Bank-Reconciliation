const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

async function request(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`;
  const headers = { ...options.headers };

  if (!(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }

  const response = await fetch(url, {
    ...options,
    headers,
  });

  const contentType = response.headers.get('content-type');
  const data = (contentType && contentType.includes('application/json'))
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const errorMsg =
      (data && data.error) ||
      (data && data.message) ||
      `Request failed with status ${response.status}`;
    const error = new Error(errorMsg);
    error.status = response.status;
    error.data = data;
    throw error;
  }

  return data;
}

export const api = {
  getOrganizations() {
    return request('/api/organizations/');
  },

  getSummary(orgSlug) {
    return request('/api/dashboard/summary/', {
      headers: { 'X-Organization': orgSlug },
    });
  },

  getTransactions(orgSlug, status = '') {
    const query = status ? `?status=${encodeURIComponent(status)}` : '';
    return request(`/api/transactions/${query}`, {
      headers: { 'X-Organization': orgSlug },
    });
  },

  getLineItems(orgSlug) {
    return request('/api/line-items/', {
      headers: { 'X-Organization': orgSlug },
    });
  },

  uploadStatement(orgSlug, file) {
    const formData = new FormData();
    formData.append('file', file);
    return request('/api/transactions/upload/', {
      method: 'POST',
      headers: { 'X-Organization': orgSlug },
      body: formData,
    });
  },

  approveMatch(orgSlug, matchId) {
    return request(`/api/matches/${matchId}/approve/`, {
      method: 'POST',
      headers: { 'X-Organization': orgSlug },
    });
  },

  rejectMatch(orgSlug, matchId) {
    return request(`/api/matches/${matchId}/reject/`, {
      method: 'POST',
      headers: { 'X-Organization': orgSlug },
    });
  },

  ignoreTransaction(orgSlug, txId) {
    return request(`/api/transactions/${txId}/ignore/`, {
      method: 'POST',
      headers: { 'X-Organization': orgSlug },
    });
  },
};
