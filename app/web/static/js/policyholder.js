document.addEventListener("DOMContentLoaded", function () {
  function wireToggle(radioName, fieldsEl, activate) {
    var radios = document.querySelectorAll('input[name="' + radioName + '"]');
    if (!radios.length) return;
    radios.forEach(function (radio) {
      radio.addEventListener("change", function () {
        radios.forEach(function (r) {
          r.closest(".segmented__option").classList.toggle("is-active", r.checked);
        });
        if (fieldsEl) fieldsEl.hidden = !activate();
      });
    });
  }

  var driverFields = document.getElementById("driver-fields");
  wireToggle("driver_same_as_policyholder", driverFields, function () {
    var no = document.querySelector('input[name="driver_same_as_policyholder"][value="no"]');
    return no && no.checked;
  });

  var ownerFields = document.getElementById("owner-fields");
  wireToggle("owner_same_as_policyholder", ownerFields, function () {
    var no = document.querySelector('input[name="owner_same_as_policyholder"][value="no"]');
    return no && no.checked;
  });

  var ownerNameLabel = document.getElementById("owner-name-label");
  var ownerIdentifierLabel = document.getElementById("owner-identifier-label");
  var ownerCitizenshipField = document.getElementById("owner-citizenship-field");
  wireToggle("owner_entity_type", null, function () {
    return true; // no hide/show of its own container -- only relabels/toggles the citizenship field below
  });
  document.querySelectorAll('input[name="owner_entity_type"]').forEach(function (radio) {
    radio.addEventListener("change", function () {
      var isLegal = document.querySelector('input[name="owner_entity_type"][value="legal"]').checked;
      if (ownerNameLabel) ownerNameLabel.textContent = isLegal ? "Название организации" : "Владелец";
      if (ownerIdentifierLabel) ownerIdentifierLabel.textContent = isLegal ? "Идентификационный код" : "Идентификационный номер";
      if (ownerCitizenshipField) ownerCitizenshipField.hidden = isLegal;
    });
  });
});
