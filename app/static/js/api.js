(() => {
  "use strict";

  class ApiRequestError extends Error {
    constructor(message, status, code) {
      super(message);
      this.name = "ApiRequestError";
      this.status = status;
      this.code = code;
    }
  }

  async function getJson(path, { signal } = {}) {
    let response;
    try {
      response = await fetch(path, {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal,
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new ApiRequestError("The service could not be reached. Check the connection and try again.", 0, "network_error");
    }

    let body;
    try {
      body = await response.json();
    } catch {
      throw new ApiRequestError("The service returned an unreadable response. Try again later.", response.status, "invalid_response");
    }

    if (!response.ok) {
      const message = body && body.error && typeof body.error.message === "string"
        ? body.error.message
        : "The service could not complete the request. Try again later.";
      const safeMessages = {
        database_unavailable: "Transaction data is temporarily unavailable. Try again later.",
        analytics_unavailable: "Analytics are unavailable for the current currency data.",
        invalid_query_parameter: "One or more filters are invalid. Check the selected values.",
      };
      throw new ApiRequestError(safeMessages[body?.error?.code] || message, response.status, body?.error?.code || "request_error");
    }
    return body;
  }

  function toExclusiveEnd(inclusiveDate) {
    if (!inclusiveDate) return "";
    const midnightUtc = new Date(`${inclusiveDate}T00:00:00Z`);
    if (Number.isNaN(midnightUtc.getTime())) return "";
    midnightUtc.setUTCDate(midnightUtc.getUTCDate() + 1);
    return midnightUtc.toISOString().slice(0, 10);
  }

  function dateRangeQuery(startDate, throughDate) {
    return new URLSearchParams({
      start_date: startDate,
      end_date: toExclusiveEnd(throughDate),
    }).toString();
  }

  function buildTransactionQuery(filters, page) {
    const params = new URLSearchParams({ page: String(page), page_size: String(filters.pageSize) });
    if (filters.status) params.set("status", filters.status);
    if (filters.type) params.set("transaction_type", filters.type);
    if (filters.channel) params.set("channel", filters.channel);
    if (filters.startDate) params.set("start_date", filters.startDate);
    if (filters.endDate) params.set("end_date", toExclusiveEnd(filters.endDate));
    return params.toString();
  }

  function buildAccountTransactionQuery(accountNumber, filters, page) {
    const params = new URLSearchParams(buildTransactionQuery(filters, page));
    params.set("account_number", accountNumber);
    return params.toString();
  }

  window.TransactionApi = Object.freeze({
    ApiRequestError,
    getSummary: (options) => getJson("/api/analytics/summary", options),
    getAnalysis: (startDate, throughDate, options) =>
      getJson(`/api/analytics/volume?${dateRangeQuery(startDate, throughDate)}`, options),
    getTransactions: (filters, page, options) =>
      getJson(`/api/transactions?${buildTransactionQuery(filters, page)}`, options),
    getAccountTransactions: (accountNumber, filters, page, options) =>
      getJson(`/api/accounts/transactions?${buildAccountTransactionQuery(accountNumber, filters, page)}`, options),
    toExclusiveEnd,
  });
})();
