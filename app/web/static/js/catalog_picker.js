document.addEventListener("DOMContentLoaded", function () {
  var manufacturerIdInput = document.getElementById("manufacturer_id");
  var modelIdInput = document.getElementById("model_id");
  if (!manufacturerIdInput || !modelIdInput) return;

  var modelTrigger = document.getElementById("model-trigger");
  var modelTriggerText = document.getElementById("model-trigger-text");
  var modelPickerRoot = document.getElementById("model-picker");
  var allModelsForManufacturer = [];
  var modelsLoadFailed = false;
  var modelPicker = null; // assigned once createPicker() runs, below

  function loadModelsForManufacturer(manufacturerId) {
    modelsLoadFailed = false;
    allModelsForManufacturer = [];
    return fetch("/api/vehicle-models?manufacturer_id=" + encodeURIComponent(manufacturerId))
      .then(function (response) { return response.ok ? response.json() : { models: [], synced: false }; })
      .then(function (data) {
        allModelsForManufacturer = data.models || [];
        modelsLoadFailed = data.synced === false;
        if (modelPicker) modelPicker.refresh();
      })
      .catch(function () {
        modelsLoadFailed = true;
        if (modelPicker) modelPicker.refresh();
      });
  }

  function createPicker(config) {
    var isOpen = false;

    function renderList(items) {
      config.list.innerHTML = "";

      if (config.failedState && config.failedState()) {
        var errorMsg = document.createElement("p");
        errorMsg.className = "picker__empty picker__empty--error";
        errorMsg.textContent = "Не удалось загрузить модели. ";
        var retry = document.createElement("button");
        retry.type = "button";
        retry.className = "picker__retry";
        retry.textContent = "Повторить";
        retry.addEventListener("click", function () {
          if (config.onRetry) config.onRetry();
        });
        errorMsg.appendChild(retry);
        config.list.appendChild(errorMsg);
        return;
      }

      if (!items.length) {
        var empty = document.createElement("p");
        empty.className = "picker__empty";
        empty.textContent = "Ничего не найдено";
        config.list.appendChild(empty);
        return;
      }
      items.forEach(function (item) {
        var option = document.createElement("button");
        option.type = "button";
        option.className = "picker__option";
        option.textContent = item.name;
        option.addEventListener("click", function () {
          config.hiddenInput.value = item.id;
          config.triggerText.textContent = item.name;
          close();
          if (config.onSelect) config.onSelect(item);
        });
        config.list.appendChild(option);
      });
    }

    function load(query) {
      if (config.clientItems) {
        var q = (query || "").trim().toLowerCase();
        var items = q
          ? config.clientItems().filter(function (item) { return item.name.toLowerCase().indexOf(q) !== -1; })
          : config.clientItems();
        renderList(items);
        return;
      }
      fetch(config.fetchUrl(query || ""))
        .then(function (response) { return response.ok ? response.json() : []; })
        .then(renderList);
    }

    function open() {
      if (config.trigger.disabled) return;
      isOpen = true;
      config.panel.hidden = false;
      config.search.value = "";
      load("");
      config.search.focus();
    }

    function close() {
      isOpen = false;
      config.panel.hidden = true;
    }

    config.trigger.addEventListener("click", function () {
      if (isOpen) close(); else open();
    });
    config.search.addEventListener("input", function () {
      load(config.search.value);
    });
    document.addEventListener("click", function (event) {
      if (isOpen && !config.root.contains(event.target)) close();
    });

    return {
      close: close,
      refresh: function () { if (isOpen) load(config.search.value); },
    };
  }

  createPicker({
    root: document.getElementById("manufacturer-picker"),
    trigger: document.getElementById("manufacturer-trigger"),
    triggerText: document.getElementById("manufacturer-trigger-text"),
    panel: document.getElementById("manufacturer-panel"),
    search: document.getElementById("manufacturer-search"),
    list: document.getElementById("manufacturer-list"),
    hiddenInput: manufacturerIdInput,
    fetchUrl: function (query) {
      return "/api/manufacturers?q=" + encodeURIComponent(query);
    },
    onSelect: function (item) {
      modelIdInput.value = "";
      modelTriggerText.textContent = "Выберите";
      modelTrigger.disabled = false;
      modelPickerRoot.classList.remove("picker--disabled");
      loadModelsForManufacturer(item.id);
    },
  });

  modelPicker = createPicker({
    root: modelPickerRoot,
    trigger: modelTrigger,
    triggerText: modelTriggerText,
    panel: document.getElementById("model-panel"),
    search: document.getElementById("model-search"),
    list: document.getElementById("model-list"),
    hiddenInput: modelIdInput,
    clientItems: function () {
      return allModelsForManufacturer;
    },
    failedState: function () {
      return modelsLoadFailed;
    },
    onRetry: function () {
      loadModelsForManufacturer(manufacturerIdInput.value);
    },
  });

  // Returning to this step (draft resume, or post-order edit) with a
  // manufacturer already chosen — enable the model picker right away and
  // warm its list, instead of waiting for a manufacturer re-selection.
  if (manufacturerIdInput.value) {
    modelTrigger.disabled = false;
    modelPickerRoot.classList.remove("picker--disabled");
    loadModelsForManufacturer(manufacturerIdInput.value);
  }
});
