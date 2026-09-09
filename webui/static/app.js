(() => {
  "use strict";

  const state = {
    symbol: "BTCUSDT",
    market: "crypto",
    timeframe: "1h",
    signal: null,
    chart: null,
    series: null,
    priceLines: [],
    overlayPriceLines: [],
    overlaySeries: [],
    markerPlugin: null,
    session: "London",
    sessionId: window.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    // Intentionally held in memory only; never persist a broker-dashboard secret in browser storage.
    oandaAccessToken: "",
  };

  const $ = (selector) => document.querySelector(selector);
  const elements = {
    chart: $("#chart"),
    symbol: $("#symbol-input"),
    market: $("#market-select"),
    session: $("#session-select"),
    source: $("#data-source"),
    marketBadge: $("#market-badge"),
    instrumentName: $("#instrument-name"),
    chartTitle: $("#chart-title"),
    updated: $("#last-updated"),
    error: $("#error-banner"),
    analyze: $("#analyze-button"),
    refresh: $("#refresh-button"),
    signalStatus: $("#signal-status"),
    signalAction: $("#signal-action"),
    signalConfidence: $("#signal-confidence"),
    levelEntry: $("#level-entry"),
    levelStop: $("#level-stop"),
    levelTarget: $("#level-target"),
    levelRr: $("#level-rr"),
    signalNotice: $("#signal-notice"),
    metricClose: $("#metric-close"),
    metricAtr: $("#metric-atr"),
    metricEma: $("#metric-ema"),
    metricRsi: $("#metric-rsi"),
    reasoning: $("#reasoning-list"),
    exits: $("#exit-list"),
    sourceChip: $("#source-chip"),
    model: $("#model-select"),
    chatForm: $("#chat-form"),
    chatInput: $("#chat-input"),
    chatLog: $("#chat-log"),
    memoryStatus: $("#memory-status"),
    systemStatus: $("#system-status"),
    statusDot: $(".status-dot"),
    oandaMode: $("#oanda-mode"),
    oandaEnvironment: $("#oanda-environment"),
    oandaAccessToken: $("#oanda-access-token"),
    oandaRefresh: $("#oanda-refresh"),
    accountBalance: $("#account-balance"),
    accountNav: $("#account-nav"),
    accountMargin: $("#account-margin"),
    accountMarginUsed: $("#account-margin-used"),
    accountTrades: $("#account-trades"),
    accountUnrealizedPl: $("#account-unrealized-pl"),
    oandaQuote: $("#oanda-quote"),
    accountPositions: $("#account-positions"),
    accountOpenTrades: $("#account-open-trades"),
    accountPendingOrders: $("#account-pending-orders"),
    overlayStatus: $("#overlay-status"),
    overlaySummary: $("#overlay-summary"),
    overlayZones: $("#overlay-zones"),
    overlayNotice: $("#overlay-notice"),
  };

  function showError(message) {
    elements.error.textContent = message;
    elements.error.hidden = !message;
  }

  function formatPrice(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    const decimals = Math.abs(number) >= 1000 ? 2 : Math.abs(number) >= 1 ? 5 : 8;
    return number.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: decimals });
  }

  function formatTime(timestamp) {
    return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", month: "short", day: "2-digit" }).format(new Date(timestamp));
  }

  function configureChart() {
    const library = window.LightweightCharts;
    if (!library) {
      elements.chart.textContent = "Chart library unavailable. Market analysis remains available when the API is online.";
      elements.chart.style.display = "grid";
      elements.chart.style.placeItems = "center";
      elements.chart.style.color = "#8d9bae";
      return;
    }
    state.chart = library.createChart(elements.chart, {
      width: elements.chart.clientWidth,
      height: elements.chart.clientHeight,
      layout: { background: { type: "solid", color: "#0e1621" }, textColor: "#8d9bae", fontFamily: "Inter, system-ui, sans-serif" },
      grid: { vertLines: { color: "rgba(39, 54, 73, .55)" }, horzLines: { color: "rgba(39, 54, 73, .55)" } },
      rightPriceScale: { borderColor: "#273649" },
      timeScale: { borderColor: "#273649", timeVisible: true, secondsVisible: false },
      crosshair: { vertLine: { color: "rgba(141,155,174,.28)" }, horzLine: { color: "rgba(141,155,174,.28)" } },
    });
    const candleOptions = {
      upColor: "#19c790", downColor: "#ff6475", borderVisible: false,
      wickUpColor: "#19c790", wickDownColor: "#ff6475",
    };
    // v4 and v5 have different series constructors; supporting both keeps the static UI resilient.
    state.series = state.chart.addCandlestickSeries
      ? state.chart.addCandlestickSeries(candleOptions)
      : state.chart.addSeries(library.CandlestickSeries, candleOptions);
    new ResizeObserver(([entry]) => {
      if (state.chart) state.chart.applyOptions({ width: Math.floor(entry.contentRect.width) });
    }).observe(elements.chart);
  }

  function clearPriceLines() {
    if (!state.series) return;
    state.priceLines.forEach((line) => state.series.removePriceLine(line));
    state.priceLines = [];
  }

  function clearOverlayAnnotations() {
    if (!state.series) return;
    state.overlayPriceLines.forEach((line) => state.series.removePriceLine(line));
    state.overlayPriceLines = [];
    state.overlaySeries.forEach((series) => state.chart.removeSeries(series));
    state.overlaySeries = [];
    if (state.markerPlugin?.detach) state.markerPlugin.detach();
    state.markerPlugin = null;
    if (state.series.setMarkers) state.series.setMarkers([]);
  }

  function overlayLineStyle(style) {
    // Lightweight Charts uses 0 solid, 1 dotted, 2 dashed in both supported versions.
    return style === "solid" ? 0 : style === "dotted" ? 1 : 2;
  }

  function addZoneSegment(zone) {
    if (!state.chart || !window.LightweightCharts) return;
    const bullish = zone.direction === "bullish";
    const color = zone.active
      ? bullish ? "#4dd5a1" : "#ff8491"
      : bullish ? "rgba(77, 213, 161, .45)" : "rgba(255, 132, 145, .45)";
    const options = {
      color,
      lineWidth: zone.active ? 2 : 1,
      lineStyle: overlayLineStyle("dotted"),
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    };
    const createLine = state.chart.addLineSeries
      ? state.chart.addLineSeries.bind(state.chart)
      : (lineOptions) => state.chart.addSeries(window.LightweightCharts.LineSeries, lineOptions);
    [zone.bottom, zone.top].forEach((price) => {
      const series = createLine(options);
      series.setData([
        { time: Math.floor(zone.start_timestamp / 1000), value: price },
        { time: Math.floor(zone.end_timestamp / 1000), value: price },
      ]);
      state.overlaySeries.push(series);
    });
  }

  function renderOverlayMarkers(markers) {
    if (!state.series) return;
    const chartMarkers = markers.map((marker) => ({
      time: Math.floor(marker.timestamp / 1000),
      position: marker.position,
      color: marker.color,
      shape: marker.shape,
      text: marker.text,
    }));
    if (state.series.setMarkers) {
      state.series.setMarkers(chartMarkers);
    } else if (window.LightweightCharts?.createSeriesMarkers) {
      state.markerPlugin = window.LightweightCharts.createSeriesMarkers(state.series, chartMarkers);
    }
  }

  function updateOverlayPanel(overlays) {
    elements.overlayStatus.className = "signal-status neutral";
    elements.overlayStatus.textContent = `${overlays.markers.length} MARKERS`;
    elements.overlaySummary.replaceChildren();
    overlays.summary.forEach((item) => {
      const summary = document.createElement("div");
      summary.className = `overlay-summary-item ${item.tone}`;
      const label = document.createElement("span");
      label.textContent = item.label;
      const value = document.createElement("strong");
      value.textContent = item.value;
      summary.append(label, value);
      elements.overlaySummary.append(summary);
    });
    const zones = overlays.zones.map((zone) => `${zone.label}${zone.active ? " · active" : " · mitigated"}: ${formatPrice(zone.bottom)} – ${formatPrice(zone.top)}`);
    listItems(elements.overlayZones, zones.length ? zones : ["No recent FVG or order-block zones in the loaded closed-candle window."]);
    elements.overlayNotice.textContent = overlays.warnings.length
      ? overlays.warnings.join(" ")
      : "Markers and levels use closed candles. Swing/FVG tails that require later confirmation are intentionally hidden; historical liquidity and order-block overlays remain research context, not execution instructions.";
  }

  function renderOverlays(overlays) {
    if (state.series) {
      clearOverlayAnnotations();
      overlays.levels.forEach((level) => {
        state.overlayPriceLines.push(state.series.createPriceLine({
          price: level.price,
          color: level.color,
          lineWidth: 1,
          lineStyle: overlayLineStyle(level.style),
          axisLabelVisible: true,
          title: level.label,
        }));
      });
      // FVG/OB regions are rendered as time-bounded paired lines. Active zones are
      // brighter; mitigated zones stay visible in the historical overlay map.
      overlays.zones.forEach(addZoneSegment);
      renderOverlayMarkers(overlays.markers);
    }
    updateOverlayPanel(overlays);
  }

  function renderChart(candles) {
    if (!state.series) return;
    clearOverlayAnnotations();
    const data = candles.map((candle) => ({
      time: Math.floor(candle.timestamp / 1000),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    }));
    state.series.setData(data);
    state.chart.timeScale().fitContent();
  }

  function addLevel(price, title, color, style = 2) {
    if (!state.series || price === null || price === undefined) return;
    state.priceLines.push(state.series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  }

  function updateHeader(response) {
    const marketClass = response.market === "forex" ? "forex" : response.market === "futures" ? "futures" : "crypto";
    elements.instrumentName.textContent = response.symbol;
    elements.chartTitle.textContent = `${response.symbol} · ${state.timeframe}`;
    elements.marketBadge.textContent = response.market.toUpperCase();
    elements.marketBadge.className = `pill ${marketClass}`;
    elements.source.textContent = response.source;
    elements.updated.textContent = `Last closed bar: ${formatTime(response.candles.at(-1).timestamp)}`;
  }

  function listItems(container, values) {
    container.replaceChildren();
    values.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      container.append(item);
    });
  }

  function updateSignal(signal) {
    state.signal = signal;
    const actionClass = signal.action === "BUY" ? "buy" : signal.action === "SELL" ? "sell" : "no-signal";
    elements.signalStatus.className = `signal-status ${actionClass}`;
    elements.signalStatus.textContent = signal.action === "NO_SIGNAL" ? "NO SIGNAL" : signal.action;
    elements.signalAction.textContent = signal.action === "NO_SIGNAL" ? "WAIT" : signal.action;
    elements.signalAction.style.color = signal.action === "BUY" ? "var(--green)" : signal.action === "SELL" ? "var(--red)" : "var(--yellow)";
    elements.signalConfidence.textContent = `${signal.confidence}%`;
    elements.levelEntry.textContent = formatPrice(signal.entry_price);
    elements.levelStop.textContent = formatPrice(signal.stop_loss);
    elements.levelTarget.textContent = formatPrice(signal.take_profit);
    elements.levelRr.textContent = signal.risk_reward ? `${signal.risk_reward}:1` : "—";
    elements.signalNotice.textContent = signal.risk_notice;
    elements.metricClose.textContent = formatPrice(signal.technicals.close);
    elements.metricAtr.textContent = formatPrice(signal.technicals.atr);
    elements.metricEma.textContent = `${formatPrice(signal.technicals.ema_fast)} / ${formatPrice(signal.technicals.ema_slow)}`;
    elements.metricRsi.textContent = signal.technicals.rsi.toFixed(1);
    elements.sourceChip.textContent = signal.data_source.includes("Twelve Data")
      ? "TWELVE DATA"
      : signal.data_source.includes("Kraken")
        ? "KRAKEN"
        : signal.data_source.includes("Coinbase")
          ? "COINBASE"
          : signal.data_source.includes("Bybit")
          ? "BYBIT"
          : signal.data_source.includes("Binance") ? "BINANCE" : "YAHOO";
    listItems(elements.reasoning, signal.reasoning);
    listItems(elements.exits, signal.exit_plan);
    clearPriceLines();
    addLevel(signal.entry_price, "Reference entry", "#5d8dff");
    addLevel(signal.stop_loss, "Stop · 1.5 ATR", "#ff6475");
    addLevel(signal.take_profit, "Target · 2R", "#19c790");
  }

  function updateOanda(snapshot) {
    const currency = snapshot.currency || "";
    elements.oandaMode.className = `signal-status ${snapshot.environment === "practice" ? "buy" : "neutral"}`;
    elements.oandaMode.textContent = `${snapshot.environment.toUpperCase()} · READ ONLY`;
    elements.accountBalance.textContent = `${formatPrice(snapshot.balance)} ${currency}`.trim();
    elements.accountNav.textContent = `${formatPrice(snapshot.nav)} ${currency}`.trim();
    elements.accountMargin.textContent = `${formatPrice(snapshot.margin_available)} ${currency}`.trim();
    elements.accountMarginUsed.textContent = `${formatPrice(snapshot.margin_used)} ${currency}`.trim();
    elements.accountTrades.textContent = `${snapshot.open_trade_count} / ${snapshot.pending_order_count}`;
    elements.accountUnrealizedPl.textContent = `${formatPrice(snapshot.unrealized_pl)} ${currency}`.trim();
    if (snapshot.quote) {
      elements.oandaQuote.textContent = `${snapshot.quote.instrument} · bid ${formatPrice(snapshot.quote.bid)} · ask ${formatPrice(snapshot.quote.ask)} · ${snapshot.quote.timestamp}`;
    } else if (state.market !== "forex") {
      elements.oandaQuote.textContent = "Select Forex and a six-letter pair (for example EURUSD) to request OANDA bid / ask context.";
    } else {
      elements.oandaQuote.textContent = "No OANDA price is currently available for this instrument.";
    }
    const positions = snapshot.positions.map((position) => `${position.instrument}: long ${position.long_units}, short ${position.short_units}, unrealized P/L ${formatPrice(position.unrealized_pl)}`);
    const openTrades = snapshot.open_trades.map((trade) => `#${trade.id} · ${trade.instrument}: ${trade.current_units} units at ${formatPrice(trade.price)}, unrealized P/L ${formatPrice(trade.unrealized_pl)}`);
    const pendingOrders = snapshot.pending_orders.map((order) => `#${order.id} · ${order.order_type} ${order.instrument}: ${order.units} units${order.price === null ? "" : ` at ${formatPrice(order.price)}`}`);
    listItems(elements.accountPositions, positions.length ? positions : ["No open positions."]);
    listItems(elements.accountOpenTrades, openTrades.length ? openTrades : ["No open trades."]);
    listItems(elements.accountPendingOrders, pendingOrders.length ? pendingOrders : ["No pending orders."]);
  }

  async function loadOandaAccount() {
    const accessToken = elements.oandaAccessToken.value.trim();
    if (!accessToken) {
      showError("Enter the separate dashboard access token to view protected OANDA account data. Do not enter your OANDA API token in the browser.");
      return;
    }
    state.oandaAccessToken = accessToken;
    elements.oandaRefresh.disabled = true;
    elements.oandaRefresh.textContent = "Loading read-only data…";
    const environment = elements.oandaEnvironment.value;
    const query = state.market === "forex" ? `?instrument=${encodeURIComponent(state.symbol)}` : "";
    try {
      const snapshot = await requestJson(`/api/oanda/accounts/${environment}${query}`, {
        headers: { "X-SMC-Access-Token": accessToken },
      });
      updateOanda(snapshot);
      showError("");
    } catch (error) {
      elements.oandaMode.className = "signal-status neutral";
      elements.oandaMode.textContent = "UNAVAILABLE";
      showError(`OANDA account data unavailable: ${error.message}`);
    } finally {
      elements.oandaRefresh.disabled = false;
      elements.oandaRefresh.textContent = "Load account";
    }
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* use status text */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  function setBusy(busy) {
    elements.analyze.disabled = busy;
    elements.refresh.disabled = busy;
    elements.analyze.textContent = busy ? "Loading closed candles…" : "Analyze closed candles";
  }

  async function refreshDashboard() {
    state.symbol = elements.symbol.value.trim().toUpperCase().replaceAll("/", "").replaceAll("-", "");
    state.market = elements.market.value;
    if (!state.symbol) return showError("Enter a market symbol before requesting data.");
    elements.symbol.value = state.symbol;
    showError("");
    elements.overlayStatus.className = "signal-status neutral";
    elements.overlayStatus.textContent = "LOADING";
    setBusy(true);
    try {
      const parameters = new URLSearchParams({ symbol: state.symbol, market: state.market, timeframe: state.timeframe, limit: "300" });
      const candleResponse = await requestJson(`/api/market/candles?${parameters}`);
      renderChart(candleResponse.candles);
      updateHeader(candleResponse);
      const overlayParameters = new URLSearchParams({
        symbol: state.symbol,
        market: state.market,
        timeframe: state.timeframe,
        session: state.session,
        limit: "300",
      });
      const signal = await requestJson("/api/signals/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: state.symbol, market: state.market, timeframe: state.timeframe, limit: 300 }),
      });
      updateSignal(signal);
      try {
        const overlays = await requestJson(`/api/market/overlays?${overlayParameters}`);
        renderOverlays(overlays);
      } catch (overlayError) {
        clearOverlayAnnotations();
        elements.overlayStatus.className = "signal-status neutral";
        elements.overlayStatus.textContent = "UNAVAILABLE";
        elements.overlaySummary.replaceChildren();
        elements.overlaySummary.textContent = "The candle chart and deterministic signal remain available.";
        listItems(elements.overlayZones, ["No overlay zones loaded."]);
        elements.overlayNotice.textContent = `SMC overlay unavailable: ${overlayError.message}`;
      }
    } catch (error) {
      showError(`Market data, analysis, or chart overlay unavailable: ${error.message}`);
      elements.source.textContent = "No verified data loaded";
    } finally {
      setBusy(false);
    }
  }

  function appendChat(role, message) {
    const node = document.createElement("div");
    node.className = `chat-message ${role === "user" ? "user-message" : "assistant-message"}`;
    node.textContent = message;
    elements.chatLog.append(node);
    elements.chatLog.scrollTop = elements.chatLog.scrollHeight;
  }

  async function sendChat(event) {
    event.preventDefault();
    const message = elements.chatInput.value.trim();
    if (!message) return;
    appendChat("user", message);
    elements.chatInput.value = "";
    const button = elements.chatForm.querySelector("button");
    button.disabled = true;
    try {
      const response = await requestJson("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          message,
          model_id: elements.model.value,
          symbol: state.symbol,
          market: state.market,
          timeframe: state.timeframe,
        }),
      });
      appendChat("assistant", response.reply);
      elements.memoryStatus.textContent = response.memory_enabled
        ? "Recent context: MongoDB enabled (30-day server-side retention)."
        : "Recent context: this browser session only (in-memory server fallback).";
    } catch (error) {
      appendChat("assistant", `I could not answer that request: ${error.message}`);
    } finally {
      button.disabled = false;
      elements.chatInput.focus();
    }
  }

  async function loadModelsAndHealth() {
    try {
      const [models, health] = await Promise.all([requestJson("/api/models"), requestJson("/api/health")]);
      elements.model.replaceChildren();
      models.forEach((model) => {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = `${model.label}${model.configured ? "" : " · configure server key"}`;
        option.disabled = !model.configured;
        elements.model.append(option);
      });
      elements.systemStatus.textContent = `${health.memory === "mongo" ? "Mongo memory" : "Session memory"} · ${health.telegram_configured ? "Telegram ready" : "Telegram optional"}`;
      elements.statusDot.className = "status-dot connected";
      elements.memoryStatus.textContent = health.memory === "mongo"
        ? "Recent context: MongoDB enabled (30-day server-side retention)."
        : "Recent context: this browser session only (in-memory server fallback).";
      const oandaReady = health.oanda?.access_protected && (health.oanda?.practice_configured || health.oanda?.live_configured);
      elements.oandaMode.textContent = oandaReady ? "READY · READ ONLY" : "NOT CONFIGURED";
      elements.oandaMode.className = `signal-status ${oandaReady ? "buy" : "neutral"}`;
    } catch (error) {
      elements.systemStatus.textContent = "API unavailable";
      elements.statusDot.className = "status-dot error";
      showError(`Service health check failed: ${error.message}`);
    }
  }

  function wireInteractions() {
    elements.oandaAccessToken.value = state.oandaAccessToken;
    document.querySelectorAll(".timeframes button").forEach((button) => {
      button.addEventListener("click", () => {
        state.timeframe = button.dataset.timeframe;
        document.querySelectorAll(".timeframes button").forEach((item) => item.classList.toggle("selected", item === button));
        refreshDashboard();
      });
    });
    document.querySelectorAll(".watch-item").forEach((button) => {
      button.addEventListener("click", () => {
        state.symbol = button.dataset.symbol;
        state.market = button.dataset.market;
        elements.symbol.value = state.symbol;
        elements.market.value = state.market;
        document.querySelectorAll(".watch-item").forEach((item) => item.classList.toggle("active", item === button));
        refreshDashboard();
      });
    });
    elements.market.addEventListener("change", () => { state.market = elements.market.value; });
    elements.session.addEventListener("change", () => {
      state.session = elements.session.value;
      refreshDashboard();
    });
    elements.symbol.addEventListener("keydown", (event) => { if (event.key === "Enter") refreshDashboard(); });
    elements.analyze.addEventListener("click", refreshDashboard);
    elements.refresh.addEventListener("click", refreshDashboard);
    elements.oandaRefresh.addEventListener("click", loadOandaAccount);
    elements.chatForm.addEventListener("submit", sendChat);
  }

  configureChart();
  wireInteractions();
  loadModelsAndHealth();
  refreshDashboard();
})();
