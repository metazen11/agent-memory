/**
 * Shared auth header helper for agent-memory hooks.
 *
 * Reads AGENT_MEMORY_TOKEN from environment and returns
 * an Authorization header object to merge into http requests.
 *
 * Usage:
 *   const { authHeaders } = require('./auth-header');
 *   http.request({ headers: { 'Content-Type': 'application/json', ...authHeaders() } })
 */

function authHeaders() {
  const token = process.env.AGENT_MEMORY_TOKEN;
  if (token) {
    return { Authorization: `Bearer ${token}` };
  }
  return {};
}

module.exports = { authHeaders };
