"use strict";

// ── State ────────────────────────────────────────────────────
let profiles = [];
let activeProfile = null;  // {id, name, color}
let currentCard = null;    // card shown in scan result
let detailCard = null;     // card shown in detail overlay
let allCards = [];         // full collection for current profile
let sortMode = "newest";
let filterMode = "all";

const TYPE_DE = {
  Grass: "Pflanze", Fire: "Feuer", Water: "Wasser",
  Lightning: "Blitz", Psychic: "Psycho", Fighting: "Kampf",
  Darkness: "Dunkel", Metal: "Metall", Dragon: "Drache",
  Colorless: "Farblos", Fairy: "Fee",
};

// ── DOM refs ─────────────────────────────────────────────────
const video         = document.getElementById("video");
const canvas        = document.getElementById("canvas");
const captureBtn    = document.getElementById("capture-btn");
const manualNum     = document.getElementById("manual-num");
const manualTotal   = document.getElementById("manual-total");
const manualBtn     = document.getElementById("manual-btn");
const profileOverlay= document.getElementById("profile-overlay");
const profileButtons= document.getElementById("profile-buttons");
const app           = document.getElementById("app");
const profileSwitch = document.getElementById("profile-switch-btn");
const tabBtns       = document.querySelectorAll(".tab");
const tabScanner    = document.getElementById("tab-scanner");
const tabCollection = document.getElementById("tab-collection");
const loading       = document.getElementById("loading");
const cardInfo      = document.getElementById("card-info");
const cardImage     = document.getElementById("card-image");
const cardName      = document.getElementById("card-name");
const cardSet       = document.getElementById("card-set");
const cardNumber    = document.getElementById("card-number");
const cardOwned     = document.getElementById("card-owned");
const addBtn        = document.getElementById("add-to-collection-btn");
const multiMatch    = document.getElementById("multi-match");
const matchList     = document.getElementById("match-list");
const errorMsg      = document.getElementById("error-msg");
const debugSection  = document.getElementById("debug");
const debugOcr      = document.getElementById("debug-ocr");
const debugImage    = document.getElementById("debug-image");
const collectionCount = document.getElementById("collection-count");
const cardGrid      = document.getElementById("card-grid");
const detailOverlay = document.getElementById("detail-overlay");
const detailImage   = document.getElementById("detail-image");
const detailName    = document.getElementById("detail-name");
const detailSet     = document.getElementById("detail-set");
const detailNumber  = document.getElementById("detail-number");
const detailQty     = document.getElementById("detail-qty");
const detailMinus   = document.getElementById("detail-minus");
const detailPlus    = document.getElementById("detail-plus");
const detailClose   = document.getElementById("detail-close");
const detailRemove  = document.getElementById("detail-remove-btn");
const detailConfirm = document.getElementById("detail-confirm");
const detailYes     = document.getElementById("detail-confirm-yes");
const detailNo      = document.getElementById("detail-confirm-no");
const detailMeta    = document.getElementById("detail-meta");

// Scan-again buttons (there are three — one per result state)
["scan-again-btn","scan-again-btn-2","scan-again-btn-3"].forEach(id => {
  document.getElementById(id)?.addEventListener("click", resetScanner);
});

// ── Profiles ─────────────────────────────────────────────────
async function loadProfiles() {
  const res = await fetch("/profiles");
  profiles = await res.json();

  // Build profile picker buttons
  profileButtons.innerHTML = "";
  profiles.forEach(p => {
    const btn = document.createElement("button");
    btn.className = "profile-btn";
    btn.style.background = p.color;
    btn.innerHTML = `<span class="profile-avatar">🎮</span>${p.name}`;
    btn.addEventListener("click", () => selectProfile(p));
    profileButtons.appendChild(btn);
  });

  // Restore saved profile
  const savedId = localStorage.getItem("activeProfileId");
  const saved = profiles.find(p => p.id === savedId);
  if (saved) {
    selectProfile(saved, false);
  } else {
    profileOverlay.hidden = false;
  }
}

function selectProfile(profile, save = true) {
  activeProfile = profile;
  if (save) localStorage.setItem("activeProfileId", profile.id);
  profileOverlay.hidden = true;
  app.hidden = false;
  profileSwitch.textContent = `${profile.name} ▾`;
  profileSwitch.style.borderLeft = `4px solid ${profile.color}`;
  loadCollection();
}

profileSwitch.addEventListener("click", () => {
  profileOverlay.hidden = false;
  app.hidden = true;
});

// ── Tabs ─────────────────────────────────────────────────────
// Start on collection tab; camera only starts when scanner tab is opened.
tabScanner.hidden    = true;
tabCollection.hidden = false;
loadCollection();

tabBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    tabBtns.forEach(b => b.classList.toggle("active", b === btn));
    tabScanner.hidden    = tab !== "scanner";
    tabCollection.hidden = tab !== "collection";
    if (tab === "collection") loadCollection();
    if (tab === "scanner" && !video.srcObject) startCamera();
  });
});

// ── Camera ───────────────────────────────────────────────────
async function startCamera() {
  // Try progressively less restrictive constraints for device compatibility.
  const attempts = [
    { video: { facingMode: "environment", width: { ideal: 1280 } }, audio: false },
    { video: { width: { ideal: 1280 } }, audio: false },
    { video: true, audio: false },
  ];
  for (const constraints of attempts) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      video.srcObject = stream;
      await video.play();
      return;
    } catch (err) {
      if (constraints === attempts.at(-1)) {
        showError("Kamera nicht verfügbar: " + err.message);
        captureBtn.disabled = true;
      }
    }
  }
}

captureBtn.addEventListener("click", async () => {
  const vf = document.getElementById("viewfinder");
  const vfAspect = vf.offsetWidth / vf.offsetHeight;
  const vidW = video.videoWidth, vidH = video.videoHeight;
  const vidAspect = vidW / vidH;
  let sx, sy, sw, sh;
  if (vidAspect > vfAspect) {
    sh = vidH; sw = Math.round(vidH * vfAspect);
    sx = Math.round((vidW - sw) / 2); sy = 0;
  } else {
    sw = vidW; sh = Math.round(vidW / vfAspect);
    sx = 0; sy = Math.round((vidH - sh) / 2);
  }
  canvas.width = sw; canvas.height = sh;
  canvas.getContext("2d").drawImage(video, sx, sy, sw, sh, 0, 0, sw, sh);
  canvas.toBlob(async (blob) => {
    showLoading();
    try {
      const form = new FormData();
      form.append("file", blob, "card.jpg");
      const res = await fetch("/scan", { method: "POST", body: form });
      const data = await res.json();
      handleScanResult(data);
    } catch (err) {
      showError("Netzwerkfehler: " + err.message);
    }
  }, "image/jpeg", 0.9);
});

// ── Scan result ───────────────────────────────────────────────
function handleScanResult(data) {
  debugSection.hidden = false;
  debugOcr.textContent = JSON.stringify(data.ocr_raw);
  if (data.debug_image) {
    debugImage.src = "data:image/jpeg;base64," + data.debug_image;
  }
  if (data.error || !data.matches || data.matches.length === 0) {
    showError(data.error || "Keine Karte gefunden. Bessere Beleuchtung oder Karte ruhiger halten.");
    return;
  }
  if (data.matches.length === 1) {
    showCard(data.matches[0]);
  } else {
    showMultiMatch(data.matches);
  }
}

async function showCard(card) {
  hideResults();
  currentCard = card;

  cardName.textContent   = card.name;
  cardSet.textContent    = "Set: " + (card.set_name || card.set_id.toUpperCase());
  cardNumber.textContent = "Nummer: " + card.number;

  if (card.image_small) {
    cardImage.src = card.image_small;
    cardImage.hidden = false;
  } else {
    cardImage.hidden = true;
  }

  // Check ownership
  cardOwned.textContent = "";
  if (activeProfile) {
    try {
      const res = await fetch(`/collection/${activeProfile.id}/${card.id}`);
      const { quantity } = await res.json();
      cardOwned.textContent = quantity > 0
        ? `Du hast diese Karte: ${quantity}×`
        : "Noch nicht in deiner Sammlung";
    } catch (_) {}
  }

  cardInfo.hidden = false;
}

function showMultiMatch(matches) {
  hideResults();
  matchList.innerHTML = "";
  matches.forEach(card => {
    const li = document.createElement("li");
    if (card.image_small) {
      const img = document.createElement("img");
      img.src = card.image_small;
      img.style.cssText = "width:50px;border-radius:4px;flex-shrink:0";
      li.appendChild(img);
    }
    const span = document.createElement("span");
    span.textContent = `${card.name} — ${card.set_name || card.set_id.toUpperCase()} #${card.number}`;
    li.appendChild(span);
    li.addEventListener("click", () => showCard(card));
    matchList.appendChild(li);
  });
  multiMatch.hidden = false;
}

function showError(msg) {
  hideResults();
  errorMsg.textContent = msg;
  errorMsg.hidden = false;
  document.getElementById("scan-again-btn-3").hidden = false;
}

function showLoading() {
  hideResults();
  loading.hidden = false;
}

function hideResults() {
  loading.hidden = true;
  cardInfo.hidden = true;
  multiMatch.hidden = true;
  errorMsg.hidden = true;
  document.getElementById("scan-again-btn-3").hidden = true;
  debugSection.hidden = true;
}

function resetScanner() {
  hideResults();
  currentCard = null;
  manualNum.value = "";
  manualTotal.value = "";
}

// ── Add to collection ────────────────────────────────────────
addBtn.addEventListener("click", async () => {
  if (!currentCard || !activeProfile) return;
  addBtn.disabled = true;
  try {
    const res = await fetch(`/collection/${activeProfile.id}/${currentCard.id}/add`, { method: "POST" });
    const { quantity } = await res.json();
    cardOwned.textContent = `Du hast diese Karte: ${quantity}×`;
    addBtn.textContent = "✓ Hinzugefügt!";
    addBtn.style.background = "#1a7a45";
    setTimeout(() => {
      addBtn.textContent = "➕ Zur Sammlung";
      addBtn.style.background = "";
      addBtn.disabled = false;
    }, 1500);
  } catch (err) {
    addBtn.disabled = false;
    showError("Fehler: " + err.message);
  }
});

// ── Manual lookup ─────────────────────────────────────────────
async function doManualLookup() {
  const num = manualNum.value.trim(), total = manualTotal.value.trim();
  if (!num) return;
  showLoading();
  try {
    const res  = await fetch("/lookup?number=" + encodeURIComponent(total ? `${num}/${total}` : num));
    const data = await res.json();
    if (!res.ok) { showError(data.detail || "Suche fehlgeschlagen"); return; }
    handleScanResult(data);
  } catch (err) {
    showError("Netzwerkfehler: " + err.message);
  }
}
manualBtn.addEventListener("click", doManualLookup);
[manualNum, manualTotal].forEach(el =>
  el.addEventListener("keydown", e => { if (e.key === "Enter") doManualLookup(); })
);
manualNum.addEventListener("keydown", e => {
  if (e.key === "/" && manualNum.value.trim()) { e.preventDefault(); manualTotal.focus(); }
});

// ── Collection view ───────────────────────────────────────────
async function loadCollection() {
  if (!activeProfile) return;
  cardGrid.innerHTML = '<div style="padding:1rem;color:#888">Wird geladen…</div>';
  try {
    const res = await fetch(`/collection/${activeProfile.id}`);
    allCards = await res.json();
    collectionCount.textContent = `${activeProfile.name} · ${allCards.length} Karten`;
    applyChips();
  } catch (err) {
    cardGrid.innerHTML = `<div style="padding:1rem;color:#f88">Fehler: ${err.message}</div>`;
  }
}

function applyChips() {
  let cards = [...allCards];
  if (filterMode === "doubles")  cards = cards.filter(c => c.quantity > 1);
  else if (filterMode === "pokemon") cards = cards.filter(c => c.category === "Pokemon");
  else if (filterMode === "trainer") cards = cards.filter(c => c.category === "Trainer");
  else if (filterMode === "energy")  cards = cards.filter(c => c.category === "Energy");
  if (sortMode === "name-asc")  cards.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  else if (sortMode === "name-desc") cards.sort((a, b) => (b.name || "").localeCompare(a.name || ""));
  else if (sortMode === "quantity")  cards.sort((a, b) => b.quantity - a.quantity);
  // "newest" keeps server order (added_at DESC)
  renderGrid(cards);
}

document.querySelectorAll("#sort-chips .chip").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#sort-chips .chip").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    sortMode = btn.dataset.sort;
    applyChips();
  });
});

document.querySelectorAll("#filter-chips .chip").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#filter-chips .chip").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    filterMode = btn.dataset.filter;
    applyChips();
  });
});

function renderGrid(cards) {
  cardGrid.innerHTML = "";
  if (cards.length === 0) {
    cardGrid.innerHTML = '<div style="padding:2rem;text-align:center;color:#888">Noch keine Karten in deiner Sammlung.<br>Scanne eine Karte und tippe ➕!</div>';
    return;
  }
  cards.forEach(card => {
    const div = document.createElement("div");
    div.className = "grid-card";
    div.dataset.cardId = card.id;

    if (card.image_small) {
      const img = document.createElement("img");
      img.src = card.image_small;
      img.alt = card.name;
      div.appendChild(img);

      const displayName = card.name_de || card.name_en;
      if (displayName) {
        const label = document.createElement("div");
        label.className = "grid-card-name";
        label.textContent = displayName;
        div.appendChild(label);
      }
    }

    if (card.quantity > 1) {
      const badge = document.createElement("span");
      badge.className = "qty-badge";
      badge.textContent = card.quantity + "×";
      div.appendChild(badge);
    }

    div.addEventListener("click", () => openDetail(card));
    cardGrid.appendChild(div);
  });
}

// ── Card detail overlay ───────────────────────────────────────
function openDetail(card) {
  detailCard = card;
  detailImage.src = card.image_small || "";
  detailName.textContent   = card.name;
  detailSet.textContent    = card.set_name || card.set_id?.toUpperCase() || "";
  detailNumber.textContent = "Nummer: " + card.number;
  detailQty.textContent    = card.quantity;
  detailConfirm.hidden     = true;

  const parts = [];
  if (card.hp) parts.push(`❤️ ${card.hp} KP`);
  if (card.types?.length) parts.push(card.types.map(t => TYPE_DE[t] || t).join(", "));
  if (card.rarity) parts.push(card.rarity);
  if (card.stage)  parts.push(card.stage);
  detailMeta.textContent = parts.join(" · ");

  detailOverlay.hidden = false;
}

detailClose.addEventListener("click", () => { detailOverlay.hidden = true; });

detailPlus.addEventListener("click", async () => {
  if (!detailCard || !activeProfile) return;
  const res = await fetch(`/collection/${activeProfile.id}/${detailCard.id}/add`, { method: "POST" });
  const { quantity } = await res.json();
  detailCard.quantity = quantity;
  detailQty.textContent = quantity;
  refreshGridCard(detailCard);
});

detailMinus.addEventListener("click", async () => {
  if (!detailCard || !activeProfile) return;
  const res = await fetch(`/collection/${activeProfile.id}/${detailCard.id}/remove`, { method: "POST" });
  const { quantity } = await res.json();
  if (quantity === 0) {
    detailOverlay.hidden = true;
    allCards = allCards.filter(c => c.id !== detailCard.id);
    removeGridCard(detailCard.id);
    updateCollectionCount(-1);
  } else {
    detailCard.quantity = quantity;
    detailQty.textContent = quantity;
    refreshGridCard(detailCard);
  }
});

detailRemove.addEventListener("click", () => { detailConfirm.hidden = false; });
detailNo.addEventListener("click",     () => { detailConfirm.hidden = true; });
detailYes.addEventListener("click", async () => {
  if (!detailCard || !activeProfile) return;
  // Remove all copies
  for (let i = 0; i < detailCard.quantity; i++) {
    await fetch(`/collection/${activeProfile.id}/${detailCard.id}/remove`, { method: "POST" });
  }
  detailOverlay.hidden = true;
  allCards = allCards.filter(c => c.id !== detailCard.id);
  removeGridCard(detailCard.id);
  updateCollectionCount(-1);
});

function refreshGridCard(card) {
  const div = cardGrid.querySelector(`[data-card-id="${card.id}"]`);
  if (!div) return;
  let badge = div.querySelector(".qty-badge");
  if (card.quantity > 1) {
    if (!badge) { badge = document.createElement("span"); badge.className = "qty-badge"; div.appendChild(badge); }
    badge.textContent = card.quantity + "×";
  } else if (badge) {
    badge.remove();
  }
}

function removeGridCard(cardId) {
  cardGrid.querySelector(`[data-card-id="${cardId}"]`)?.remove();
}

function updateCollectionCount(delta) {
  const m = collectionCount.textContent.match(/(\d+) Karten/);
  if (m) {
    const n = Math.max(0, parseInt(m[1]) + delta);
    collectionCount.textContent = collectionCount.textContent.replace(/\d+ Karten/, `${n} Karten`);
  }
}

// ── Boot ──────────────────────────────────────────────────────
loadProfiles();
