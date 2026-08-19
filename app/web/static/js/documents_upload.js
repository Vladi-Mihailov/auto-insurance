document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("upload-form");
  if (!form) return; // recognition unavailable -- no upload form rendered

  var input = document.getElementById("upload-input");
  var cameraArea = document.getElementById("camera-area");
  var cameraModal = document.getElementById("camera-modal");
  var cameraVideo = document.getElementById("camera-video");
  var cameraCaptureBtn = document.getElementById("camera-capture");
  var cameraCancelBtn = document.getElementById("camera-cancel");
  var cameraCanvas = document.getElementById("camera-canvas");
  var countEl = document.getElementById("upload-count");
  var listEl = document.getElementById("upload-list");
  var recognizeBtn = document.getElementById("recognize-btn");
  var maxFiles = parseInt(input.getAttribute("data-max-files"), 10) || 5;
  var cameraStream = null;
  var cameraOpening = false;
  var cameraRequestId = 0;
  var cameraShotCount = 0;

  // The native <input multiple> replaces its own FileList on every picker
  // use, so "add more photos" (selecting the label again) would otherwise
  // discard whatever was already chosen. This array is the real source of
  // truth for what's selected; input.files is rebuilt from it via
  // DataTransfer so the form still submits the full accumulated set.
  var selectedFiles = [];

  function pluralizeFiles(n) {
    var mod10 = n % 10;
    var mod100 = n % 100;
    var word = "файлов";
    if (mod10 === 1 && mod100 !== 11) {
      word = "файл";
    } else if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) {
      word = "файла";
    }
    return n + " " + word + " выбрано";
  }

  function isDuplicate(file) {
    return selectedFiles.some(function (existing) {
      return existing.name === file.name && existing.size === file.size && existing.lastModified === file.lastModified;
    });
  }

  function syncInputFiles() {
    var transfer = new DataTransfer();
    selectedFiles.forEach(function (file) {
      transfer.items.add(file);
    });
    input.files = transfer.files;
  }

  function render() {
    listEl.innerHTML = "";
    selectedFiles.forEach(function (file, index) {
      var item = document.createElement("div");
      item.className = "upload-list__item";

      var name = document.createElement("span");
      name.className = "upload-list__name";
      name.textContent = "✓ " + file.name;

      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "upload-list__remove";
      remove.setAttribute("aria-label", "Удалить файл");
      remove.textContent = "×";
      remove.addEventListener("click", function () {
        selectedFiles.splice(index, 1);
        syncInputFiles();
        render();
      });

      item.appendChild(name);
      item.appendChild(remove);
      listEl.appendChild(item);
    });

    listEl.hidden = selectedFiles.length === 0;
    countEl.hidden = selectedFiles.length === 0;
    if (selectedFiles.length > 0) {
      countEl.textContent = pluralizeFiles(selectedFiles.length);
    }
    recognizeBtn.disabled = selectedFiles.length === 0;
  }

  // Shared by both the gallery/file-picker input and camera captures (see
  // openCamera below) -- a photo taken with the camera feeds the exact same
  // selectedFiles accumulation, per-file validation, and submit as one
  // photo picked from the gallery. Only #upload-input itself carries
  // name="files" and is what actually gets submitted (its .files is
  // rebuilt from selectedFiles via syncInputFiles()).
  function addPickedFiles(fileList) {
    var picked = fileList ? Array.prototype.slice.call(fileList) : [];
    picked.forEach(function (file) {
      if (!isDuplicate(file)) {
        selectedFiles.push(file);
      }
    });
    if (selectedFiles.length > maxFiles) {
      selectedFiles = selectedFiles.slice(0, maxFiles);
    }
    syncInputFiles();
    render();
  }

  input.addEventListener("change", function () {
    addPickedFiles(input.files);
  });

  // --- Camera capture (getUserMedia) -----------------------------------
  //
  // A plain `<input type=file accept="image/*" capture="environment">` was
  // tried first, but on real devices `capture` is only a hint: several
  // Android Chrome / vendor browser combinations show the same
  // camera-or-gallery chooser as a bare file input instead of launching
  // the camera directly, so it can't guarantee a camera-first flow. This
  // uses the MediaDevices API directly (feature-detected, no UA sniffing)
  // to open an in-page live preview, capture a still frame to a canvas,
  // and turn it into a File -- which then goes through the exact same
  // addPickedFiles() path as a gallery pick.
  //
  // The modal is shown SYNCHRONOUSLY on click, in a "loading" data-state,
  // before getUserMedia is even called -- a real permission prompt (or
  // just slow camera hardware startup) can take a noticeable moment to
  // settle, and showing nothing during that window is exactly what made
  // an earlier version of this button look dead on click.
  function cameraSupported() {
    return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function setCameraState(state) {
    cameraModal.dataset.state = state;
    cameraCaptureBtn.disabled = state !== "active";
    cameraCancelBtn.textContent = state === "error" ? "Закрыть" : "Отмена";
  }

  function stopCameraStream() {
    if (cameraStream) {
      cameraStream.getTracks().forEach(function (track) {
        track.stop();
      });
      cameraStream = null;
    }
    cameraVideo.srcObject = null;
  }

  function closeCamera() {
    cameraOpening = false;
    stopCameraStream();
    cameraModal.hidden = true;
    setCameraState("loading"); // reset so the next open starts clean
  }

  function openCamera() {
    if (cameraOpening || cameraStream) {
      return; // already open/opening -- a stray double-click is a no-op
    }
    cameraModal.hidden = false;
    setCameraState("loading");

    if (!cameraSupported()) {
      setCameraState("error");
      return;
    }

    cameraOpening = true;
    // Tokened so a slow/stale request (e.g. the user cancelled and
    // reopened before the first getUserMedia call settled) can never
    // clobber a newer one -- without this, "open -> cancel -> open" in
    // quick succession could race two concurrent getUserMedia calls.
    var requestId = ++cameraRequestId;
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false })
      .then(function (stream) {
        cameraOpening = false;
        if (requestId !== cameraRequestId || cameraModal.hidden) {
          // Superseded by a newer open(), or the user hit Cancel/Отмена
          // while the permission prompt/hardware startup was pending.
          stream.getTracks().forEach(function (track) {
            track.stop();
          });
          return;
        }
        cameraStream = stream;
        cameraVideo.srcObject = stream;
        var playResult = cameraVideo.play();
        if (playResult && typeof playResult.catch === "function") {
          playResult.catch(function () {}); // benign: e.g. interrupted by an immediate close
        }
        setCameraState("active");
      })
      .catch(function () {
        // Covers NotAllowedError (permission denied), NotFoundError (no
        // camera), NotReadableError (camera already in use by another
        // app/tab), OverconstrainedError, SecurityError, AbortError, etc.
        // -- never surface the raw exception, and the modal stays open
        // with a plain-language message plus a way to close it; the
        // gallery picker outside the modal stays fully usable throughout.
        cameraOpening = false;
        if (requestId !== cameraRequestId) return; // a newer request already superseded this one
        setCameraState("error");
      });
  }

  function capturePhoto() {
    if (cameraModal.dataset.state !== "active") {
      return; // capture button is disabled outside "active", but guard anyway
    }
    var width = cameraVideo.videoWidth;
    var height = cameraVideo.videoHeight;
    if (!width || !height) {
      return; // stream technically active but no frame decoded yet
    }
    cameraCanvas.width = width;
    cameraCanvas.height = height;
    var ctx = cameraCanvas.getContext("2d");
    ctx.drawImage(cameraVideo, 0, 0, width, height);
    cameraCanvas.toBlob(
      function (blob) {
        if (!blob) return;
        cameraShotCount += 1;
        var file = new File([blob], "camera-" + cameraShotCount + ".jpg", { type: "image/jpeg" });
        closeCamera();
        addPickedFiles([file]);
      },
      "image/jpeg",
      0.92
    );
  }

  if (cameraArea) {
    cameraArea.addEventListener("click", openCamera);
  }
  if (cameraCaptureBtn) {
    cameraCaptureBtn.addEventListener("click", capturePhoto);
  }
  if (cameraCancelBtn) {
    cameraCancelBtn.addEventListener("click", closeCamera);
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      closeCamera();
    }
  });
  window.addEventListener("pagehide", stopCameraStream);

  form.addEventListener("submit", function () {
    recognizeBtn.disabled = true;
    recognizeBtn.textContent = "Распознаём документы…";
  });
});
