document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("category-period-form");
  if (!form || !window.INSURANCE_PERIODS_URL) return;

  var CHECK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>';

  var categoryButtons = form.querySelectorAll(".choice-card--category");
  var categoryInput = document.getElementById("category_code");
  var periodInput = document.getElementById("period_code");
  var periodGrid = document.getElementById("period-grid");
  var nextBtn = document.getElementById("cp-next-btn");

  var currentPeriods = [];

  function formatRub(value) {
    return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function updateNextEnabled() {
    var period = currentPeriods.filter(function (p) { return p.code === periodInput.value; })[0];
    nextBtn.disabled = !(categoryInput.value && period && period.is_priced);
  }

  function markCategorySelected(button) {
    categoryButtons.forEach(function (b) {
      b.classList.remove("is-selected");
      var existingCheck = b.querySelector(".choice-card__check");
      if (existingCheck) existingCheck.remove();
    });
    button.classList.add("is-selected");
    var check = document.createElement("span");
    check.className = "choice-card__check";
    check.innerHTML = CHECK_ICON;
    button.prepend(check);
  }

  function selectPeriod(code) {
    var period = currentPeriods.filter(function (p) { return p.code === code; })[0];
    if (!period || !period.is_priced) return;
    periodInput.value = code;
    periodGrid.querySelectorAll(".choice-card--period").forEach(function (btn) {
      btn.classList.toggle("is-selected", btn.getAttribute("data-period-code") === code);
    });
    updateNextEnabled();
  }

  function bindPeriodButtons() {
    periodGrid.querySelectorAll(".choice-card--period").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (btn.disabled) return;
        selectPeriod(btn.getAttribute("data-period-code"));
      });
    });
  }

  function renderPeriods(periods) {
    currentPeriods = periods;
    periodGrid.innerHTML = "";

    if (!periods.length) {
      var message = document.createElement("p");
      message.className = "step__text";
      message.textContent = "Эта категория скоро будет доступна.";
      periodGrid.appendChild(message);
      periodInput.value = "";
      updateNextEnabled();
      return;
    }

    periods.forEach(function (period) {
      var button = document.createElement("button");
      button.type = "button";
      button.setAttribute("data-period-code", period.code);
      button.className = "choice-card choice-card--period" + (period.is_priced ? "" : " is-unavailable");
      if (!period.is_priced) button.disabled = true;

      var title = document.createElement("span");
      title.className = "choice-card__title";
      title.textContent = period.label;
      button.appendChild(title);

      var priceEl = document.createElement("span");
      if (period.is_priced) {
        priceEl.className = "choice-card__price";
        priceEl.textContent = formatRub(period.price_rub) + " ₽";
      } else {
        priceEl.className = "choice-card__soon-label";
        priceEl.textContent = "Цена уточняется";
      }
      button.appendChild(priceEl);
      periodGrid.appendChild(button);
    });

    bindPeriodButtons();

    var stillValid = periods.filter(function (p) { return p.code === periodInput.value && p.is_priced; })[0];
    if (stillValid) {
      selectPeriod(stillValid.code);
    } else {
      periodInput.value = "";
      updateNextEnabled();
    }
  }

  categoryButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.classList.contains("is-selected")) return;
      markCategorySelected(button);
      var code = button.getAttribute("data-category-code");
      categoryInput.value = code;
      periodInput.value = "";

      fetch(window.INSURANCE_PERIODS_URL + "?category_code=" + encodeURIComponent(code))
        .then(function (response) { return response.ok ? response.json() : []; })
        .then(renderPeriods);
    });
  });

  // Initial state comes straight from the server-rendered period buttons.
  bindPeriodButtons();
  currentPeriods = Array.prototype.map.call(periodGrid.querySelectorAll(".choice-card--period"), function (btn) {
    var priceEl = btn.querySelector(".choice-card__price");
    return {
      code: btn.getAttribute("data-period-code"),
      label: btn.querySelector(".choice-card__title").textContent,
      is_priced: !btn.disabled,
      price_rub: priceEl ? parseInt(priceEl.textContent.replace(/\D/g, ""), 10) : null,
    };
  });
  updateNextEnabled();
});
