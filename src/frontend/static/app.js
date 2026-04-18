"use strict";

const video      = document.getElementById("video");
const canvas     = document.getElementById("canvas");
const captureBtn = document.getElementById("capture-btn");
const scanAgain  = document.getElementById("scan-again-btn");
const scanner    = document.getElementById("scanner");
const result     = document.getElementById("result");
const loading    = document.getElementById("loading");
const cardInfo   = document.getElementById("card-info");
const multiMatch = document.getElementById("multi-match");
const matchList  = document.getElementById("match-list");
const errorMsg   = document.getElementById("error-msg");

// --- Camera setup ---
async function startCamera() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1280 } },
      audio: false,
    });
    video.srcObject = stream;
  } catch (err) {
    showError("Camera not available: " + err.message);
    captureBtn.disabled = true;
  }
}

// --- Capture & scan ---
captureBtn.addEventListener("click", async () => {
  canvas.width  = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);

  canvas.toBlob(async (blob) => {
    showLoading();
    try {
      const form = new FormData();
      form.append("file", blob, "card.jpg");
      const res  = await fetch("/scan", { method: "POST", body: form });
      const data = await res.json();
      handleScanResult(data);
    } catch (err) {
      showError("Network error: " + err.message);
    }
  }, "image/jpeg", 0.9);
});

scanAgain.addEventListener("click", () => {
  result.hidden = true;
  scanner.hidden = false;
});

// --- Result handling ---
function handleScanResult(data) {
  if (data.error || data.matches.length === 0) {
    showError(data.error || "No card found. Try better lighting or hold the card steadier.");
    return;
  }
  if (data.matches.length === 1) {
    showCard(data.matches[0]);
  } else {
    showMultiMatch(data.matches);
  }
}

function showCard(card) {
  hideAll();
  document.getElementById("card-name").textContent   = card.name;
  document.getElementById("card-set").textContent    = "Set: " + card.set_id.toUpperCase();
  document.getElementById("card-number").textContent = "Number: " + card.number;
  document.getElementById("card-hp").textContent     = card.hp ? "HP: " + card.hp : "";
  document.getElementById("card-types").textContent  = card.types ? "Type: " + JSON.parse(card.types).join(", ") : "";
  document.getElementById("card-rarity").textContent = card.rarity ? "Rarity: " + card.rarity : "";

  const img = document.getElementById("card-image");
  if (card.image_small) {
    img.src = card.image_small;
    img.hidden = false;
  } else {
    img.hidden = true;
  }

  cardInfo.hidden = false;
  result.hidden = false;
  scanner.hidden = true;
}

function showMultiMatch(matches) {
  hideAll();
  matchList.innerHTML = "";
  matches.forEach(card => {
    const li = document.createElement("li");
    li.textContent = `${card.name} — ${card.set_id.toUpperCase()} #${card.number}`;
    li.addEventListener("click", () => showCard(card));
    matchList.appendChild(li);
  });
  multiMatch.hidden = false;
  result.hidden = false;
  scanner.hidden = true;
}

function showLoading() {
  hideAll();
  loading.hidden = false;
  result.hidden = false;
  scanner.hidden = true;
}

function showError(msg) {
  hideAll();
  errorMsg.textContent = msg;
  errorMsg.hidden = false;
  result.hidden = false;
  scanner.hidden = true;
}

function hideAll() {
  loading.hidden = true;
  cardInfo.hidden = true;
  multiMatch.hidden = true;
  errorMsg.hidden = true;
}

// --- Start ---
startCamera();
