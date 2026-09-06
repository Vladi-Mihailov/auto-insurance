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

  // EXACT DATE RANGE product (AM passenger_car): both start_date and
  // end_date are real editable <input> fields (unlike the FIXED-period
  // block above, whose end date is a read-only <div id="end-date-display">)
  // -- that's what distinguishes this mode, no extra window flag needed.
  // The rule is a flat day-count, so it's plain client-side date math, no
  // server round-trip like the FIXED-period preview above needs.
  var endInput = document.getElementById("end_date");
  if (startInput && endInput) {
    var durationText = document.getElementById("duration-days-text");

    var updateDurationText = function () {
      var days = daysBetween(startInput.value, endInput.value);
      if (durationText) {
        durationText.textContent = days !== null ? "Срок: " + days + " дней" : "";
      }
    };

    startInput.addEventListener("change", function () {
      var previousDuration = daysBetween(startInput.dataset.prevValue, endInput.value);
      if (previousDuration !== null && startInput.value) {
        endInput.value = addDays(startInput.value, previousDuration);
      }
      startInput.dataset.prevValue = startInput.value;
      updateDurationText();
    });
    startInput.dataset.prevValue = startInput.value;

    endInput.addEventListener("change", updateDurationText);
  }
});

function formatDate(isoDate) {
  var parts = isoDate.split("-");
  return parts[2] + "." + parts[1] + "." + parts[0];
}

function parseIsoDateUTC(isoDate) {
  if (!isoDate) return null;
  var parts = isoDate.split("-");
  return Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
}

function daysBetween(startIso, endIso) {
  var start = parseIsoDateUTC(startIso);
  var end = parseIsoDateUTC(endIso);
  if (start === null || end === null) return null;
  return Math.round((end - start) / 86400000);
}

function addDays(isoDate, days) {
  var utc = parseIsoDateUTC(isoDate);
  var result = new Date(utc + days * 86400000);
  var year = result.getUTCFullYear();
  var month = String(result.getUTCMonth() + 1).padStart(2, "0");
  var day = String(result.getUTCDate()).padStart(2, "0");
  return year + "-" + month + "-" + day;
}
