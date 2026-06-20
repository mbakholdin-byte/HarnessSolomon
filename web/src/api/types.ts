/**
 * WI-02: Shared API types.
 *
 * ``APIError`` provides structured error handling for non-2xx responses.
 * ``Page<T>`` is the generic paginated-response contract used by list endpoints.
 */

/** Thrown by ``APIClient.request()`` on non-2xx responses (except 401, which
 *  triggers a redirect before throwing). Carries the HTTP status and parsed
 *  response body so callers can inspect backend error details. */
export class APIError extends Error {
  constructor(
    public status: number,
    public body: unknown,
    message: string,
  ) {
    super(message);
    this.name = "APIError";
    // Fix prototype chain for ``instanceof`` checks after TS compilation.
    Object.setPrototypeOf(this, APIError.prototype);
  }
}

/** Generic paginated response envelope.
 *
 *  Matches the backend's ``list`` endpoint shape (e.g.
 *  ``PrivacyZoneListResponse``, ``AuditRecentResponse``). */
export interface Page<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}
