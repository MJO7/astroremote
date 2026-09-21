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
let started = false;
let stopping = false;
const clientId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
const zoomSteps = [1, 2, 4, 8];
let zoomIndex = 0;
let pan = { x: 50, y: 50 };
let dragging = false;

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

function renderZoom() {
  const zoom = zoomSteps[zoomIndex];
  preview.style.setProperty("--zoom", zoom);
  preview.style.setProperty("--pan-x", `${pan.x}%`);
  preview.style.setProperty("--pan-y", `${pan.y}%`);
  focusBox.style.setProperty("--x", `${pan.x}%`);
  focusBox.style.setProperty("--y", `${pan.y}%`);
  focusBox.setAttribute("aria-valuetext", `${zoom} times zoom at ${Math.round(pan.x)}, ${Math.round(pan.y)}`);
  zoomLevel.textContent = `${zoom}×`;
  zoomIn.disabled = zoomIndex === zoomSteps.length - 1;
  zoomOut.disabled = zoomIndex === 0;
}

function setPan(x, y) {
  pan.x = Math.max(12, Math.min(88, x));
  pan.y = Math.max(12, Math.min(88, y));
  renderZoom();
}

function panFromPointer(event) {
  const bounds = stage.getBoundingClientRect();
  setPan((event.clientX - bounds.left) / bounds.width * 100, (event.clientY - bounds.top) / bounds.height * 100);
}

async function start() {
  errorBox.classList.add("hidden");
  loading.classList.remove("hidden");
  badge.classList.add("waiting");
  badge.lastElementChild.textContent = "STARTING";
  preview.removeAttribute("src");
  try {
    const [camera] = await Promise.all([
      request("/api/camera"),
      request(`/api/liveview/start?client_id=${encodeURIComponent(clientId)}`, { method: "POST" }),
    ]);
    cameraName.textContent = camera.name || "Connected camera";
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
});
preview.addEventListener("error", () => showError("The camera stream ended. Check the USB connection and try again."));
document.querySelector("#retry").addEventListener("click", start);
zoomIn.addEventListener("click", () => { if (zoomIndex < zoomSteps.length - 1) zoomIndex += 1; renderZoom(); });
zoomOut.addEventListener("click", () => { if (zoomIndex > 0) zoomIndex -= 1; renderZoom(); });
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
renderZoom();
start();
