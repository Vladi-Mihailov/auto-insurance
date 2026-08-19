document.addEventListener("DOMContentLoaded", function () {
  var startInput = document.getElementById("start_date");
  if (startInput && window.INSURANCE_DATE_PREVIEW_URL) {
    startInput.addEventListener("change", function () {
      var value = startInput.value;
      if (!value) return;

      fetch(window.INSURANCE_DATE_PREVIEW_URL + "?start=" + encodeURIComponent(value))
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (data) {
          if (!data) return;
          var display = document.getElementById("end-date-display");
          if (display) {
            display.textContent = formatDate(data.end_date);
            display.classList.remove("is-empty");
            return;
          }
          // Legacy date.html (pre-existing old orders only) still uses the
          // older date-preview markup.
          var preview = document.getElementById("date-preview");
          var valueEl = document.getElementById("date-preview-value");
          if (preview && valueEl) {
            valueEl.textContent = formatDate(value) + " — " + formatDate(data.end_date);
            preview.hidden = false;
          }
        });
    });
  }

});

function formatDate(isoDate) {
  var parts = isoDate.split("-");
  return parts[2] + "." + parts[1] + "." + parts[0];
}
