function toastStack() {
  let el = document.getElementById("toast-stack");
  if (el) return el;
  el = document.createElement("div");
  el.id = "toast-stack";
  el.className = "toast-stack";
  document.body.appendChild(el);
  return el;
}

function showStatus(text, ok) {
  const stack = toastStack();
  const item = document.createElement("div");
  item.className = "toast " + (ok ? "ok" : "error");
  item.innerHTML =
    '<div class="toast-title">' + (ok ? "Готово" : "Ошибка") + "</div>" +
    '<div class="toast-text"></div>' +
    '<div class="toast-bar"><i></i></div>';
  item.querySelector(".toast-text").textContent = text;
  stack.appendChild(item);
  setTimeout(() => {
    item.classList.add("hiding");
    setTimeout(() => item.remove(), 360);
  }, 5000);
}

document.querySelectorAll("[data-toast]").forEach((el) => {
  const text = (el.getAttribute("data-toast") || "").trim();
  if (text) showStatus(text, el.hasAttribute("data-toast-ok"));
});

function openModal(html) {
  document.getElementById("modal-content").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
}

function qrUrl(link) {
  return (
    "https://api.qrserver.com/v1/create-qr-code/?size=240x240&margin=0&data=" +
    encodeURIComponent(link)
  );
}

function deviceModal(link, title, subtitle) {
  return `
    <h3>${title}</h3>
    <p class="muted">${subtitle}</p>
    <img class="qr" src="${qrUrl(link)}" alt="QR-код">
    <code class="vless-link">${link}</code>
    <button class="btn btn-gradient btn-block" data-copy="${link}">Копировать ссылку</button>`;
}

document.getElementById("burger")?.addEventListener("click", () => {
  document.getElementById("nav-links")?.classList.toggle("open");
});

function tariffPickerModal() {
  return `
    <h3>Подключить устройство</h3>
    <p class="muted">Выберите тариф. После подключения появится ссылка vless:// и QR-код.</p>
    <div class="tariff-picker">
      <label class="tariff-option"><input type="radio" name="new-tariff" value="lite"><b>Лайт</b><span>10₽/день · 5 ГБ</span></label>
      <label class="tariff-option"><input type="radio" name="new-tariff" value="standard" checked><b>Стандарт</b><span>25₽/день · 15 ГБ</span></label>
      <label class="tariff-option"><input type="radio" name="new-tariff" value="pro"><b>Про</b><span>50₽/день · 40 ГБ</span></label>
      <label class="tariff-option"><input type="radio" name="new-tariff" value="business"><b>Бизнес</b><span>100₽/день · 100 ГБ</span></label>
    </div>
    <button class="btn btn-gradient btn-block" id="confirm-add-device">Получить vless:// ссылку</button>`;
}

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  return { resp, data };
}

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (target.id === "modal") closeModal();

  if (target.dataset.copy) {
    try {
      await navigator.clipboard.writeText(target.dataset.copy);
      const old = target.textContent;
      target.textContent = "Скопировано";
      showStatus("Ссылка vless:// скопирована", true);
      setTimeout(() => (target.textContent = old), 1500);
    } catch (_) {
      showStatus("Не удалось скопировать — выделите ссылку вручную", false);
    }
  }

  if (target.dataset.qr) {
    openModal(
      deviceModal(target.dataset.qr, target.dataset.name || "Устройство",
        "Отсканируйте QR-код в приложении или скопируйте ссылку:")
    );
  }

  if (target.dataset.del) {
    if (!confirm("Удалить устройство?")) return;
    try {
      const resp = await fetch("/device/" + target.dataset.del, { method: "DELETE" });
      const data = await resp.json();
      if (data.success) {
        showStatus("Устройство удалено", true);
        setTimeout(() => location.reload(), 700);
      } else showStatus(data.error || "Ошибка удаления", false);
    } catch (err) {
      showStatus("Ошибка сети: " + err, false);
    }
  }

  if (target.id === "trial") {
    target.disabled = true;
    try {
      const { data } = await postJson("/trial", {});
      if (data.success) {
        location.reload();
        return;
      }
      showStatus(data.error || data.message || "Не удалось активировать", false);
    } catch (err) {
      showStatus("Ошибка сети: " + err, false);
    }
    target.disabled = false;
  }

  if (target.id === "add-device") {
    openModal(tariffPickerModal());
  }

  if (target.id === "confirm-add-device") {
    const picked = document.querySelector('input[name="new-tariff"]:checked');
    const tariff = picked ? picked.value : "standard";
    target.disabled = true;
    const label = target.textContent;
    target.textContent = "Создаём ссылку…";
    try {
      const { data } = await postJson("/device", { tariff });
      if (data.success && data.link) {
        closeModal();
        openModal(
          deviceModal(data.link, "Устройство подключено",
            "Импортируйте ссылку vless:// в приложение или отсканируйте QR:")
        );
        showStatus("Устройство создано. Скопируйте vless:// ссылку.", true);
      } else {
        showStatus(data.error || "Не удалось создать устройство", false);
      }
    } catch (err) {
      showStatus("Ошибка сети: " + err, false);
    }
    target.disabled = false;
    target.textContent = label;
  }

  if (target.id === "topup") {
    const amountEl = document.getElementById("amount");
    const amount = amountEl ? parseInt(amountEl.value, 10) : 0;
    if (!amount || amount < 100) {
      showStatus("Минимальная сумма пополнения — 100 ₽", false);
      return;
    }
    target.disabled = true;
    const label = target.textContent;
    target.textContent = "Открываем оплату…";
    try {
      const { data } = await postJson("/pay", { amount: amount });
      if (data.success && data.url) {
        window.location.href = data.url;
        return;
      }
      showStatus(data.error || "Не удалось создать платёж", false);
    } catch (err) {
      showStatus("Ошибка сети: " + err, false);
    }
    target.disabled = false;
    target.textContent = label;
  }
});

document.addEventListener("change", async (event) => {
  const select = event.target;
  if (!select.classList || !select.classList.contains("tariff-select")) return;
  const deviceId = select.dataset.device;
  try {
    const { data } = await postJson(`/device/${deviceId}/tariff`, { tariff: select.value });
    if (!data.success) {
      showStatus(data.error || "Не удалось сменить тариф", false);
      location.reload();
      return;
    }
    showStatus("Тариф устройства обновлён", true);
    setTimeout(() => location.reload(), 700);
  } catch (err) {
    showStatus("Ошибка сети: " + err, false);
  }
});

document.getElementById("promo-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = document.getElementById("promo-code")?.value || "";
  try {
    const { data } = await postJson("/promo", { code });
    if (data.success) {
      showStatus("Промокод применён: +" + data.bonus + " ₽", true);
      setTimeout(() => location.reload(), 900);
    } else {
      showStatus(data.error || "Не удалось применить промокод", false);
    }
  } catch (err) {
    showStatus("Ошибка сети: " + err, false);
  }
});

document.getElementById("gift-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const to = document.getElementById("gift-to")?.value || "";
  const amount = parseFloat(document.getElementById("gift-amount")?.value || "0");
  if (!amount || amount < 100) {
    showStatus("Минимальная сумма — 100 ₽", false);
    return;
  }
  try {
    const { data } = await postJson("/gift", { to, amount });
    if (data.success) {
      showStatus("Пополнение другу отправлено", true);
      setTimeout(() => location.reload(), 900);
    } else {
      showStatus(data.error || "Не удалось отправить", false);
    }
  } catch (err) {
    showStatus("Ошибка сети: " + err, false);
  }
});

document.getElementById("profile-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const nickname = document.getElementById("nickname")?.value || "";
  let avatar = document.getElementById("avatar")?.value || "";
  const file = document.getElementById("avatar-file")?.files?.[0];
  try {
    if (file) {
      avatar = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }
    const { data } = await postJson("/profile", { nickname, avatar });
    if (data.success) {
      showStatus("Профиль сохранён", true);
      setTimeout(() => location.reload(), 700);
    } else {
      showStatus(data.error || "Не удалось сохранить", false);
    }
  } catch (err) {
    showStatus("Ошибка сети: " + err, false);
  }
});

document.getElementById("password-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const { data } = await postJson("/profile/password", {
      current: document.getElementById("current")?.value || "",
      password: document.getElementById("new-password")?.value || "",
      password2: document.getElementById("new-password2")?.value || "",
    });
    if (data.success) {
      showStatus("Пароль обновлён", true);
      e.target.reset();
    } else {
      showStatus(data.error || "Не удалось сменить пароль", false);
    }
  } catch (err) {
    showStatus("Ошибка сети: " + err, false);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});
