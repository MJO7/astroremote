"use strict";

const $ = (selector) => document.querySelector(selector);
const ui = {
  form: $("#sequence-form"), start: $("#start-button"), stop: $("#stop-button"),
  refresh: $("#refresh-camera"), cameraDot: $("#camera-dot"), cameraName: $("#camera-name"),
  cameraPort: $("#camera-port"),
  connection: $("#connection-label"), simulation: $("#simulation-badge"), alert: $("#alert"),
  exposure: $("#exposure"), interval: $("#interval"), shots: $("#shots"),
  iso: $("#iso"), aperture: $("#aperture"), isoHelp: $("#iso-help"), apertureHelp: $("#aperture-help"),
  storageDestination: $("#storage-destination"), storageHelp: $("#storage-help"),
  computerFolderField: $("#computer-folder-field"), outputRoot: $("#output-root"), outputSubdirectory: $("#output-subdirectory"),
  liveview: $("#liveview-button"), playback: $("#playback-button"), displayMode: $("#display-mode-label"), displayHelp: $("#display-help"),
  state: $("#state"), frameCount: $("#frame-count"), countdown: $("#countdown"),
  countdownLabel: $("#countdown-label"), exposureProgress: $("#exposure-progress"),
  sequenceProgress: $("#sequence-progress"), sequencePercent: $("#sequence-percent"),
  lastCompleted: $("#last-completed"), currentFile: $("#current-file"), nextFile: $("#next-file"),
  eventLog: $("#event-log"), debugLog: $("#debug-log"),
};

let cameraConnected = false;
let sequenceActive = false;
let helperConfigured = false;
let currentConfig = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* use status message */ }
    throw new Error(message);
  }
  return response.json();
}

function showAlert(message, offerRelease = false) {
  ui.alert.replaceChildren();
  ui.alert.append(document.createTextNode(message));
  if (offerRelease && helperConfigured) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "RELEASE macOS CAMERA SERVICE";
    button.addEventListener("click", releaseCamera);
    ui.alert.append(button);
  }
  ui.alert.classList.remove("hidden");
}

function clearAlert() { ui.alert.classList.add("hidden"); }

function fillSelect(select, help, property) {
  select.replaceChildren();
  const choices = property?.choices || [];
  const enabled = Boolean(property?.available && choices.length);
  if (enabled) {
    for (const value of choices) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = value === property.current;
      select.append(option);
    }
    help.textContent = "Reported by camera";
  } else {
    const option = document.createElement("option");
    option.textContent = "Unavailable";
    select.append(option);
    help.textContent = property?.message || "Not exposed by camera";
  }
  select.disabled = !enabled || sequenceActive;
  select.dataset.available = enabled ? "true" : "false";
}

function hasChoice(property, text) {
  return Boolean(property?.available && (property.choices || []).some((choice) => choice.toLowerCase().includes(text)));
}

function renderConfig(config) {
  currentConfig = config;
  fillSelect(ui.iso, ui.isoHelp, config.iso);
  fillSelect(ui.aperture, ui.apertureHelp, config.aperture);

  const macOption = ui.storageDestination.querySelector('option[value="computer"]');
  const cardOption = ui.storageDestination.querySelector('option[value="camera"]');
  macOption.disabled = !(hasChoice(config.capture_target, "internal ram") || hasChoice(config.capture_target, "sdram"));
  cardOption.disabled = !(hasChoice(config.capture_target, "memory card") || hasChoice(config.capture_target, "card"));
  if (ui.storageDestination.selectedOptions[0]?.disabled) {
    const available = [...ui.storageDestination.options].find((option) => !option.disabled);
    if (available) ui.storageDestination.value = available.value;
  }

  const liveViewAvailable = Boolean(config.viewfinder?.available);
  const playbackAvailable = cameraConnected;
  ui.liveview.dataset.available = liveViewAvailable ? "true" : "false";
  ui.playback.dataset.available = playbackAvailable ? "true" : "false";
  ui.displayHelp.textContent = liveViewAvailable
    ? "Live preview and read-only camera-card gallery are available"
    : "Camera gallery is available; this camera does not expose Live View controls";
  const live = String(config.viewfinder?.current) === "1";
  const playback = String(config.application_mode?.current || "").toLowerCase().includes("mode 1");
  ui.displayMode.textContent = live ? "LIVE VIEW" : (playback ? "PLAYBACK" : "REMOTE");
  updateStorageUI();
  setControls();
}

function updateStorageUI() {
  const onComputer = ui.storageDestination.value === "computer";
  ui.computerFolderField.classList.toggle("hidden", !onComputer);
  ui.storageHelp.textContent = onComputer
    ? "Download and verify each image on this Mac"
    : "Keep each image on the Nikon memory card; no Mac copy is created";
}

function renderCamera(camera) {
  cameraConnected = Boolean(camera.connected);
  ui.cameraName.textContent = camera.name || "No camera detected";
  ui.cameraPort.textContent = camera.port || "—";
  ui.cameraDot.classList.toggle("connected", cameraConnected);
  ui.connection.textContent = cameraConnected ? "CONNECTED" : "DISCONNECTED";
  ui.connection.classList.toggle("connected", cameraConnected);
  ui.simulation.classList.toggle("hidden", !camera.simulation);
  setControls();
  if (camera.message) showAlert(camera.message, camera.ownership_error);
  else if (cameraConnected) clearAlert();
}

function formatTime(seconds) {
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  if (hours) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

function renderStatus(status) {
  sequenceActive = Boolean(status.active);
  ui.state.textContent = status.state;
  ui.state.classList.toggle("active", sequenceActive || status.state === "ERROR");
  ui.frameCount.textContent = `${status.sequence_completed} / ${status.sequence_total} frames`;

  let remaining = 0;
  let label = "ready";
  if (status.state === "EXPOSING" || status.state === "STOPPING" && status.exposure_elapsed > 0) {
    remaining = Math.max(0, status.exposure_duration - status.exposure_elapsed);
    label = "remaining";
  } else if (status.state === "INTERVAL") {
    remaining = status.interval_remaining;
    label = "next exposure";
  } else if (status.state === "DOWNLOADING") {
    label = status.storage_destination === "camera" ? "saving to camera" : "downloading";
  } else if (status.state === "PREPARING") {
    label = "preparing camera";
  } else if (status.state === "ERROR") {
    label = "sequence error";
  }
  ui.countdown.textContent = formatTime(remaining);
  ui.countdownLabel.textContent = label;
  const exposurePct = status.exposure_duration > 0 ? Math.min(100, status.exposure_elapsed / status.exposure_duration * 100) : 0;
  const sequencePct = status.sequence_total > 0 ? Math.min(100, status.sequence_completed / status.sequence_total * 100) : 0;
  ui.exposureProgress.style.width = `${exposurePct}%`;
  ui.sequenceProgress.style.width = `${sequencePct}%`;
  ui.sequencePercent.textContent = `${Math.round(sequencePct)}%`;
  ui.lastCompleted.textContent = status.last_completed || "—";
  ui.currentFile.textContent = status.current_filename || "—";
  ui.nextFile.textContent = status.next_filename || "—";
  if (status.error) showAlert(status.error);
  setControls();
}

function setControls() {
  ui.start.disabled = !cameraConnected || sequenceActive;
  ui.stop.disabled = !sequenceActive;
  for (const element of [ui.exposure, ui.interval, ui.shots, ui.storageDestination]) element.disabled = sequenceActive;
  ui.outputSubdirectory.disabled = sequenceActive || ui.storageDestination.value !== "computer";
  for (const select of [ui.iso, ui.aperture]) select.disabled = sequenceActive || select.dataset.available !== "true";
  ui.refresh.disabled = sequenceActive;
  ui.liveview.disabled = sequenceActive || !cameraConnected || ui.liveview.dataset.available !== "true";
  ui.playback.disabled = sequenceActive || !cameraConnected || ui.playback.dataset.available !== "true";
}

function appendLog(container, entry, raw = false) {
  if (container.querySelector(".muted")) container.replaceChildren();
  const line = document.createElement("div");
  line.className = `log-line ${entry.level || ""}`;
  const time = document.createElement("time");
  time.textContent = new Date(entry.timestamp).toLocaleTimeString([], { hour12: false });
  const text = document.createElement("span");
  text.textContent = entry.message;
  if (raw) text.style.whiteSpace = "pre-wrap";
  line.append(time, text);
  container.append(line);
  while (container.children.length > 200) container.firstElementChild.remove();
  container.scrollTop = container.scrollHeight;
}

async function loadCamera() {
  const [runtime, camera, config] = await Promise.all([
    api("/api/runtime"), api("/api/camera"), api("/api/camera/config"),
  ]);
  helperConfigured = runtime.ptp_helper_configured;
  ui.outputRoot.textContent = `${runtime.output_root.replace(/\/$/, "")}/`;
  renderCamera(camera);
  renderConfig(config);
}

async function refreshCamera() {
  ui.refresh.disabled = true;
  try {
    const camera = await api("/api/camera/refresh", { method: "POST" });
    renderCamera(camera);
    const config = await api("/api/camera/config");
    renderConfig(config);
  } catch (error) { showAlert(error.message); }
  finally { setControls(); }
}

async function releaseCamera() {
  try {
    showAlert("Releasing macOS camera service…");
    const camera = await api("/api/camera/release", { method: "POST" });
    renderCamera(camera);
    await refreshCamera();
  } catch (error) { showAlert(error.message); }
}

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearAlert();
  const payload = {
    exposure: Number(ui.exposure.value), interval: Number(ui.interval.value), shots: Number(ui.shots.value),
    iso: ui.iso.dataset.available === "true" ? ui.iso.value : null,
    aperture: ui.aperture.dataset.available === "true" ? ui.aperture.value : null,
    storage_destination: ui.storageDestination.value,
    output_subdirectory: ui.outputSubdirectory.value.trim(),
  };
  try {
    renderStatus(await api("/api/sequence/start", { method: "POST", body: JSON.stringify(payload) }));
  } catch (error) { showAlert(error.message); }
});

ui.stop.addEventListener("click", async () => {
  ui.stop.disabled = true;
  try { renderStatus(await api("/api/sequence/stop", { method: "POST" })); }
  catch (error) { showAlert(error.message); }
});
ui.refresh.addEventListener("click", refreshCamera);
ui.storageDestination.addEventListener("change", () => { updateStorageUI(); setControls(); });

async function setCameraDisplay(mode) {
  clearAlert();
  ui.liveview.disabled = true;
  ui.playback.disabled = true;
  try {
    const config = await api("/api/camera/display", { method: "POST", body: JSON.stringify({ mode }) });
    renderConfig(config);
    showAlert(mode === "liveview" ? "Live View enabled on the Nikon display." : "Camera menus/playback mode enabled. Press the Nikon playback button.");
  } catch (error) { showAlert(error.message); }
  finally { setControls(); }
}

ui.liveview.addEventListener("click", () => { window.location.assign("/liveview"); });
ui.playback.addEventListener("click", () => { window.location.assign("/playback"); });

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "status") renderStatus(event.data);
      else if (event.type === "log") appendLog(ui.eventLog, event.data);
      else if (event.type === "debug") appendLog(ui.debugLog, event.data, true);
      else if (event.type === "camera") renderCamera(event.data);
      else if (event.type === "config") renderConfig(event.data);
    } catch (_) { /* Ignore malformed event and allow EventSource to continue. */ }
  };
  source.onerror = () => { /* EventSource reconnects automatically. */ };
}

async function initialize() {
  try {
    const [status, logs, debug] = await Promise.all([api("/api/status"), api("/api/logs"), api("/api/logs?debug=true")]);
    renderStatus(status);
    for (const entry of logs.logs) appendLog(ui.eventLog, entry);
    for (const entry of debug.logs) appendLog(ui.debugLog, entry, true);
    await loadCamera();
  } catch (error) { showAlert(`Cannot reach AstroRemote: ${error.message}`); }
  connectEvents();
}

initialize();
