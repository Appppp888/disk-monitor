const state = {
  rows: [],
  alerts: [],
  hiddenItems: [],
  hiddenKeys: new Set(),
  sortKey: "session_write_bytes",
  sortDir: -1,
  filter: "",
  settingsReady: false,
  consecutiveFailures: 0,
  lastSuccessText: "",
  visibleRows: [],
  selectedRow: null,
  notifiedAlertIds: new Set(),
  pageLoadedAt: Date.now() / 1000,
};

const els = {
  subline: document.querySelector("#subline"),
  totalWrite: document.querySelector("#totalWrite"),
  totalRead: document.querySelector("#totalRead"),
  writeRate: document.querySelector("#writeRate"),
  nextLog: document.querySelector("#nextLog"),
  processBody: document.querySelector("#processBody"),
  rowCount: document.querySelector("#rowCount"),
  tableMeta: document.querySelector("#tableMeta"),
  filterInput: document.querySelector("#filterInput"),
  sampleInterval: document.querySelector("#sampleInterval"),
  logInterval: document.querySelector("#logInterval"),
  logEnabled: document.querySelector("#logEnabled"),
  monitorDuringSleep: document.querySelector("#monitorDuringSleep"),
  alertEnabled: document.querySelector("#alertEnabled"),
  alertWindow: document.querySelector("#alertWindow"),
  alertThreshold: document.querySelector("#alertThreshold"),
  dailyReportEnabled: document.querySelector("#dailyReportEnabled"),
  dailyReportTime: document.querySelector("#dailyReportTime"),
  dailyReportInfo: document.querySelector("#dailyReportInfo"),
  dailyReportInfoText: document.querySelector("#dailyReportInfoText"),
  weeklyReportEnabled: document.querySelector("#weeklyReportEnabled"),
  weeklyReportDay: document.querySelector("#weeklyReportDay"),
  weeklyReportTime: document.querySelector("#weeklyReportTime"),
  weeklyReportInfo: document.querySelector("#weeklyReportInfo"),
  weeklyReportInfoText: document.querySelector("#weeklyReportInfoText"),
  logDirectory: document.querySelector("#logDirectory"),
  chooseFolderBtn: document.querySelector("#chooseFolderBtn"),
  detailOverlay: document.querySelector("#detailOverlay"),
  closeDetailBtn: document.querySelector("#closeDetailBtn"),
  detailTitle: document.querySelector("#detailTitle"),
  detailSubtitle: document.querySelector("#detailSubtitle"),
  detailAppName: document.querySelector("#detailAppName"),
  detailPids: document.querySelector("#detailPids"),
  detailPath: document.querySelector("#detailPath"),
  detailSessionWrite: document.querySelector("#detailSessionWrite"),
  detailSessionRead: document.querySelector("#detailSessionRead"),
  detailWriteRate: document.querySelector("#detailWriteRate"),
  detailReadRate: document.querySelector("#detailReadRate"),
  copyPathBtn: document.querySelector("#copyPathBtn"),
  copyAllBtn: document.querySelector("#copyAllBtn"),
  hideProcessBtn: document.querySelector("#hideProcessBtn"),
  hiddenSettingsBtn: document.querySelector("#hiddenSettingsBtn"),
  hiddenOverlay: document.querySelector("#hiddenOverlay"),
  closeHiddenBtn: document.querySelector("#closeHiddenBtn"),
  hiddenMeta: document.querySelector("#hiddenMeta"),
  hiddenList: document.querySelector("#hiddenList"),
  alertButton: document.querySelector("#alertButton"),
  alertBadge: document.querySelector("#alertBadge"),
  alertOverlay: document.querySelector("#alertOverlay"),
  closeAlertBtn: document.querySelector("#closeAlertBtn"),
  snapshotBtn: document.querySelector("#snapshotBtn"),
  refreshBtn: document.querySelector("#refreshBtn"),
  alertMeta: document.querySelector("#alertMeta"),
  alertList: document.querySelector("#alertList"),
  aboutProjectBtn: document.querySelector("#aboutProjectBtn"),
  aboutOverlay: document.querySelector("#aboutOverlay"),
  closeAboutBtn: document.querySelector("#closeAboutBtn"),
};

function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = Math.max(0, Number(value || 0));
  for (const unit of units) {
    if (size < 1024 || unit === units[units.length - 1]) {
      return unit === "B" ? `${Math.round(size)} ${unit}` : `${size.toFixed(2)} ${unit}`;
    }
    size /= 1024;
  }
  return `${size.toFixed(2)} PB`;
}

function formatDuration(seconds) {
  const safe = Math.max(0, Math.round(Number(seconds || 0)));
  const h = Math.floor(safe / 3600);
  const m = Math.floor((safe % 3600) / 60);
  const s = safe % 60;
  if (h) return `${h}小时 ${m}分`;
  if (m) return `${m}分 ${s}秒`;
  return `${s}秒`;
}

function formatNumber(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "0";
  return number.toFixed(2).replace(/\.?0+$/, "");
}

function weekdayName(value) {
  const index = Number(value);
  return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
    Number.isFinite(index) ? index : 6
  ] || "周日";
}

async function getJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function loadState() {
  try {
    const payload = await getJson("/api/state");
    state.consecutiveFailures = 0;
    state.lastSuccessText = payload.now_text || "";
    state.rows = payload.rows || [];
    state.alerts = payload.alerts || [];
    syncHiddenItems(payload.hidden_items || []);
    renderSummary(payload);
    renderRows();
    renderAlerts();
    notifyNewAlerts(state.alerts);
    syncSettings(payload);
    return payload;
  } catch (error) {
    state.consecutiveFailures += 1;
    const hasData = state.rows.length > 0;
    if (hasData && state.consecutiveFailures < 3) {
      els.subline.textContent = `本地采样器短暂无响应，正在重试；上次成功 ${state.lastSuccessText || "--"}`;
    } else {
      els.subline.innerHTML = `<span class="error">连接本地采样器失败：${error.message}</span>`;
    }
    return null;
  }
}

function renderSummary(payload) {
  const totals = payload.totals || {};
  els.totalWrite.textContent = formatBytes(totals.session_write_bytes);
  els.totalRead.textContent = formatBytes(totals.session_read_bytes);
  els.writeRate.textContent = `${formatBytes(totals.write_rate_bps)}/s`;
  els.nextLog.textContent = payload.log_enabled ? formatDuration(payload.next_log_in_seconds) : "已暂停";
  const status = payload.status === "running" ? "运行中" : payload.status;
  const error = payload.error ? `；${payload.error}` : "";
  els.subline.textContent = `${status}；开始于 ${payload.started_text}；上次采样 ${payload.last_sample_text}${error}`;
  els.tableMeta.textContent = `${payload.running_processes} 个进程；${payload.tracked_apps} 个分组`;
}

function syncSettings(payload) {
  if (!state.settingsReady) {
    els.sampleInterval.value = String(Math.round(payload.sample_interval_seconds || 5));
    els.logInterval.value = String(Math.round((payload.log_interval_seconds || 1800) / 60));
    els.logEnabled.checked = Boolean(payload.log_enabled);
    els.monitorDuringSleep.checked = Boolean(payload.monitor_during_sleep);
    els.alertEnabled.checked = Boolean(payload.alert_enabled);
    els.alertWindow.value = String(Math.round((payload.alert_window_seconds || 600) / 60));
    els.alertThreshold.value = formatNumber(payload.alert_threshold_gb || 10);
    els.dailyReportEnabled.checked = Boolean(payload.daily_report_enabled);
    els.dailyReportTime.value = payload.daily_report_time || "23:55";
    els.weeklyReportEnabled.checked = Boolean(payload.weekly_report_enabled);
    els.weeklyReportDay.value = String(payload.weekly_report_day ?? 6);
    els.weeklyReportTime.value = payload.weekly_report_time || "23:55";
    state.settingsReady = true;
  }
  els.logDirectory.value = payload.log_directory || "";
  els.logDirectory.title = payload.log_directory || "";
  renderReportInfo(payload);
}

function renderReportInfo(payload) {
  const dailyText = payload.daily_report_enabled
    ? [
        `日报周期: 每日 ${payload.daily_report_time || "--"} 作为边界。`,
        "首次启用或修改后: 从设置生效时刻开始，到下一次日报时间结束。",
        "之后: 从上一次日报时间到下一次日报时间。",
        `当前周期起点: ${payload.daily_period_start_text || "--"}`,
        `下一次写入: ${payload.next_daily_report_text || "--"}`,
        `剩余: ${formatDuration(payload.next_daily_report_in_seconds)}`,
      ].join("\n")
    : [
        `日报周期: 每日 ${payload.daily_report_time || "--"} 作为边界。`,
        "当前状态: 未开启。",
        "开启或修改后，首个周期从设置生效时刻开始。",
      ].join("\n");

  const weeklyText = payload.weekly_report_enabled
    ? [
        `周报周期: 每${weekdayName(payload.weekly_report_day)} ${payload.weekly_report_time || "--"} 作为边界。`,
        "首次启用或修改后: 从设置生效时刻开始，到下一次周报时间结束。",
        "之后: 从上一次周报时间到下一次周报时间。",
        `当前周期起点: ${payload.weekly_period_start_text || "--"}`,
        `下一次写入: ${payload.next_weekly_report_text || "--"}`,
        `剩余: ${formatDuration(payload.next_weekly_report_in_seconds)}`,
      ].join("\n")
    : [
        `周报周期: 每${weekdayName(payload.weekly_report_day)} ${payload.weekly_report_time || "--"} 作为边界。`,
        "当前状态: 未开启。",
        "开启或修改后，首个周期从设置生效时刻开始。",
      ].join("\n");

  els.dailyReportInfoText.textContent = dailyText;
  els.dailyReportInfo.title = dailyText;
  els.weeklyReportInfoText.textContent = weeklyText;
  els.weeklyReportInfo.title = weeklyText;
}

function placeInfoTooltip(button) {
  const tooltip = button.querySelector(".info-tooltip");
  if (!tooltip) return;
  tooltip.classList.add("is-visible");
  tooltip.style.left = "0px";
  tooltip.style.top = "0px";

  const margin = 12;
  const gap = 10;
  const buttonRect = button.getBoundingClientRect();
  const tooltipRect = tooltip.getBoundingClientRect();
  const maxLeft = window.innerWidth - tooltipRect.width - margin;
  const maxTop = window.innerHeight - tooltipRect.height - margin;

  let left = buttonRect.left + buttonRect.width / 2 - tooltipRect.width / 2;
  left = Math.min(Math.max(margin, left), Math.max(margin, maxLeft));

  let top = buttonRect.top - tooltipRect.height - gap;
  if (top < margin) {
    top = buttonRect.bottom + gap;
  }
  top = Math.min(Math.max(margin, top), Math.max(margin, maxTop));

  tooltip.style.left = `${Math.round(left)}px`;
  tooltip.style.top = `${Math.round(top)}px`;
}

function hideInfoTooltip(button) {
  const tooltip = button.querySelector(".info-tooltip");
  if (!tooltip) return;
  tooltip.classList.remove("is-visible");
}

function bindInfoTooltip(button) {
  button.addEventListener("mouseenter", () => placeInfoTooltip(button));
  button.addEventListener("focus", () => placeInfoTooltip(button));
  button.addEventListener("mouseleave", () => hideInfoTooltip(button));
  button.addEventListener("blur", () => hideInfoTooltip(button));
}

function renderRows() {
  const needle = state.filter.trim().toLowerCase();
  const rows = state.rows
    .filter((row) => !state.hiddenKeys.has(row.app_key))
    .filter((row) => {
      if (!needle) return true;
      return [row.app, row.path, (row.pids || []).join(",")]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    })
    .sort((a, b) => compareRows(a, b));

  const hiddenCount = state.hiddenItems.length;
  els.rowCount.textContent = hiddenCount
    ? `${rows.length} / ${state.rows.length} 项，隐藏 ${hiddenCount} 项`
    : `${rows.length} / ${state.rows.length} 项`;
  state.visibleRows = rows;
  if (!rows.length) {
    els.processBody.innerHTML = `<tr><td class="empty" colspan="8">没有匹配的进程</td></tr>`;
    return;
  }

  els.processBody.innerHTML = rows
    .map(
      (row, index) => `<tr>
        <td class="app">
          <button class="app-link" type="button" data-row-index="${index}" title="查看完整进程信息">${escapeHtml(row.app)}</button>
        </td>
        <td>${formatBytes(row.session_write_bytes)}</td>
        <td>${formatBytes(row.session_read_bytes)}</td>
        <td>${formatBytes(row.write_rate_bps)}/s</td>
        <td>${formatBytes(row.read_rate_bps)}/s</td>
        <td>${formatBytes(row.lifetime_write_bytes)}</td>
        <td>${escapeHtml((row.pids || []).join(", ") || "-")}</td>
        <td class="path" title="${escapeHtml(row.path)}">${escapeHtml(row.path)}</td>
      </tr>`
    )
    .join("");
  updateSortButtons();
}

function showProcessDetail(row) {
  state.selectedRow = row;
  const pids = (row.pids || []).join(", ") || "-";
  els.detailTitle.textContent = row.app || "进程详情";
  els.detailSubtitle.textContent = `进程数 ${row.process_count || 0}；已结束记录 ${row.completed_count || 0}`;
  els.detailAppName.value = row.app || "";
  els.detailPids.value = pids;
  els.detailPath.value = row.path || "";
  els.detailSessionWrite.value = formatBytes(row.session_write_bytes);
  els.detailSessionRead.value = formatBytes(row.session_read_bytes);
  els.detailWriteRate.value = `${formatBytes(row.write_rate_bps)}/s`;
  els.detailReadRate.value = `${formatBytes(row.read_rate_bps)}/s`;
  els.detailOverlay.hidden = false;
  els.detailPath.focus();
  els.detailPath.select();
}

function closeProcessDetail() {
  els.detailOverlay.hidden = true;
}

function showAboutProject() {
  els.aboutOverlay.hidden = false;
}

function closeAboutProject() {
  els.aboutOverlay.hidden = true;
}

function syncHiddenItems(items) {
  state.hiddenItems = normalizeHiddenItems(items);
  state.hiddenKeys = new Set(state.hiddenItems.map((item) => item.key));
  renderHiddenItems();
}

function normalizeHiddenItems(items) {
  if (!Array.isArray(items)) return [];
  const seen = new Set();
  const normalized = [];
  items.forEach((item) => {
    const key = String(item?.key || "").trim();
    if (!key || seen.has(key)) return;
    seen.add(key);
    normalized.push({
      key,
      app: String(item?.app || ""),
      path: String(item?.path || ""),
    });
  });
  return normalized;
}

async function saveHiddenItems(items) {
  const normalized = normalizeHiddenItems(items);
  const payload = await getJson("/api/settings", {
    method: "POST",
    body: JSON.stringify({ hidden_items: normalized }),
  });
  syncHiddenItems(payload.state?.hidden_items || normalized);
  renderRows();
}

async function hideSelectedProcess() {
  if (!state.selectedRow) return;
  const row = state.selectedRow;
  const key = row.app_key || `${row.app}\u001f${row.path}`;
  const next = [
    ...state.hiddenItems,
    {
      key,
      app: row.app || "",
      path: row.path || "",
    },
  ];
  await saveHiddenItems(next);
  closeProcessDetail();
}

async function restoreHiddenItem(key) {
  await saveHiddenItems(state.hiddenItems.filter((item) => item.key !== key));
}

function renderHiddenItems() {
  const count = state.hiddenItems.length;
  els.hiddenMeta.textContent = count ? `${count} 个隐藏项` : "暂无隐藏项";
  if (!count) {
    els.hiddenList.innerHTML = `<div class="log-meta">还没有隐藏任何应用或进程。</div>`;
    return;
  }
  els.hiddenList.innerHTML = state.hiddenItems
    .map(
      (item) => `<div class="hidden-item">
        <div>
          <strong>${escapeHtml(item.app || "未命名进程")}</strong>
          <small title="${escapeHtml(item.path)}">${escapeHtml(item.path || item.key)}</small>
        </div>
        <button class="restore-hidden" type="button" data-hidden-key="${escapeHtml(item.key)}">恢复显示</button>
      </div>`
    )
    .join("");
}

function showHiddenSettings() {
  renderHiddenItems();
  els.hiddenOverlay.hidden = false;
  els.closeHiddenBtn.focus();
}

function closeHiddenSettings() {
  els.hiddenOverlay.hidden = true;
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const temp = document.createElement("textarea");
  temp.value = text;
  document.body.appendChild(temp);
  temp.select();
  document.execCommand("copy");
  temp.remove();
}

function detailText(row) {
  const pids = (row.pids || []).join(", ") || "-";
  return [
    `完整应用/进程名: ${row.app || ""}`,
    `PID: ${pids}`,
    `完整可执行文件路径: ${row.path || ""}`,
    `启动后写入: ${formatBytes(row.session_write_bytes)}`,
    `启动后读取: ${formatBytes(row.session_read_bytes)}`,
    `当前写速: ${formatBytes(row.write_rate_bps)}/s`,
    `当前读速: ${formatBytes(row.read_rate_bps)}/s`,
  ].join("\n");
}

function compareRows(a, b) {
  const key = state.sortKey;
  const av = a[key];
  const bv = b[key];
  if (typeof av === "number" && typeof bv === "number") {
    return (av - bv) * state.sortDir;
  }
  return String(av || "").localeCompare(String(bv || ""), "zh-Hans-CN") * state.sortDir;
}

function updateSortButtons() {
  document.querySelectorAll(".sort-buttons button").forEach((button) => {
    const isActive =
      button.dataset.sort === state.sortKey &&
      Number(button.dataset.sortDir) === state.sortDir;
    button.classList.toggle("active", isActive);
    button.setAttribute("aria-pressed", isActive ? "true" : "false");
  });
}

function renderAlerts() {
  const count = state.alerts.length;
  els.alertMeta.textContent = count
    ? `${count} 条提醒`
    : "暂无提醒";
  els.alertBadge.hidden = count === 0;
  els.alertBadge.textContent = count > 99 ? "99+" : String(count);
  els.alertButton.setAttribute("aria-label", count ? `消息中心，${count} 条提醒` : "消息中心");
  if (!count) {
    els.alertList.innerHTML = `<div class="log-meta">还没有触发阈值提醒。</div>`;
    return;
  }
  els.alertList.innerHTML = state.alerts
    .map(
      (alert) => `<div class="alert-item" data-alert-id="${escapeHtml(alert.id)}">
        <div>
          <strong>${escapeHtml(alert.title)}</strong>
          <p>${escapeHtml(alert.body)}</p>
          <small>${escapeHtml(alert.window_start_text)} ~ ${escapeHtml(alert.window_end_text)} · 读取 ${formatBytes(alert.read_bytes)} · 写入 ${formatBytes(alert.write_bytes)}</small>
        </div>
        <button class="alert-delete" type="button" data-alert-id="${escapeHtml(alert.id)}">删除</button>
      </div>`
    )
    .join("");
}

function showAlertCenter() {
  els.alertOverlay.hidden = false;
  els.closeAlertBtn.focus();
}

function closeAlertCenter() {
  els.alertOverlay.hidden = true;
}

function notifyNewAlerts(alerts) {
  const handler = window.webkit?.messageHandlers?.systemNotification;
  alerts.forEach((alert) => {
    if (state.notifiedAlertIds.has(alert.id)) return;
    state.notifiedAlertIds.add(alert.id);
    if (Number(alert.created_at || 0) < state.pageLoadedAt - 2) return;
    if (handler) {
      handler.postMessage({
        id: alert.id,
        title: alert.title,
        body: alert.body,
      });
    }
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function saveSettings() {
  await getJson("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      sample_interval_seconds: Number(els.sampleInterval.value),
      log_interval_minutes: Number(els.logInterval.value),
      log_enabled: els.logEnabled.checked,
      monitor_during_sleep: els.monitorDuringSleep.checked,
      alert_enabled: els.alertEnabled.checked,
      alert_window_minutes: Number(els.alertWindow.value),
      alert_threshold_gb: Number(els.alertThreshold.value),
      daily_report_enabled: els.dailyReportEnabled.checked,
      daily_report_time: els.dailyReportTime.value,
      weekly_report_enabled: els.weeklyReportEnabled.checked,
      weekly_report_day: Number(els.weeklyReportDay.value),
      weekly_report_time: els.weeklyReportTime.value,
    }),
  });
  const payload = await loadState();
  notifyNativeSleepMode(els.monitorDuringSleep.checked);
}

function notifyNativeSleepMode(enabled) {
  const handler = window.webkit?.messageHandlers?.sleepModeChanged;
  if (handler) {
    handler.postMessage({ monitorDuringSleep: Boolean(enabled) });
  }
}

async function setLogDirectory(path) {
  if (!path) return;
  await getJson("/api/settings", {
    method: "POST",
    body: JSON.stringify({ log_directory: path }),
  });
  await loadState();
}

els.filterInput.addEventListener("input", () => {
  state.filter = els.filterInput.value;
  renderRows();
});

els.processBody.addEventListener("click", (event) => {
  const button = event.target.closest(".app-link");
  if (!button) return;
  const row = state.visibleRows[Number(button.dataset.rowIndex)];
  if (row) showProcessDetail(row);
});

els.alertList.addEventListener("click", async (event) => {
  const button = event.target.closest(".alert-delete");
  if (!button) return;
  button.disabled = true;
  const payload = await getJson("/api/alerts/delete", {
    method: "POST",
    body: JSON.stringify({ id: button.dataset.alertId }),
  });
  state.alerts = payload.state?.alerts || [];
  renderAlerts();
});

els.alertButton.addEventListener("click", showAlertCenter);
els.closeAlertBtn.addEventListener("click", closeAlertCenter);
els.alertOverlay.addEventListener("click", (event) => {
  if (event.target === els.alertOverlay) closeAlertCenter();
});

els.hiddenSettingsBtn.addEventListener("click", showHiddenSettings);
els.closeHiddenBtn.addEventListener("click", closeHiddenSettings);
els.hiddenOverlay.addEventListener("click", (event) => {
  if (event.target === els.hiddenOverlay) closeHiddenSettings();
});
els.hiddenList.addEventListener("click", async (event) => {
  const button = event.target.closest(".restore-hidden");
  if (!button) return;
  button.disabled = true;
  await restoreHiddenItem(button.dataset.hiddenKey || "");
});

els.closeDetailBtn.addEventListener("click", closeProcessDetail);
els.detailOverlay.addEventListener("click", (event) => {
  if (event.target === els.detailOverlay) closeProcessDetail();
});

els.aboutProjectBtn.addEventListener("click", showAboutProject);
els.closeAboutBtn.addEventListener("click", closeAboutProject);
els.aboutOverlay.addEventListener("click", (event) => {
  if (event.target === els.aboutOverlay) closeAboutProject();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !els.aboutOverlay.hidden) {
    closeAboutProject();
    return;
  }
  if (event.key === "Escape" && !els.hiddenOverlay.hidden) {
    closeHiddenSettings();
    return;
  }
  if (event.key === "Escape" && !els.alertOverlay.hidden) {
    closeAlertCenter();
    return;
  }
  if (event.key === "Escape" && !els.detailOverlay.hidden) {
    closeProcessDetail();
  }
});

els.copyPathBtn.addEventListener("click", async () => {
  if (state.selectedRow) await copyText(state.selectedRow.path || "");
});

els.copyAllBtn.addEventListener("click", async () => {
  if (state.selectedRow) await copyText(detailText(state.selectedRow));
});

els.hideProcessBtn.addEventListener("click", hideSelectedProcess);

els.sampleInterval.addEventListener("change", saveSettings);
els.logInterval.addEventListener("change", saveSettings);
els.logEnabled.addEventListener("change", saveSettings);
els.monitorDuringSleep.addEventListener("change", saveSettings);
els.alertEnabled.addEventListener("change", saveSettings);
els.alertWindow.addEventListener("change", saveSettings);
els.alertThreshold.addEventListener("change", saveSettings);
els.dailyReportEnabled.addEventListener("change", saveSettings);
els.dailyReportTime.addEventListener("change", saveSettings);
els.weeklyReportEnabled.addEventListener("change", saveSettings);
els.weeklyReportDay.addEventListener("change", saveSettings);
els.weeklyReportTime.addEventListener("change", saveSettings);
bindInfoTooltip(els.dailyReportInfo);
bindInfoTooltip(els.weeklyReportInfo);
window.addEventListener("scroll", () => {
  hideInfoTooltip(els.dailyReportInfo);
  hideInfoTooltip(els.weeklyReportInfo);
});
window.addEventListener("resize", () => {
  hideInfoTooltip(els.dailyReportInfo);
  hideInfoTooltip(els.weeklyReportInfo);
});

els.chooseFolderBtn.addEventListener("click", async () => {
  const nativeChooser = window.webkit?.messageHandlers?.chooseLogFolder;
  if (nativeChooser) {
    nativeChooser.postMessage({ current: els.logDirectory.value });
    return;
  }
  const path = window.prompt("请输入日志文件夹路径", els.logDirectory.value || "");
  if (path) {
    await setLogDirectory(path);
  }
});

window.afterNativeLogDirectorySelected = async function afterNativeLogDirectorySelected(result) {
  if (!result || result.ok !== true) {
    if (result?.error) window.alert(result.error);
    return;
  }
  await loadState();
};

els.snapshotBtn.addEventListener("click", async () => {
  els.snapshotBtn.disabled = true;
  try {
    await getJson("/api/snapshot", { method: "POST", body: "{}" });
    await loadState();
  } finally {
    els.snapshotBtn.disabled = false;
  }
});

els.refreshBtn.addEventListener("click", () => {
  loadState();
});

document.querySelectorAll(".sort-buttons button").forEach((button) => {
  button.addEventListener("click", () => {
    state.sortKey = button.dataset.sort;
    state.sortDir = Number(button.dataset.sortDir);
    renderRows();
  });
});

loadState();
setInterval(loadState, 3000);
