document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("upload-form");
  if (!form) return; // recognition unavailable -- no upload form rendered

  var input = document.getElementById("upload-input");
  var filenameEl = document.getElementById("upload-filename");
  var recognizeBtn = document.getElementById("recognize-btn");

  input.addEventListener("change", function () {
    var file = input.files && input.files[0];
    if (file) {
      filenameEl.textContent = file.name;
      filenameEl.hidden = false;
      recognizeBtn.disabled = false;
    } else {
      filenameEl.hidden = true;
      recognizeBtn.disabled = true;
    }
  });

  form.addEventListener("submit", function () {
    recognizeBtn.disabled = true;
    recognizeBtn.textContent = "Распознаём документ…";
  });
});
