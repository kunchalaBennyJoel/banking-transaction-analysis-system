(() => {
  "use strict";

  const DEFAULT_START = "2025-01-01";
  const DEFAULT_END = "2025-01-31";
  const MAX_DEMO_DATE = "2026-10-18";
  const STATUS_LABELS = { successful: "Successful", pending: "Pending", failed: "Failed" };
  const TYPE_LABELS = { deposit: "Deposit", withdrawal: "Withdrawal", transfer: "Transfer", payment: "Payment" };
  const CHANNEL_LABELS = { UPI: "UPI", ATM: "ATM", BRANCH: "Branch", UNKNOWN: "Not recorded" };
  const STATUS_COLORS = { successful: "#347b74", pending: "#b28b4d", failed: "#a04b45" };
  const TYPE_COLORS = ["#244459", "#347b74", "#718b8c", "#a28b67"];
  const numberFormat = new Intl.NumberFormat(undefined);
  const moneyFormat = new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const charts = new Map();
  const requestVersions = new Map();
  const activeRequests = new Map();
  let activeAccountNumber = null;
  const $ = (id) => document.getElementById(id);

  function formatMoney(decimalValue) {
    if (decimalValue === null || decimalValue === undefined) return "—";
    const amount = Number(decimalValue);
    if (!Number.isFinite(amount)) return `INR ${decimalValue}`;
    return moneyFormat.format(amount);
  }

  function displayDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
      timeZone: "UTC", timeZoneName: "short",
    }).format(date);
  }

  function formatDateRange(startDate, throughDate) {
    return `${startDate} through ${throughDate} (UTC, inclusive dates)`;
  }

  function friendlyError(error) {
    if (error instanceof window.TransactionApi.ApiRequestError) return error.message;
    return "Something went wrong while loading this section. Try again.";
  }

  function destroyChart(canvasId) {
    const chart = charts.get(canvasId);
    if (chart) chart.destroy();
    charts.delete(canvasId);
  }

  function beginRequest(key) {
    activeRequests.get(key)?.abort();
    const controller = new AbortController();
    activeRequests.set(key, controller);
    const version = (requestVersions.get(key) || 0) + 1;
    requestVersions.set(key, version);
    return { controller, version };
  }

  function isCurrentRequest(key, version) {
    return requestVersions.get(key) === version;
  }

  function clearChartState(prefix, kind, message) {
    const canvasId = `${prefix}-${kind}-chart`;
    destroyChart(canvasId);
    $(canvasId).parentElement.hidden = true;
    const empty = $(`${prefix}-${kind}-empty`);
    const error = $(`${prefix}-${kind}-error`);
    empty.textContent = message;
    empty.hidden = false;
    error.hidden = true;
    $(`${prefix}-${kind}-description`).textContent = "";
  }

  function showChartError(prefix, kind, message) {
    destroyChart(`${prefix}-${kind}-chart`);
    $(`${prefix}-${kind}-chart`).parentElement.hidden = true;
    $(`${prefix}-${kind}-empty`).hidden = true;
    const error = $(`${prefix}-${kind}-error`);
    error.textContent = message;
    error.hidden = false;
  }

  function renderCountChart(prefix, kind, values, labels, colors) {
    const canvasId = `${prefix}-${kind}-chart`;
    const canvas = $(canvasId);
    const empty = $(`${prefix}-${kind}-empty`);
    const error = $(`${prefix}-${kind}-error`);
    destroyChart(canvasId);
    error.hidden = true;
    const entries = Object.entries(values || {}).filter(([, count]) => Number.isFinite(count) && count > 0);
    if (!entries.length) {
      canvas.parentElement.hidden = true;
      empty.textContent = "No transaction events are available for this chart.";
      empty.hidden = false;
      $(`${prefix}-${kind}-description`).textContent = empty.textContent;
      return;
    }
    if (!window.Chart) {
      showChartError(prefix, kind, "The chart library could not be loaded. The underlying metrics remain available.");
      return;
    }
    empty.hidden = true;
    canvas.parentElement.hidden = false;
    const names = entries.map(([key]) => labels[key] || key);
    const counts = entries.map(([, count]) => count);
    $(`${prefix}-${kind}-description`).textContent =
      `${names.map((name, index) => `${name}: ${numberFormat.format(counts[index])}`).join("; ")}.`;
    charts.set(canvasId, new window.Chart(canvas, {
      type: "bar",
      data: {
        labels: names,
        datasets: [{
          label: "Transaction count",
          data: counts,
          backgroundColor: entries.map((_, index) => colors[index % colors.length]),
          borderRadius: 2,
          maxBarThickness: 34,
        }],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (context) => `Transaction count: ${numberFormat.format(context.parsed.x)}` } },
        },
        scales: {
          x: {
            beginAtZero: true,
            ticks: { precision: 0, color: "#53616a" },
            grid: { color: "#e5e8e3" },
            title: { display: true, text: "Transaction count", color: "#53616a" },
          },
          y: { grid: { display: false }, ticks: { color: "#26343c" } },
        },
      },
    }));
  }

  function renderDailyChart(prefix, dailyVolume, emptyMessage) {
    const canvasId = `${prefix}-volume-chart`;
    const canvas = $(canvasId);
    const empty = $(`${prefix}-volume-empty`);
    const error = $(`${prefix}-volume-error`);
    destroyChart(canvasId);
    error.hidden = true;
    const days = dailyVolume || [];
    const total = days.reduce((sum, day) => sum + day.transaction_count, 0);
    if (!days.length || total === 0) {
      canvas.parentElement.hidden = true;
      empty.textContent = emptyMessage;
      empty.hidden = false;
      $(`${prefix}-volume-description`).textContent = emptyMessage;
      return;
    }
    if (!window.Chart) {
      showChartError(prefix, "volume", "The chart library could not be loaded. The underlying metrics remain available.");
      return;
    }
    empty.hidden = true;
    canvas.parentElement.hidden = false;
    const labels = days.map((day) => {
      const [year, month, date] = day.date.split("-").map(Number);
      return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" })
        .format(new Date(Date.UTC(year, month - 1, date)));
    });
    const counts = days.map((day) => day.transaction_count);
    $(`${prefix}-volume-description`).textContent =
      `Daily transaction counts: ${days.map((day) => `${day.date}: ${numberFormat.format(day.transaction_count)}`).join("; ")}.`;
    charts.set(canvasId, new window.Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Transactions",
          data: counts,
          borderColor: "#347b74",
          backgroundColor: "rgba(52, 123, 116, 0.10)",
          borderWidth: 2,
          fill: true,
          tension: 0.22,
          pointRadius: 3,
          pointHoverRadius: 5,
          pointBackgroundColor: "#347b74",
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => days[items[0].dataIndex]?.date || "",
              label: (context) => `Transactions: ${numberFormat.format(context.parsed.y)}`,
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: "#53616a", maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
            title: { display: true, text: "Date (UTC)", color: "#53616a" },
          },
          y: {
            beginAtZero: true,
            ticks: { precision: 0, color: "#53616a" },
            grid: { color: "#e5e8e3" },
            title: { display: true, text: "Transaction count", color: "#53616a" },
          },
        },
      },
    }));
  }

  function renderOverview(summary) {
    $("overview-status").textContent = "";
    $("overview-error").hidden = true;
    $("overview-metrics").hidden = false;
    $("overview-metrics").setAttribute("aria-busy", "false");
    $("metric-total").textContent = numberFormat.format(summary.total_transaction_count);
    $("metric-successful-count").textContent = numberFormat.format(summary.successful_transaction_count);
    $("metric-successful-value").textContent = formatMoney(summary.successful_transaction_value);
    $("metric-average").textContent = formatMoney(summary.average_successful_transaction_amount);
    $("metric-currency").textContent = "INR · successful transactions only";
    renderDailyChart("overview", summary.daily_volume, "No transaction events are available through October 18, 2026.");
    renderCountChart("overview", "status", summary.counts_by_status, STATUS_LABELS,
      Object.keys(summary.counts_by_status).map((key) => STATUS_COLORS[key] || "#718087"));
    renderCountChart("overview", "type", summary.counts_by_type, TYPE_LABELS, TYPE_COLORS);
  }

  async function loadOverview() {
    try {
      const response = await window.TransactionApi.getSummary();
      renderOverview(response.data);
    } catch (error) {
      const message = friendlyError(error);
      $("overview-status").textContent = "";
      $("overview-error").textContent = message;
      $("overview-error").hidden = false;
      $("overview-metrics").hidden = true;
      $("overview-metrics").setAttribute("aria-busy", "false");
      for (const kind of ["volume", "status", "type"]) showChartError("overview", kind, message);
    }
  }

  function readTransactionFilters() {
    return {
      status: $("filter-status").value,
      type: $("filter-type").value,
      channel: $("filter-channel").value,
      startDate: $("filter-start").value,
      endDate: $("filter-end").value,
      pageSize: Number($("filter-page-size").value),
    };
  }

  function validateTransactionDates(filters) {
    if (Boolean(filters.startDate) !== Boolean(filters.endDate)) {
      return "Choose both dates, or clear both dates.";
    }
    if (filters.startDate && filters.endDate && filters.startDate > filters.endDate) {
      return "The start date must be on or before the through date.";
    }
    if (filters.startDate > MAX_DEMO_DATE || filters.endDate > MAX_DEMO_DATE) {
      return `Choose dates on or before ${MAX_DEMO_DATE} (UTC).`;
    }
    return "";
  }

  function setActiveFilterText(filters) {
    const parts = [];
    if (filters.status) parts.push(`Status: ${STATUS_LABELS[filters.status] || filters.status}`);
    if (filters.type) parts.push(`Type: ${TYPE_LABELS[filters.type] || filters.type}`);
    if (filters.channel) parts.push(`Channel: ${CHANNEL_LABELS[filters.channel] || filters.channel}`);
    if (filters.startDate && filters.endDate) parts.push(formatDateRange(filters.startDate, filters.endDate));
    else parts.push("All dates");
    if (activeAccountNumber) parts.push("Account search active");
    parts.push(`${filters.pageSize} rows per page`);
    $("active-filters").textContent = `Active filters · ${parts.join(" · ")}`;
    document.querySelectorAll(".transaction-export").forEach((link) => {
      const params = new URLSearchParams({ format: link.dataset.exportFormat });
      if (filters.status) params.set("status", filters.status);
      if (filters.type) params.set("transaction_type", filters.type);
      if (filters.channel) params.set("channel", filters.channel);
      if (filters.startDate) params.set("start_date", filters.startDate);
      if (filters.endDate) params.set("end_date", window.TransactionApi.toExclusiveEnd(filters.endDate));
      if (activeAccountNumber) params.set("account_number", activeAccountNumber);
      link.href = `/api/transactions/export?${params.toString()}`;
    });
  }

  function makeCell(text, className) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = text;
    return cell;
  }

  function renderTransactions(response, page) {
    const tbody = $("transactions-body");
    tbody.replaceChildren();
    const accountResult = $("account-result");
    if (response.account) {
      accountResult.hidden = false;
      $("account-result-label").textContent =
        `Account ${response.account.account_number_masked} · totals reflect the current filters and successful events only.`;
      $("account-transaction-count").textContent = numberFormat.format(response.summary.transaction_count);
      $("account-debit-total").textContent = formatMoney(response.summary.successful_debit_total);
      $("account-credit-total").textContent = formatMoney(response.summary.successful_credit_total);
      $("transactions-empty").textContent = "This account has no transactions matching the current filters.";
    } else {
      accountResult.hidden = true;
      $("transactions-empty").textContent = "No transactions match these filters.";
    }
    for (const row of response.data) {
      const tr = document.createElement("tr");
      tr.append(
        makeCell(row.reference),
        makeCell(displayDate(row.occurred_at)),
        makeCell(TYPE_LABELS[row.transaction_type] || row.transaction_type),
        makeCell(CHANNEL_LABELS[row.channel] || "Not recorded"),
        makeCell(`From ${row.source_account_id ?? "—"} · To ${row.destination_account_id ?? "—"}`, "account-pair"),
      );
      const statusCell = document.createElement("td");
      const status = document.createElement("span");
      status.className = `status-label status-${row.status}`;
      status.textContent = STATUS_LABELS[row.status] || row.status;
      statusCell.append(status);
      tr.append(statusCell, makeCell(formatMoney(row.amount), "amount-column"));
      tbody.append(tr);
    }
    const pagination = response.pagination;
    $("transactions-status").textContent = "";
    $("transactions-error").hidden = true;
    $("transactions-empty").hidden = response.data.length !== 0;
    $("pagination-summary").textContent = pagination.total_records === 0
      ? "0 transactions"
      : `${numberFormat.format(pagination.total_records)} transactions`;
    $("pagination-page").textContent = `Page ${pagination.page}${pagination.total_pages ? ` of ${pagination.total_pages}` : ""}`;
    $("previous-page").disabled = !pagination.has_previous;
    $("next-page").disabled = !pagination.has_next;
    document.querySelector(".table-scroll").setAttribute("aria-busy", "false");
    return page;
  }

  async function loadTransactions(filters, page) {
    const { controller, version } = beginRequest("transactions");
    $("transactions-body").replaceChildren();
    $("transactions-empty").hidden = true;
    $("transactions-error").hidden = true;
    $("account-result").hidden = true;
    $("transactions-status").textContent = activeAccountNumber
      ? "Searching account transactions…"
      : "Loading transactions…";
    $("pagination-summary").textContent = "Loading transactions…";
    $("pagination-page").textContent = `Page ${page}`;
    $("previous-page").disabled = true;
    $("next-page").disabled = true;
    document.querySelector(".table-scroll").setAttribute("aria-busy", "true");
    const invalid = validateTransactionDates(filters);
    if (invalid) {
      controller.abort();
      $("transactions-status").textContent = "";
      $("transactions-error").textContent = invalid;
      $("transactions-error").hidden = false;
      $("pagination-summary").textContent = "Filters need attention";
      document.querySelector(".table-scroll").setAttribute("aria-busy", "false");
      return;
    }
    try {
      const response = activeAccountNumber
        ? await window.TransactionApi.getAccountTransactions(activeAccountNumber, filters, page, { signal: controller.signal })
        : await window.TransactionApi.getTransactions(filters, page, { signal: controller.signal });
      if (isCurrentRequest("transactions", version)) renderTransactions(response, page);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrentRequest("transactions", version)) return;
      $("transactions-status").textContent = "";
      $("transactions-error").textContent = friendlyError(error);
      $("transactions-error").hidden = false;
      $("pagination-summary").textContent = "Transaction count unavailable";
      $("pagination-page").textContent = `Page ${page}`;
      document.querySelector(".table-scroll").setAttribute("aria-busy", "false");
    }
  }

  function initializeTransactions() {
    const filters = readTransactionFilters();
    let page = 1;
    setActiveFilterText(filters);
    $("account-search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const accountNumber = $("account-number").value.trim().toUpperCase();
      if (!accountNumber) {
        $("account-number").focus();
        return;
      }
      $("account-number").value = accountNumber;
      activeAccountNumber = accountNumber;
      $("clear-account-search").hidden = false;
      page = 1;
      const nextFilters = readTransactionFilters();
      setActiveFilterText(nextFilters);
      loadTransactions(nextFilters, page);
    });
    $("clear-account-search").addEventListener("click", () => {
      activeAccountNumber = null;
      $("account-number").value = "";
      $("clear-account-search").hidden = true;
      page = 1;
      const currentFilters = readTransactionFilters();
      setActiveFilterText(currentFilters);
      loadTransactions(currentFilters, page);
    });
    $("filters-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const nextFilters = readTransactionFilters();
      page = 1;
      setActiveFilterText(nextFilters);
      loadTransactions(nextFilters, page);
    });
    $("reset-filters").addEventListener("click", () => {
      $("filters-form").reset();
      $("account-number").value = "";
      activeAccountNumber = null;
      $("clear-account-search").hidden = true;
      $("filter-start").value = DEFAULT_START;
      $("filter-end").value = DEFAULT_END;
      $("filter-page-size").value = "25";
      page = 1;
      const defaults = readTransactionFilters();
      setActiveFilterText(defaults);
      loadTransactions(defaults, page);
    });
    $("previous-page").addEventListener("click", () => {
      if (page > 1) {
        page -= 1;
        loadTransactions(readTransactionFilters(), page);
      }
    });
    $("next-page").addEventListener("click", () => {
      if (!$("next-page").disabled) {
        page += 1;
        loadTransactions(readTransactionFilters(), page);
      }
    });
    loadTransactions(filters, page);
  }

  function readAnalysisDates() {
    return { startDate: $("analysis-start").value, endDate: $("analysis-end").value };
  }

  function clearAnalysisResults(message) {
    $("analysis-metrics").hidden = true;
    $("analysis-metrics").setAttribute("aria-busy", "true");
    for (const kind of ["volume", "status", "type"]) clearChartState("analysis", kind, message);
  }

  function renderAnalysis(data, dates) {
    const range = formatDateRange(dates.startDate, dates.endDate);
    $("analysis-period").textContent = `Selected period · ${range}. The API uses an exclusive end at midnight after the through date.`;
    $("analysis-scope").textContent = range;
    $("analysis-status").textContent = "";
    $("analysis-error").hidden = true;
    $("analysis-metrics").hidden = false;
    $("analysis-metrics").setAttribute("aria-busy", "false");
    $("analysis-total").textContent = numberFormat.format(data.total_transaction_count);
    $("analysis-success-count").textContent = numberFormat.format(data.successful_transaction_count);
    $("analysis-success-value").textContent = formatMoney(data.successful_transaction_value);
    $("analysis-average").textContent = formatMoney(data.average_successful_transaction_amount);
    $("analysis-deposits").textContent = formatMoney(data.successful_deposit_total);
    $("analysis-withdrawals").textContent = formatMoney(data.successful_withdrawal_total);
    renderDailyChart("analysis", data.daily_volume, "No transaction events occurred in this date range.");
    renderCountChart("analysis", "status", data.counts_by_status, STATUS_LABELS,
      Object.keys(data.counts_by_status).map((key) => STATUS_COLORS[key] || "#718087"));
    renderCountChart("analysis", "type", data.counts_by_type, TYPE_LABELS, TYPE_COLORS);
    for (const [channel, id] of Object.entries({ UPI: "analysis-channel-upi", ATM: "analysis-channel-atm", BRANCH: "analysis-channel-branch", UNKNOWN: "analysis-channel-unknown" })) {
      $(id).textContent = numberFormat.format(data.counts_by_channel?.[channel] || 0);
    }
    document.querySelectorAll(".channel-export").forEach((link) => {
      const params = new URLSearchParams({
        channel: link.dataset.channel,
        start_date: dates.startDate,
        end_date: window.TransactionApi.toExclusiveEnd(dates.endDate),
      });
      link.href = `/api/transactions/export?${params.toString()}`;
    });
  }

  function validateAnalysisDates(dates) {
    if (!dates.startDate || !dates.endDate) return "Select both a start date and a through date.";
    if (dates.startDate > dates.endDate) return "The start date must be on or before the through date.";
    if (dates.startDate > MAX_DEMO_DATE || dates.endDate > MAX_DEMO_DATE) {
      return `Choose dates on or before ${MAX_DEMO_DATE} (UTC).`;
    }
    return "";
  }

  async function loadAnalysis(dates) {
    const { controller, version } = beginRequest("analysis");
    $("analysis-error").hidden = true;
    $("analysis-status").textContent = "Loading selected-period analytics…";
    clearAnalysisResults("Loading selected-period data…");
    const invalid = validateAnalysisDates(dates);
    if (invalid) {
      controller.abort();
      $("analysis-status").textContent = "";
      $("analysis-error").textContent = invalid;
      $("analysis-error").hidden = false;
      $("analysis-metrics").setAttribute("aria-busy", "false");
      $("analysis-period").textContent = "Selected period unavailable until the date range is corrected.";
      $("analysis-scope").textContent = "Correct the dates to load period metrics.";
      for (const kind of ["volume", "status", "type"]) $(`analysis-${kind}-empty`).hidden = true;
      return;
    }
    const range = formatDateRange(dates.startDate, dates.endDate);
    $("analysis-period").textContent = `Selected period · ${range}. The API uses an exclusive end at midnight after the through date.`;
    $("analysis-scope").textContent = range;
    try {
      const response = await window.TransactionApi.getAnalysis(dates.startDate, dates.endDate, { signal: controller.signal });
      if (isCurrentRequest("analysis", version)) renderAnalysis(response.data, dates);
    } catch (error) {
      if (error.name === "AbortError" || !isCurrentRequest("analysis", version)) return;
      const message = friendlyError(error);
      $("analysis-status").textContent = "";
      $("analysis-error").textContent = message;
      $("analysis-error").hidden = false;
      $("analysis-metrics").hidden = true;
      $("analysis-metrics").setAttribute("aria-busy", "false");
      for (const kind of ["volume", "status", "type"]) showChartError("analysis", kind, message);
    }
  }

  function initializeAnalysis() {
    const defaults = { startDate: DEFAULT_START, endDate: DEFAULT_END };
    $("analysis-period").textContent = `Selected period · ${formatDateRange(defaults.startDate, defaults.endDate)}.`;
    $("analysis-form").addEventListener("submit", (event) => {
      event.preventDefault();
      loadAnalysis(readAnalysisDates());
    });
    $("reset-analysis").addEventListener("click", () => {
      $("analysis-start").value = DEFAULT_START;
      $("analysis-end").value = DEFAULT_END;
      loadAnalysis(defaults);
    });
    loadAnalysis(readAnalysisDates());
  }

  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page;
    if (page === "overview") loadOverview();
    if (page === "transactions") initializeTransactions();
    if (page === "analysis") initializeAnalysis();
  });
})();
