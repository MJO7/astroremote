"use strict";

const preview = document.querySelector("#preview");
const loading = document.querySelector("#loading");
const errorBox = document.querySelector("#error");
const errorDetail = document.querySelector("#error-detail");
const badge = document.querySelector("#live-badge");
const cameraName = document.querySelector("#camera-name");
const stage = document.querySelector(".stage");
const focusBox = document.querySelector("#focus-box");
const zoomLevel = document.querySelector("#zoom-level");
const zoomIn = document.querySelector("#zoom-in");
const zoomOut = document.querySelector("#zoom-out");
const exposureSlider = document.querySelector("#preview-exposure");
const contrastSlider = document.querySelector("#preview-contrast");
const saturationSlider = document.querySelector("#preview-saturation");
const lensFocus = document.querySelector("#lens-focus");
const focusToggle = document.querySelector("#focus-toggle");
const focusPanel = document.querySelector("#focus-panel");
const focusStep = document.querySelector("#focus-step");
const focusStepValue = document.querySelector("#focus-step-value");
const focusStatus = document.querySelector("#focus-status");
const focusButtons = [...document.querySelectorAll("[data-focus-direction]")];
let started = false;
let stopping = false;
let focusAvailable = false;
let focusBusy = false;
let heldFocusDirection = null;
const clientId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const zoomSteps = [1, 2, 4, 8];
let zoomIndex = 0;
let pan = { x: 50, y: 50 };
let dragging = false;
const focusAmounts = { 1: 100, 2: 600, 3: 2000 };
const focusStepNames = { 1: "FINE", 2: "MEDIUM", 3: "COARSE" };

async function request(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* keep fallback */ }
    throw new Error(detail);
  }
  return response.json();
}

function showError(message) {
  loading.classList.add("hidden");
  preview.classList.remove("ready");
  errorDetail.textContent = message;
  errorBox.classList.remove("hidden");
  badge.classList.add("waiting");
  badge.lastElementChild.textContent = "OFFLINE";
}

function visibleImageBounds() {
  const stageBounds = stage.getBoundingClientRect();
  const naturalWidth = preview.naturalWidth || stageBounds.width;
  const naturalHeight = preview.naturalHeight || stageBounds.height;
  const imageRatio = naturalWidth / Math.max(naturalHeight, 1);
  const stageRatio = stageBounds.width / Math.max(stageBounds.height, 1);
  let width = stageBounds.width;
  let height = stageBounds.height;
  let left = 0;
  let top = 0;
  if (stageRatio > imageRatio) {
    width = height * imageRatio;
    left = (stageBounds.width - width) / 2;
  } else {
    height = width / imageRatio;
    top = (stageBounds.height - height) / 2;
  }
  return { stageBounds, width, height, left, top };
}

function renderZoom() {
  const zoom = zoomSteps[zoomIndex];
  const bounds = visibleImageBounds();
  const screenX = bounds.left + pan.x / 100 * bounds.width;
  const screenY = bounds.top + pan.y / 100 * bounds.height;
  preview.style.setProperty("--zoom", zoom);
  preview.style.setProperty("--pan-x", `${screenX / Math.max(bounds.stageBounds.width, 1) * 100}%`);
  preview.style.setProperty("--pan-y", `${screenY / Math.max(bounds.stageBounds.height, 1) * 100}%`);
  focusBox.style.setProperty("--x", `${screenX}px`);
  focusBox.style.setProperty("--y", `${screenY}px`);
  focusBox.setAttribute("aria-valuetext", `${zoom} times zoom at ${Math.round(pan.x)}, ${Math.round(pan.y)}`);
  zoomLevel.textContent = `${zoom}×`;
  zoomIn.disabled = zoomIndex === zoomSteps.length - 1;
  zoomOut.disabled = zoomIndex === 0;
}

function setPan(x, y) {
  const bounds = visibleImageBounds();
  const halfWidth = Math.min(50, focusBox.offsetWidth / 2 / Math.max(bounds.width, 1) * 100);
  const halfHeight = Math.min(50, focusBox.offsetHeight / 2 / Math.max(bounds.height, 1) * 100);
  pan.x = Math.max(halfWidth, Math.min(100 - halfWidth, x));
  pan.y = Math.max(halfHeight, Math.min(100 - halfHeight, y));
  renderZoom();
}

function panFromPointer(event) {
  const bounds = visibleImageBounds();
  setPan(
    (event.clientX - bounds.stageBounds.left - bounds.left) / Math.max(bounds.width, 1) * 100,
    (event.clientY - bounds.stageBounds.top - bounds.top) / Math.max(bounds.height, 1) * 100,
  );
}

function renderPreviewAdjustments() {
  const exposure = Number(exposureSlider.value);
  const contrast = Number(contrastSlider.value);
  const saturation = Number(saturationSlider.value);
  preview.style.setProperty("--preview-brightness", 2 ** exposure);
  preview.style.setProperty("--preview-contrast", contrast / 100);
  preview.style.setProperty("--preview-saturation", saturation / 100);
  document.querySelector("#exposure-value").textContent = `${exposure > 0 ? "+" : ""}${exposure.toFixed(1)} EV`;
  document.querySelector("#contrast-value").textContent = `${contrast}%`;
  document.querySelector("#saturation-value").textContent = `${saturation}%`;
}

function resetPreview() {
  exposureSlider.value = "0";
  contrastSlider.value = "100";
  saturationSlider.value = "100";
  zoomIndex = 0;
  pan = { x: 50, y: 50 };
  renderPreviewAdjustments();
  setPan(50, 50);
}

async function start() {
  errorBox.classList.add("hidden");
  loading.classList.remove("hidden");
  badge.classList.add("waiting");
  badge.lastElementChild.textContent = "STARTING";
  preview.removeAttribute("src");
  try {
    const [camera, config] = await Promise.all([
      request("/api/camera"),
      request("/api/camera/config"),
      request(`/api/liveview/start?client_id=${encodeURIComponent(clientId)}`, { method: "POST" }),
    ]);
    cameraName.textContent = camera.name || "Connected camera";
    focusAvailable = Boolean(
      config.manual_focus?.available
      && Number.isFinite(config.manual_focus?.minimum)
      && Number.isFinite(config.manual_focus?.maximum)
    );
    lensFocus.classList.toggle("unavailable", !focusAvailable);
    focusButtons.forEach((button) => { button.disabled = !focusAvailable; });
    focusStatus.textContent = focusAvailable ? "READY" : "NOT SUPPORTED BY CAMERA";
    focusStatus.classList.toggle("error", !focusAvailable);
    preview.src = `/api/liveview/stream?t=${Date.now()}`;
    preview.classList.add("ready");
    focusBox.classList.add("visible");
    loading.classList.add("hidden");
    badge.classList.remove("waiting");
    badge.lastElementChild.textContent = "LIVE";
    started = true;
  } catch (error) { showError(error.message); }
}

preview.addEventListener("load", () => {
  loading.classList.add("hidden");
  errorBox.classList.add("hidden");
  preview.classList.add("ready");
  badge.classList.remove("waiting");
  badge.lastElementChild.textContent = "LIVE";
  setPan(pan.x, pan.y);
});
preview.addEventListener("error", () => {
  if (!focusBusy) showError("The camera stream ended. Check the USB connection and try again.");
});
document.querySelector("#retry").addEventListener("click", start);
zoomIn.addEventListener("click", () => { if (zoomIndex < zoomSteps.length - 1) zoomIndex += 1; renderZoom(); });
zoomOut.addEventListener("click", () => { if (zoomIndex > 0) zoomIndex -= 1; renderZoom(); });
[exposureSlider, contrastSlider, saturationSlider].forEach((slider) => slider.addEventListener("input", renderPreviewAdjustments));
document.querySelector("#reset-preview").addEventListener("click", resetPreview);
focusToggle.addEventListener("click", () => {
  const open = lensFocus.classList.toggle("open");
  focusToggle.setAttribute("aria-expanded", String(open));
  focusPanel.inert = !open;
});
focusStep.addEventListener("input", () => {
  const step = Number(focusStep.value);
  focusStepValue.textContent = `${focusStepNames[step]} · ${focusAmounts[step]}`;
});

async function driveFocus(direction) {
  if (!focusAvailable || focusBusy || !started) return;
  focusBusy = true;
  lensFocus.classList.add("busy");
  focusButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.focusDirection === direction);
  });
  const step = Number(focusStep.value);
  focusStatus.classList.remove("error");
  focusStatus.textContent = `MOVING ${direction.toUpperCase()}…`;
  badge.classList.add("waiting");
  badge.lastElementChild.textContent = "FOCUSING";
  try {
    const result = await request("/api/liveview/focus", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction, step }),
    });
    focusStatus.textContent = `${direction.toUpperCase()} · ${Math.abs(result.amount)} STEPS`;
  } catch (error) {
    focusStatus.textContent = error.message;
    focusStatus.classList.add("error");
  } finally {
    if (started) preview.src = `/api/liveview/stream?t=${Date.now()}`;
    focusBusy = false;
    lensFocus.classList.remove("busy");
    focusButtons.forEach((button) => {
      button.classList.remove("active");
    });
    badge.classList.remove("waiting");
    badge.lastElementChild.textContent = "LIVE";
  }
}

async function repeatFocus(direction) {
  do {
    await driveFocus(direction);
    if (heldFocusDirection === direction) await new Promise((resolve) => setTimeout(resolve, 140));
  } while (heldFocusDirection === direction);
}

focusButtons.forEach((button) => {
  button.addEventListener("pointerdown", (event) => {
    if (button.disabled || event.button !== 0) return;
    event.preventDefault();
    heldFocusDirection = button.dataset.focusDirection;
    button.setPointerCapture?.(event.pointerId);
    repeatFocus(heldFocusDirection);
  });
  const stopHolding = () => { heldFocusDirection = null; };
  button.addEventListener("pointerup", stopHolding);
  button.addEventListener("pointercancel", stopHolding);
  button.addEventListener("lostpointercapture", stopHolding);
  button.addEventListener("click", (event) => {
    if (event.detail === 0) driveFocus(button.dataset.focusDirection);
  });
});
focusBox.addEventListener("pointerdown", (event) => {
  dragging = true;
  focusBox.classList.add("dragging");
  focusBox.setPointerCapture(event.pointerId);
  panFromPointer(event);
});
focusBox.addEventListener("pointermove", (event) => { if (dragging) panFromPointer(event); });
focusBox.addEventListener("pointerup", () => { dragging = false; focusBox.classList.remove("dragging"); });
focusBox.addEventListener("pointercancel", () => { dragging = false; focusBox.classList.remove("dragging"); });
document.querySelectorAll("[data-move]").forEach((button) => button.addEventListener("click", () => {
  const direction = button.dataset.move;
  if (direction === "center") setPan(50, 50);
  else setPan(pan.x + (direction === "left" ? -8 : direction === "right" ? 8 : 0), pan.y + (direction === "up" ? -8 : direction === "down" ? 8 : 0));
}));
document.addEventListener("keydown", (event) => {
  if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    setPan(pan.x + (event.key === "ArrowLeft" ? -5 : event.key === "ArrowRight" ? 5 : 0), pan.y + (event.key === "ArrowUp" ? -5 : event.key === "ArrowDown" ? 5 : 0));
  } else if (event.key === "+" || event.key === "=") zoomIn.click();
  else if (event.key === "-" || event.key === "_") zoomOut.click();
});
document.querySelector("#fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.();
});

function stop() {
  if (!started || stopping) return;
  stopping = true;
  navigator.sendBeacon(`/api/liveview/stop?client_id=${encodeURIComponent(clientId)}`);
}

document.querySelector("#back").addEventListener("click", stop);
window.addEventListener("pagehide", stop);
window.addEventListener("resize", () => setPan(pan.x, pan.y));
renderPreviewAdjustments();
setPan(50, 50);
start();
