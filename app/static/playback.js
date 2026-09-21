"use strict";

const gallery = document.querySelector("#gallery");
const loading = document.querySelector("#loading");
const empty = document.querySelector("#empty");
const errorBox = document.querySelector("#error");
const errorDetail = document.querySelector("#error-detail");
const summary = document.querySelector("#summary");
const lightbox = document.querySelector("#lightbox");
const viewerImage = document.querySelector("#viewer-image");
let items = [];
let currentIndex = -1;

async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* keep fallback */ }
    throw new Error(detail);
  }
  return response.json();
}

function thumbnailUrl(item) {
  return `/api/gallery/thumbnail?path=${encodeURIComponent(item.path)}`;
}

const imageObserver = new IntersectionObserver((entries, observer) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const image = entry.target;
    image.src = image.dataset.src;
    observer.unobserve(image);
  }
}, { rootMargin: "500px" });

function makeCard(item, index) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "card";
  card.setAttribute("aria-label", `View ${item.name}`);
  const thumb = document.createElement("span");
  thumb.className = "thumb";
  if (item.previewable) {
    const image = document.createElement("img");
    image.alt = "";
    image.loading = "lazy";
    image.dataset.src = thumbnailUrl(item);
    image.addEventListener("load", () => image.classList.add("loaded"));
    image.addEventListener("error", () => thumb.classList.add("unavailable"));
    thumb.append(image);
    imageObserver.observe(image);
  } else thumb.classList.add("unavailable");
  const info = document.createElement("span");
  info.className = "card-info";
  const name = document.createElement("strong");
  name.textContent = item.name;
  const folder = document.createElement("small");
  folder.textContent = item.folder;
  info.append(name, folder);
  card.append(thumb, info);
  card.addEventListener("click", () => openViewer(index));
  return card;
}

function openViewer(index) {
  const item = items[index];
  if (!item?.previewable) return;
  currentIndex = index;
  document.querySelector("#viewer-name").textContent = item.name;
  document.querySelector("#viewer-count").textContent = `${index + 1} / ${items.length}`;
  document.querySelector("#viewer-folder").textContent = item.folder;
  viewerImage.src = thumbnailUrl(item);
  document.querySelector("#previous").disabled = index <= 0;
  document.querySelector("#next").disabled = index >= items.length - 1;
  lightbox.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeViewer() {
  lightbox.classList.add("hidden");
  viewerImage.removeAttribute("src");
  document.body.style.overflow = "";
}

async function loadGallery() {
  loading.classList.remove("hidden");
  empty.classList.add("hidden");
  errorBox.classList.add("hidden");
  gallery.replaceChildren();
  try {
    items = await api("/api/gallery");
    summary.textContent = `${items.length} file${items.length === 1 ? "" : "s"} · newest first`;
    for (const [index, item] of items.entries()) gallery.append(makeCard(item, index));
    empty.classList.toggle("hidden", items.length !== 0);
  } catch (error) {
    errorDetail.textContent = error.message;
    errorBox.classList.remove("hidden");
    summary.textContent = "Camera unavailable";
  } finally { loading.classList.add("hidden"); }
}

document.querySelector("#refresh").addEventListener("click", loadGallery);
document.querySelector("#retry").addEventListener("click", loadGallery);
document.querySelector("#close").addEventListener("click", closeViewer);
document.querySelector("#previous").addEventListener("click", () => openViewer(currentIndex - 1));
document.querySelector("#next").addEventListener("click", () => openViewer(currentIndex + 1));
document.addEventListener("keydown", (event) => {
  if (lightbox.classList.contains("hidden")) return;
  if (event.key === "Escape") closeViewer();
  else if (event.key === "ArrowLeft" && currentIndex > 0) openViewer(currentIndex - 1);
  else if (event.key === "ArrowRight" && currentIndex < items.length - 1) openViewer(currentIndex + 1);
});

loadGallery();
