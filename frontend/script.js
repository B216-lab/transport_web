// frontend/script.js
const API = "http://127.0.0.1:8000/analyze";

// Application state
const state = {
  rawGeoJson: null,
  analysisResult: null,
  filteredPoints: null,
  routeLayer: null,
  legend: null,
};

// Initialize map
const map = L.map("map").setView([52.3, 104.3], 11);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

// Handle window resize
window.addEventListener("resize", () => {
  setTimeout(() => {
    map.invalidateSize();
  }, 100);
});

let ptsLayer = null;
let heatmapLayer = null;
let polyLayers = [];
let chart = null;
let worker = null;
let currentVisualizationMode = "points";

// Toggle sidebar
document.getElementById("toggleSidebar")?.addEventListener("click", () => {
  const sidebar = document.getElementById("sidebar");
  sidebar.classList.toggle("sidebar-collapsed");
  // Trigger map resize after sidebar animation
  setTimeout(() => {
    map.invalidateSize();
  }, 300);
});

// Utility functions
function showNotification(message, type = "info") {
  // Remove existing notifications
  const existing = document.querySelector(".alert");
  if (existing) existing.remove();

  const alert = document.createElement("div");
  alert.className = `alert alert-${type}`;
  alert.textContent = message;
  const sidebarContent = document.querySelector(".sidebar-content");
  if (sidebarContent) {
    sidebarContent.insertBefore(alert, sidebarContent.firstChild);
  }

  setTimeout(() => {
    if (alert.parentNode) {
      alert.style.opacity = "0";
      setTimeout(() => alert.remove(), 300);
    }
  }, 5000);
}

function formatNumber(num, decimals = 1) {
  if (num === null || num === undefined) return "—";
  return num.toFixed(decimals);
}

function formatDistance(meters) {
  if (meters < 1000) return `${formatNumber(meters, 0)} м`;
  return `${formatNumber(meters / 1000, 2)} км`;
}

function haversineDistance(lat1, lon1, lat2, lon2) {
  const R = 6371000; // Earth radius in meters
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

function speedColor(s) {
  if (s < 10) return "#dc2626"; // Red - very slow
  if (s < 20) return "#f97316"; // Orange - slow
  if (s < 40) return "#eab308"; // Yellow - medium
  return "#22c55e"; // Green - fast
}

function getSpeedCategory(s) {
  if (s < 10) return "Очень медленно";
  if (s < 20) return "Медленно";
  if (s < 40) return "Средне";
  return "Быстро";
}

function extractTimeRange(geojson) {
  /**
   * Extract min and max time from GeoJSON features.
   * Returns {min: Date, max: Date} or null if no valid times found.
   */
  if (!geojson || !geojson.features) {
    return null;
  }

  const times = [];
  
  for (const feature of geojson.features) {
    const props = feature.properties || {};
    const rawTime = props.time;
    
    if (!rawTime) continue;
    
    try {
      // Parse date with support for DD.MM.YYYY format
      let date = null;
      const timeStr = String(rawTime).trim();
      
      // Format: DD.MM.YYYY HH:mm:ss or DD.MM.YYYY HH:mm
      const ddmmyyyyMatch = timeStr.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?/);
      if (ddmmyyyyMatch) {
        const [, day, month, year, hour, minute, second] = ddmmyyyyMatch;
        date = new Date(
          parseInt(year),
          parseInt(month) - 1, // Month is 0-indexed
          parseInt(day),
          parseInt(hour),
          parseInt(minute),
          second ? parseInt(second) : 0
        );
      } else {
        // Try standard Date parsing
        date = new Date(timeStr);
      }
      
      if (date && !isNaN(date.getTime())) {
        times.push(date);
      }
    } catch (e) {
      // Skip invalid dates
      continue;
    }
  }

  if (times.length === 0) {
    return null;
  }

  const minTime = new Date(Math.min(...times.map(t => t.getTime())));
  const maxTime = new Date(Math.max(...times.map(t => t.getTime())));

  return { min: minTime, max: maxTime };
}

function formatDateTimeLocal(date) {
  /**
   * Convert Date to datetime-local input format: YYYY-MM-DDTHH:MM
   */
  if (!date || !(date instanceof Date) || isNaN(date.getTime())) {
    return "";
  }
  
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

// File input handler
document.getElementById("fileInput").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;

  try {
    const text = await f.text();
    const parsed = JSON.parse(text);

    // Validate GeoJSON structure
    if (!parsed || typeof parsed !== "object") {
      throw new Error("Неверный формат JSON");
    }

    if (parsed.type !== "FeatureCollection") {
      throw new Error(`Ожидается FeatureCollection, получен: ${parsed.type}`);
    }

    if (!Array.isArray(parsed.features)) {
      throw new Error("GeoJSON должен содержать массив 'features'");
    }

    state.rawGeoJson = parsed;

    const featureCount = parsed.features.length;
    
    if (featureCount === 0) {
      showNotification(
        "Внимание: файл не содержит объектов (features). Проверьте структуру GeoJSON.",
        "warning"
      );
    } else {
      // Validate that features have required properties
      let validPoints = 0;
      let hasTime = 0;
      let hasSpeed = 0;
      
      for (const feat of parsed.features.slice(0, 100)) { // Check first 100
        if (feat.geometry && feat.geometry.type === "Point") {
          validPoints++;
          if (feat.properties && feat.properties.time) hasTime++;
          if (feat.properties && feat.properties.speed !== undefined) hasSpeed++;
        }
      }

      const warnings = [];
      if (validPoints === 0) {
        warnings.push("не найдено точек (Point)");
      }
      if (hasTime < validPoints * 0.5) {
        warnings.push("у многих точек отсутствует поле 'time'");
      }
      if (hasSpeed < validPoints * 0.5) {
        warnings.push("у многих точек отсутствует поле 'speed'");
      }

      if (warnings.length > 0) {
        showNotification(
          `GeoJSON загружен: ${featureCount} объектов. Предупреждения: ${warnings.join(", ")}`,
          "warning"
        );
      } else {
        showNotification(
          `GeoJSON загружен: ${featureCount} объектов`,
          "success"
        );
      }
    }

    // Fit bounds to data
    try {
      const coords = state.rawGeoJson.features
        .filter((ft) => ft.geometry && ft.geometry.type === "Point")
        .map((ft) => [
          ft.geometry.coordinates[1],
          ft.geometry.coordinates[0],
        ]);
      if (coords.length) {
        map.fitBounds(coords, { padding: [50, 50] });
      }
    } catch (e) {
      console.error("Error fitting bounds:", e);
    }

    // Extract and set time range automatically
    const timeRange = extractTimeRange(state.rawGeoJson);
    if (timeRange) {
      const startInput = document.getElementById("start");
      const endInput = document.getElementById("end");
      
      // Set values only if fields are empty (don't overwrite user input)
      if (!startInput.value) {
        startInput.value = formatDateTimeLocal(timeRange.min);
      }
      if (!endInput.value) {
        endInput.value = formatDateTimeLocal(timeRange.max);
      }
      
      showNotification(
        `Временной диапазон автоматически установлен: ${timeRange.min.toLocaleString("ru-RU")} - ${timeRange.max.toLocaleString("ru-RU")}`,
        "info"
      );
    } else {
      showNotification(
        "Не удалось определить временной диапазон из данных",
        "warning"
      );
    }

    // Enable analyze button
    document.getElementById("analyzeBtn").disabled = false;
  } catch (e) {
    showNotification(`Ошибка загрузки GeoJSON: ${e.message}`, "error");
    state.rawGeoJson = null;
  }
});

// Debounce function for performance
function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

// Analyze button handler with Web Worker support
document.getElementById("analyzeBtn").addEventListener("click", async () => {
  if (!state.rawGeoJson) {
    showNotification("Сначала загрузите GeoJSON файл", "warning");
    return;
  }

  const btn = document.getElementById("analyzeBtn");
  const btnText = document.getElementById("analyzeBtnText");
  const btnLoader = document.getElementById("analyzeBtnLoader");

  // Show loading state
  btn.disabled = true;
  btnText.style.display = "none";
  btnLoader.style.display = "inline-block";
  showProgress();

  // Show performance indicator
  const startTime = performance.now();

  try {
    // Gather parameters
    const startVal = document.getElementById("start").value;
    const endVal = document.getElementById("end").value;
    const include_zero = document.getElementById("include_zero").checked;
    const speed_thresh = parseFloat(
      document.getElementById("speed_thresh").value
    );
    const eps = parseFloat(document.getElementById("eps").value);
    const min_pts = parseInt(document.getElementById("min_pts").value);

    // Validate inputs
    if (isNaN(speed_thresh) || speed_thresh < 0) {
      throw new Error("Некорректный порог скорости");
    }
    if (isNaN(eps) || eps < 0) {
      throw new Error("Некорректный радиус кластера");
    }
    if (isNaN(min_pts) || min_pts < 1) {
      throw new Error("Некорректное минимальное количество точек");
    }

    const featureCount = state.rawGeoJson.features?.length || 0;
    
    if (featureCount > 10000) {
      showNotification(`Обработка большого файла (${featureCount} точек)...`, "info");
    }

    // Use Web Worker for preprocessing if available
    if (worker) {
      updateProgress({ stage: "Подготовка данных", progress: 10, current: 0, total: featureCount });
      
      worker.postMessage({
        type: "process",
        data: state.rawGeoJson,
        options: {
          start: startVal ? startVal.replace("T", " ") : null,
          end: endVal ? endVal.replace("T", " ") : null,
          include_zero,
          speed_thresh,
        }
      });

      // Store parameters for later use
      state.pendingAnalysis = {
        speed_thresh,
        eps_m: eps,
        min_pts,
        startTime
      };
    } else {
      // Fallback to direct processing
      await performAnalysisDirectly(startVal, endVal, include_zero, speed_thresh, eps, min_pts, startTime);
    }
  } catch (e) {
    showNotification(`Ошибка анализа: ${e.message}`, "error");
    console.error("Analysis error:", e);
    hideProgress();
    btn.disabled = false;
    btnText.style.display = "inline";
    btnLoader.style.display = "none";
  }
});

// Continue analysis after worker preprocessing
async function continueAnalysis(processedData) {
  const { speed_thresh, eps_m, min_pts, startTime } = state.pendingAnalysis;
  
  // Validate that we have filtered points
  if (!processedData || !processedData.filtered || processedData.filtered.length === 0) {
    hideProgress();
    const btn = document.getElementById("analyzeBtn");
    const btnText = document.getElementById("analyzeBtnText");
    const btnLoader = document.getElementById("analyzeBtnLoader");
    if (btn) btn.disabled = false;
    if (btnText) btnText.style.display = "inline";
    if (btnLoader) btnLoader.style.display = "none";
    
    // Build detailed error message
    let errorMsg = "Все точки были отфильтрованы.\n\n";
    
    if (processedData.stats && processedData.stats.details) {
      const details = processedData.stats.details;
      errorMsg += "Причины фильтрации:\n";
      
      if (details.invalidGeometry > 0) {
        errorMsg += `• Неверная геометрия: ${details.invalidGeometry} точек\n`;
      }
      if (details.invalidCoordinates > 0) {
        errorMsg += `• Неверные координаты: ${details.invalidCoordinates} точек\n`;
      }
      if (details.noTime > 0) {
        errorMsg += `• Нет поля 'time': ${details.noTime} точек\n`;
      }
      if (details.invalidTime > 0) {
        errorMsg += `• Неверный формат времени: ${details.invalidTime} точек\n`;
      }
      if (details.timeFiltered > 0) {
        errorMsg += `• Отфильтровано по времени: ${details.timeFiltered} точек\n`;
        errorMsg += "  (проверьте диапазон начала/конца периода)\n";
      }
      if (details.noSpeed > 0) {
        errorMsg += `• Нет поля 'speed': ${details.noSpeed} точек\n`;
      }
      if (details.zeroSpeedFiltered > 0) {
        errorMsg += `• Нулевая скорость (отфильтровано): ${details.zeroSpeedFiltered} точек\n`;
      }
      
      errorMsg += `\nВсего обработано: ${details.total || 0} точек\n`;
      errorMsg += `Валидных: ${details.valid || 0} точек`;
    } else {
      errorMsg += "Проверьте:\n";
      errorMsg += "1. Временной диапазон (начало/конец периода)\n";
      errorMsg += "2. Наличие поля 'time' в свойствах точек\n";
      errorMsg += "3. Наличие поля 'speed' в свойствах точек\n";
      errorMsg += "4. Формат данных GeoJSON";
    }
    
    errorMsg += "\n\nИспользуйте кнопку 'Проверить данные' для детальной диагностики.";
    
    showNotification(errorMsg, "warning");
    
    // Auto-show diagnostic results if available
    if (state.rawGeoJson) {
      setTimeout(() => {
        diagnoseGeoJSON();
      }, 1000);
    }
    
    return;
  }

  updateProgress({ stage: "Отправка на сервер", progress: 60, current: 0, total: 0 });

  try {
    // Build filtered GeoJSON
    const filteredGeoJson = {
      type: "FeatureCollection",
      features: processedData.filtered
    };

    // Double-check before sending
    if (!filteredGeoJson.features || filteredGeoJson.features.length === 0) {
      throw new Error("Нет точек для анализа после фильтрации");
    }

    const payload = {
      geojson: filteredGeoJson,
      start: null, // Already filtered
      end: null,   // Already filtered
      include_zero: true, // Already filtered
      speed_thresh,
      eps_m,
      min_pts,
    };

    updateProgress({ stage: "Кластеризация заторов", progress: 70, current: 0, total: 0 });

    // Call backend
    const resp = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const errorData = await resp.json().catch(() => ({}));
      throw new Error(
        errorData.detail || `Ошибка сервера: ${resp.status} ${resp.statusText}`
      );
    }

    updateProgress({ stage: "Визуализация", progress: 90, current: 0, total: 0 });

    const data = await resp.json();
    
    // Add heatmap data to result
    data.heatmapData = processedData.heatmapData;
    state.analysisResult = data;
    
    // Use requestAnimationFrame for smooth UI updates
    requestAnimationFrame(() => {
      showResults(data);
      const elapsed = ((performance.now() - startTime) / 1000).toFixed(1);
      updateProgress({ stage: "Завершено", progress: 100, current: 0, total: 0 });
      setTimeout(() => {
        hideProgress();
        showNotification(`Анализ завершен за ${elapsed} сек.`, "success");
      }, 500);
    });
  } catch (e) {
    showNotification(`Ошибка анализа: ${e.message}`, "error");
    console.error("Analysis error:", e);
    hideProgress();
  } finally {
    const btn = document.getElementById("analyzeBtn");
    const btnText = document.getElementById("analyzeBtnText");
    const btnLoader = document.getElementById("analyzeBtnLoader");
    btn.disabled = false;
    btnText.style.display = "inline";
    btnLoader.style.display = "none";
  }
}

// Fallback function for direct analysis (no worker)
async function performAnalysisDirectly(startVal, endVal, include_zero, speed_thresh, eps, min_pts, startTime) {
  // Validate GeoJSON before sending
  if (!state.rawGeoJson || !state.rawGeoJson.features || state.rawGeoJson.features.length === 0) {
    hideProgress();
    const btn = document.getElementById("analyzeBtn");
    const btnText = document.getElementById("analyzeBtnText");
    const btnLoader = document.getElementById("analyzeBtnLoader");
    if (btn) btn.disabled = false;
    if (btnText) btnText.style.display = "inline";
    if (btnLoader) btnLoader.style.display = "none";
    
    showNotification(
      "GeoJSON не содержит объектов (features). Проверьте структуру файла.",
      "error"
    );
    return;
  }

  updateProgress({ stage: "Отправка на сервер", progress: 30, current: 0, total: 0 });

  const payload = {
    geojson: state.rawGeoJson,
    start: startVal ? startVal.replace("T", " ") : null,
    end: endVal ? endVal.replace("T", " ") : null,
    include_zero,
    speed_thresh,
    eps_m: eps,
    min_pts,
  };

  updateProgress({ stage: "Обработка на сервере", progress: 50, current: 0, total: 0 });

  const resp = await fetch(API, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(
      errorData.detail || `Ошибка сервера: ${resp.status} ${resp.statusText}`
    );
  }

  updateProgress({ stage: "Визуализация", progress: 90, current: 0, total: 0 });

  const data = await resp.json();
  state.analysisResult = data;
  
  requestAnimationFrame(() => {
    showResults(data);
    const elapsed = ((performance.now() - startTime) / 1000).toFixed(1);
    updateProgress({ stage: "Завершено", progress: 100, current: 0, total: 0 });
    setTimeout(() => {
      hideProgress();
      showNotification(`Анализ завершен за ${elapsed} сек.`, "success");
    }, 500);
  });
}

// Show results
function showResults(data) {
  // Update statistics
  document.getElementById("avg").innerText = data.avg_speed
    ? `${formatNumber(data.avg_speed)} км/ч`
    : "—";
  document.getElementById("dropped").innerText = data.dropped || 0;
  document.getElementById("count").innerText = data.count || 0;

  // Calculate additional statistics from raw data if available, otherwise use aggregated
  const rawSpeeds = data.plot.raw_speeds || data.plot.speeds || [];
  const speeds = rawSpeeds.length > 0 ? rawSpeeds : (data.plot.speeds || []);
  const maxSpeed = speeds.length ? Math.max(...speeds) : null;
  const minSpeed = speeds.length ? Math.min(...speeds) : null;

  document.getElementById("maxSpeed").innerText = maxSpeed
    ? `${formatNumber(maxSpeed)} км/ч`
    : "—";
  document.getElementById("minSpeed").innerText = minSpeed
    ? `${formatNumber(minSpeed)} км/ч`
    : "—";

  // Calculate total distance
  let totalDistance = 0;
  const features = data.filtered_geojson?.features || [];
  for (let i = 1; i < features.length; i++) {
    const prev = features[i - 1];
    const curr = features[i];
    if (
      prev.geometry?.coordinates &&
      curr.geometry?.coordinates &&
      prev.geometry.type === "Point" &&
      curr.geometry.type === "Point"
    ) {
      const [lon1, lat1] = prev.geometry.coordinates;
      const [lon2, lat2] = curr.geometry.coordinates;
      totalDistance += haversineDistance(lat1, lon1, lat2, lon2);
    }
  }
  document.getElementById("totalDistance").innerText = formatDistance(
    totalDistance
  );

  // Store filtered points for filtering
  state.filteredPoints = features;

  // Store heatmap data if available
  if (data.heatmapData) {
    state.heatmapData = data.heatmapData;
  }

  // Draw visualization based on current mode
  updateVisualization(data, currentVisualizationMode);

  // Draw congestion polygons
  polyLayers.forEach((layer) => map.removeLayer(layer));
  polyLayers = [];

  if (data.congestion_zones && data.congestion_zones.length > 0) {
    data.congestion_zones.forEach((zone, idx) => {
      const polyLayer = L.geoJSON(zone, {
        style: {
          color: "#ef4444",
          fillColor: "#ef4444",
          fillOpacity: 0.3,
          weight: 2,
        },
      }).addTo(map);

      polyLayers.push(polyLayer);
    });
  } else if (data.congestion) {
    // Backward compatibility
    const polyLayer = L.geoJSON(data.congestion, {
      style: {
        color: "#ef4444",
        fillColor: "#ef4444",
        fillOpacity: 0.3,
        weight: 2,
      },
    }).addTo(map);
    polyLayers.push(polyLayer);
  }

  // Add legend
  if (state.legend) {
    map.removeControl(state.legend);
  }

  state.legend = L.control({ position: "bottomright" });
  state.legend.onAdd = function () {
    const div = L.DomUtil.create("div", "legend");
    div.innerHTML = `
      <div style="background: white; padding: 0.75rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
        <strong style="font-size: 0.75rem; margin-bottom: 0.5rem; display: block;">Легенда скорости:</strong>
        <div class="legend-item"><span class="legend-color" style="background: #dc2626;"></span> < 10 км/ч</div>
        <div class="legend-item"><span class="legend-color" style="background: #f97316;"></span> 10-20 км/ч</div>
        <div class="legend-item"><span class="legend-color" style="background: #eab308;"></span> 20-40 км/ч</div>
        <div class="legend-item"><span class="legend-color" style="background: #22c55e;"></span> 40+ км/ч</div>
      </div>
    `;
    return div;
  };
  state.legend.addTo(map);

  // Update chart
  if (chart) {
    chart.destroy();
    chart = null;
  }

  const ctx = document.getElementById("speedChart").getContext("2d");
  
  // Use aggregated data (15-minute intervals) if available, otherwise use raw data
  const plotTimes = data.plot.times || [];
  const plotSpeeds = data.plot.speeds || [];
  const labels = plotTimes.map((t) => new Date(t));

  chart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Скорость (км/ч) - 15 мин интервалы",
          data: plotSpeeds.map((speed, idx) => ({
            x: labels[idx],
            y: speed,
          })),
          borderColor: "#2563eb",
          backgroundColor: "rgba(37, 99, 235, 0.1)",
          fill: true,
          tension: 0.4,
          pointRadius: 3,
          pointHoverRadius: 6,
          pointBorderWidth: 2,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: {
        duration: 750,
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
        },
        tooltip: {
          mode: "index",
          intersect: false,
          callbacks: {
            label: function (context) {
              return `Средняя скорость: ${formatNumber(context.parsed.y)} км/ч (15 мин интервал)`;
            },
            title: function (context) {
              const date = new Date(context[0].parsed.x);
              return date.toLocaleString("ru-RU", {
                day: "2-digit",
                month: "2-digit",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              });
            },
          },
        },
      },
      scales: {
        x: {
          type: "time",
          time: {
            unit: "minute",
            stepSize: 15,
            tooltipFormat: "dd.MM.yyyy HH:mm",
            displayFormats: {
              minute: "HH:mm",
              hour: "HH:mm",
              day: "dd.MM",
            },
          },
          title: {
            display: true,
            text: "Время (15-минутные интервалы)",
          },
          ticks: {
            maxRotation: 45,
            minRotation: 45,
          },
        },
        y: {
          title: {
            display: true,
            text: "Скорость (км/ч)",
          },
          beginAtZero: true,
        },
      },
      interaction: {
        mode: "nearest",
        axis: "x",
        intersect: false,
      },
    },
  });

  // Show stats, filters, and visualization panels
  document.getElementById("statsSection").style.display = "block";
  document.getElementById("filters").style.display = "block";
  document.getElementById("visualizationSection").style.display = "block";
  document.getElementById("exportBtn").disabled = false;
  
  // Update speed filter range
  updateSpeedFilterRange();
  
  // Scroll to stats section
  const statsSection = document.getElementById("statsSection");
  if (statsSection) {
    setTimeout(() => {
      statsSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 100);
  }
}

// Update visualization based on mode
function updateVisualization(data, mode) {
  const features = data.filtered_geojson?.features || [];
  
  // Remove existing layers
  if (ptsLayer) {
    map.removeLayer(ptsLayer);
    ptsLayer = null;
  }
  if (heatmapLayer) {
    map.removeLayer(heatmapLayer);
    heatmapLayer = null;
  }

  if (features.length === 0) return;

  if (mode === "heatmap" && state.heatmapData && state.heatmapData.length > 0) {
    // Show heatmap
    const heatmapPoints = state.heatmapData.map(([lat, lon, intensity]) => [lat, lon, intensity]);
    
    heatmapLayer = L.heatLayer(heatmapPoints, {
      radius: 25,
      blur: 15,
      maxZoom: 17,
      max: 1.0,
      gradient: {
        0.0: "blue",
        0.3: "cyan",
        0.5: "lime",
        0.7: "yellow",
        1.0: "red"
      }
    }).addTo(map);

    // Fit bounds using first and last points
    if (heatmapPoints.length > 0) {
      const bounds = heatmapPoints.reduce((acc, [lat, lon]) => {
        return acc.extend([lat, lon]);
      }, L.latLngBounds([heatmapPoints[0][0], heatmapPoints[0][1]], [heatmapPoints[0][0], heatmapPoints[0][1]]));
      map.fitBounds(bounds, { padding: [50, 50], maxZoom: 16 });
    }
  } else {
    // Show points
    ptsLayer = L.geoJSON(data.filtered_geojson, {
      pointToLayer: function (feature, latlng) {
        const s =
          feature.properties && feature.properties.speed
            ? feature.properties.speed
            : 0;
        return L.circleMarker(latlng, {
          radius: 6,
          color: speedColor(s),
          fillColor: speedColor(s),
          fillOpacity: 0.8,
          weight: 1,
        });
      },
      onEachFeature: function (f, layer) {
        const p = f.properties || {};
        const speed = p.speed || 0;
        const time = p.time ? new Date(p.time).toLocaleString("ru-RU") : "—";
        const popupContent = `
          <div style="font-size: 0.875rem;">
            <strong>Скорость:</strong> ${formatNumber(speed)} км/ч<br/>
            <strong>Категория:</strong> ${getSpeedCategory(speed)}<br/>
            <strong>Время:</strong> ${time}<br/>
            ${Object.entries(p)
              .filter(([k]) => k !== "speed" && k !== "time")
              .map(([k, v]) => `<strong>${k}:</strong> ${v}`)
              .join("<br/>")}
          </div>
        `;
        layer.bindPopup(popupContent);
      },
    }).addTo(map);

    // Fit bounds
    if (ptsLayer.getBounds && ptsLayer.getBounds().isValid()) {
      map.fitBounds(ptsLayer.getBounds(), {
        padding: [50, 50],
        maxZoom: 16,
      });
    }
  }
}

// Visualization mode switcher
document.querySelectorAll('input[name="visualizationMode"]').forEach(radio => {
  radio.addEventListener("change", (e) => {
    if (e.target.checked) {
      currentVisualizationMode = e.target.value;
      if (state.analysisResult) {
        updateVisualization(state.analysisResult, currentVisualizationMode);
      }
    }
  });
});

// Export functionality
document.getElementById("exportBtn").addEventListener("click", () => {
  if (!state.analysisResult) {
    showNotification("Нет данных для экспорта", "warning");
    return;
  }

  const format = prompt(
    "Выберите формат экспорта:\n1 - GeoJSON\n2 - CSV\n3 - JSON",
    "1"
  );

  if (!format) return;

  try {
    if (format === "1" || format.toLowerCase() === "geojson") {
      // Export GeoJSON
      const dataStr = JSON.stringify(
        state.analysisResult.filtered_geojson,
        null,
        2
      );
      const blob = new Blob([dataStr], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `transport_analysis_${new Date().toISOString().split("T")[0]}.geojson`;
      a.click();
      URL.revokeObjectURL(url);
      showNotification("GeoJSON экспортирован", "success");
    } else if (format === "2" || format.toLowerCase() === "csv") {
      // Export CSV
      const features = state.analysisResult.filtered_geojson?.features || [];
      const headers = ["Время", "Широта", "Долгота", "Скорость (км/ч)"];
      const rows = features.map((f) => {
        const props = f.properties || {};
        const coords = f.geometry?.coordinates || [];
        return [
          props.time || "",
          coords[1] || "",
          coords[0] || "",
          props.speed || 0,
        ];
      });
      const csv =
        headers.join(",") +
        "\n" +
        rows.map((r) => r.join(",")).join("\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `transport_analysis_${new Date().toISOString().split("T")[0]}.csv`;
      a.click();
      URL.revokeObjectURL(url);
      showNotification("CSV экспортирован", "success");
    } else if (format === "3" || format.toLowerCase() === "json") {
      // Export full JSON
      const dataStr = JSON.stringify(state.analysisResult, null, 2);
      const blob = new Blob([dataStr], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `transport_analysis_full_${new Date().toISOString().split("T")[0]}.json`;
      a.click();
      URL.revokeObjectURL(url);
      showNotification("JSON экспортирован", "success");
    }
  } catch (e) {
    showNotification(`Ошибка экспорта: ${e.message}`, "error");
  }
});

// Clear functionality
document.getElementById("clearBtn").addEventListener("click", () => {
  if (confirm("Очистить все данные и результаты?")) {
    // Clear map layers
    if (ptsLayer) {
      map.removeLayer(ptsLayer);
      ptsLayer = null;
    }
    if (heatmapLayer) {
      map.removeLayer(heatmapLayer);
      heatmapLayer = null;
    }
    polyLayers.forEach((layer) => map.removeLayer(layer));
    polyLayers = [];
    if (state.routeLayer) {
      map.removeLayer(state.routeLayer);
      state.routeLayer = null;
    }
    if (state.legend) {
      map.removeControl(state.legend);
      state.legend = null;
    }

    // Clear chart
    if (chart) {
      chart.destroy();
      chart = null;
    }

    // Clear state
    state.rawGeoJson = null;
    state.analysisResult = null;
    state.filteredPoints = null;
    state.heatmapData = null;
    state.pendingAnalysis = null;

    // Clear inputs
    document.getElementById("fileInput").value = "";
    document.getElementById("start").value = "";
    document.getElementById("end").value = "";

    // Reset stats
    document.getElementById("avg").innerText = "—";
    document.getElementById("dropped").innerText = "—";
    document.getElementById("count").innerText = "—";
    document.getElementById("maxSpeed").innerText = "—";
    document.getElementById("minSpeed").innerText = "—";
    document.getElementById("totalDistance").innerText = "—";

    // Hide stats, filters, and visualization
    document.getElementById("statsSection").style.display = "none";
    document.getElementById("filters").style.display = "none";
    document.getElementById("visualizationSection").style.display = "none";
    document.getElementById("exportBtn").disabled = true;
    document.getElementById("analyzeBtn").disabled = true;

    showNotification("Данные очищены", "info");
  }
});

// Speed filter functionality
const speedFilterMin = document.getElementById("speedFilterMin");
const speedFilterMax = document.getElementById("speedFilterMax");
const speedFilterMinDisplay = document.getElementById("speedFilterMinDisplay");
const speedFilterMaxDisplay = document.getElementById("speedFilterMaxDisplay");

speedFilterMin.addEventListener("input", (e) => {
  const value = parseInt(e.target.value);
  speedFilterMinDisplay.textContent = value;
  if (value > parseInt(speedFilterMax.value)) {
    speedFilterMax.value = value;
    speedFilterMaxDisplay.textContent = value;
  }
});

speedFilterMax.addEventListener("input", (e) => {
  const value = parseInt(e.target.value);
  speedFilterMaxDisplay.textContent = value;
  if (value < parseInt(speedFilterMin.value)) {
    speedFilterMin.value = value;
    speedFilterMinDisplay.textContent = value;
  }
});

// Set max value for speed filters based on data
function updateSpeedFilterRange() {
  if (!state.analysisResult) return;
  // Use raw speeds for filter range if available
  const speeds = state.analysisResult.plot.raw_speeds || state.analysisResult.plot.speeds || [];
  if (speeds.length === 0) return;

  const maxSpeed = Math.ceil(Math.max(...speeds));
  speedFilterMin.max = maxSpeed;
  speedFilterMax.max = maxSpeed;
  speedFilterMax.value = maxSpeed;
  speedFilterMaxDisplay.textContent = maxSpeed;
}

document.getElementById("applyFilterBtn").addEventListener("click", () => {
  if (!state.filteredPoints || !state.analysisResult) {
    showNotification("Нет данных для фильтрации", "warning");
    return;
  }

  const minSpeed = parseFloat(speedFilterMin.value);
  const maxSpeed = parseFloat(speedFilterMax.value);

  const filtered = state.filteredPoints.filter((f) => {
    const speed = f.properties?.speed || 0;
    return speed >= minSpeed && speed <= maxSpeed;
  });

  // Update visualization with filtered data
  const filteredGeoJson = {
    type: "FeatureCollection",
    features: filtered
  };
  
  // Update heatmap data if in heatmap mode
  if (currentVisualizationMode === "heatmap" && state.heatmapData) {
    const filteredHeatmapData = state.heatmapData.filter(([lat, lon]) => {
      return filtered.some(f => {
        const coords = f.geometry?.coordinates;
        return coords && Math.abs(coords[1] - lat) < 0.0001 && Math.abs(coords[0] - lon) < 0.0001;
      });
    });
    state.heatmapData = filteredHeatmapData;
  }
  
  updateVisualization({ filtered_geojson: filteredGeoJson }, currentVisualizationMode);

  showNotification(
    `Отфильтровано: ${filtered.length} из ${state.filteredPoints.length} точек`,
    "success"
  );
});

document.getElementById("resetFilterBtn").addEventListener("click", () => {
  if (!state.analysisResult) return;
  speedFilterMin.value = 0;
  speedFilterMax.value = 100;
  speedFilterMinDisplay.textContent = "0";
  speedFilterMaxDisplay.textContent = "100";
  updateSpeedFilterRange();
  showResults(state.analysisResult);
  showNotification("Фильтр сброшен", "info");
});

// Initialize Web Worker
function initWorker() {
  if (typeof Worker !== "undefined") {
    try {
      worker = new Worker("worker.js");
      worker.onmessage = handleWorkerMessage;
      worker.onerror = handleWorkerError;
    } catch (e) {
      console.warn("Web Worker not available, falling back to main thread:", e);
    }
  }
}

function handleWorkerMessage(e) {
  const { type, data, error } = e.data;

  if (type === "progress") {
    updateProgress(data);
  } else if (type === "complete") {
    handleWorkerComplete(data);
  } else if (type === "error") {
    showNotification(`Ошибка обработки: ${error}`, "error");
    hideProgress();
    
    // Reset button state
    const btn = document.getElementById("analyzeBtn");
    const btnText = document.getElementById("analyzeBtnText");
    const btnLoader = document.getElementById("analyzeBtnLoader");
    if (btn) btn.disabled = false;
    if (btnText) btnText.style.display = "inline";
    if (btnLoader) btnLoader.style.display = "none";
  }
}

function handleWorkerError(error) {
  console.error("Worker error:", error);
  showNotification("Ошибка Web Worker, переключение на обычный режим", "warning");
  hideProgress();
  
  // Reset button state
  const btn = document.getElementById("analyzeBtn");
  const btnText = document.getElementById("analyzeBtnText");
  const btnLoader = document.getElementById("analyzeBtnLoader");
  if (btn) btn.disabled = false;
  if (btnText) btnText.style.display = "inline";
  if (btnLoader) btnLoader.style.display = "none";
  
  // Disable worker for this session
  worker = null;
}

function updateProgress(progressData) {
  const { stage, progress, current, total } = progressData;
  const progressBar = document.getElementById("progressBar");
  const progressText = document.getElementById("progressText");
  const progressStage = document.getElementById("progressStage");

  if (progressBar) {
    progressBar.style.width = `${Math.min(100, Math.max(0, progress))}%`;
  }
  if (progressText) {
    progressText.textContent = `${Math.round(progress)}%`;
  }
  if (progressStage) {
    progressStage.textContent = `${stage} (${current}/${total})`;
  }
}

function showProgress() {
  const container = document.getElementById("progressContainer");
  if (container) {
    container.style.display = "block";
  }
}

function hideProgress() {
  const container = document.getElementById("progressContainer");
  if (container) {
    container.style.display = "none";
  }
  updateProgress({ stage: "", progress: 0, current: 0, total: 0 });
}

function handleWorkerComplete(processedData) {
  // Log statistics
  if (processedData.stats) {
    const stats = processedData.stats;
    console.log("Обработка завершена:", {
      всего: stats.total,
      обработано: stats.processed,
      прошло_фильтр: stats.filtered,
      пропущено: stats.skipped,
      детали: stats.details
    });

    // Show warnings if any
    if (stats.warnings && stats.warnings.length > 0) {
      stats.warnings.forEach(warning => {
        showNotification(warning, "warning");
      });
    }

    // Show detailed info if all points were filtered
    if (stats.filtered === 0 && stats.details) {
      const details = stats.details;
      const reasons = [];
      if (details.invalidGeometry > 0) reasons.push(`неверная геометрия: ${details.invalidGeometry}`);
      if (details.noTime > 0) reasons.push(`нет поля 'time': ${details.noTime}`);
      if (details.noSpeed > 0) reasons.push(`нет поля 'speed': ${details.noSpeed}`);
      if (details.timeFiltered > 0) reasons.push(`отфильтровано по времени: ${details.timeFiltered}`);
      if (details.zeroSpeedFiltered > 0) reasons.push(`нулевая скорость: ${details.zeroSpeedFiltered}`);
      
      if (reasons.length > 0) {
        console.warn("Причины фильтрации:", reasons.join(", "));
      }
    }
  }

  // Continue with analysis using processed data
  continueAnalysis(processedData);
}

// Diagnostic function
function diagnoseGeoJSON() {
  if (!state.rawGeoJson) {
    showNotification("Сначала загрузите GeoJSON файл", "warning");
    return;
  }

  const resultsDiv = document.getElementById("diagnosticResults");
  resultsDiv.style.display = "block";
  resultsDiv.innerHTML = "<div class='loading'>Анализ данных...</div>";

  // Use setTimeout to allow UI to update
  setTimeout(() => {
    const diagnosis = performDiagnosis(state.rawGeoJson);
    displayDiagnosticResults(diagnosis);
  }, 100);
}

function performDiagnosis(geojson) {
  const result = {
    valid: true,
    errors: [],
    warnings: [],
    stats: {},
    samples: []
  };

  // Check structure
  if (!geojson || typeof geojson !== "object") {
    result.valid = false;
    result.errors.push("Неверный формат JSON");
    return result;
  }

  if (geojson.type !== "FeatureCollection") {
    result.valid = false;
    result.errors.push(`Ожидается FeatureCollection, получен: ${geojson.type || "неизвестно"}`);
    return result;
  }

  const features = geojson.features || [];
  result.stats.totalFeatures = features.length;

  if (features.length === 0) {
    result.valid = false;
    result.errors.push("Файл не содержит объектов (features)");
    return result;
  }

  // Analyze features
  let validPoints = 0;
  let hasTime = 0;
  let hasSpeed = 0;
  let validCoordinates = 0;
  let timeFormats = new Set();
  let speedValues = [];
  const samples = [];

  for (let i = 0; i < Math.min(features.length, 1000); i++) {
    const feat = features[i];
    const sample = { index: i, valid: true, issues: [] };

    // Check geometry
    if (!feat.geometry) {
      sample.valid = false;
      sample.issues.push("Нет geometry");
      samples.push(sample);
      continue;
    }

    if (feat.geometry.type !== "Point") {
      sample.valid = false;
      sample.issues.push(`Тип геометрии: ${feat.geometry.type} (ожидается Point)`);
      samples.push(sample);
      continue;
    }

    const coords = feat.geometry.coordinates;
    if (!coords || coords.length < 2) {
      sample.valid = false;
      sample.issues.push("Неверные координаты");
      samples.push(sample);
      continue;
    }

    const lon = coords[0];
    const lat = coords[1];
    if (!(-180 <= lon && lon <= 180) || !(-90 <= lat && lat <= 90)) {
      sample.valid = false;
      sample.issues.push(`Неверные координаты: lon=${lon}, lat=${lat}`);
      samples.push(sample);
      continue;
    }

    validCoordinates++;
    sample.coords = { lon, lat };

    // Check properties
    const props = feat.properties || {};
    sample.properties = {};

    // Check time
    if (props.time === undefined || props.time === null) {
      sample.issues.push("Нет поля 'time'");
    } else {
      hasTime++;
      const timeStr = String(props.time).trim();
      timeFormats.add(timeStr.substring(0, 20)); // First 20 chars to identify format
      
      // Parse date with support for DD.MM.YYYY format
      let dt = null;
      const ddmmyyyyMatch = timeStr.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?/);
      if (ddmmyyyyMatch) {
        const [, day, month, year, hour, minute, second] = ddmmyyyyMatch;
        dt = new Date(
          parseInt(year),
          parseInt(month) - 1, // Month is 0-indexed
          parseInt(day),
          parseInt(hour),
          parseInt(minute),
          second ? parseInt(second) : 0
        );
      } else {
        dt = new Date(timeStr);
      }
      
      if (!dt || isNaN(dt.getTime())) {
        sample.issues.push(`Неверный формат времени: ${timeStr}`);
      } else {
        sample.properties.time = dt.toISOString();
        sample.properties.timeOriginal = timeStr;
      }
    }

    // Check speed
    if (props.speed === undefined || props.speed === null) {
      sample.issues.push("Нет поля 'speed'");
    } else {
      hasSpeed++;
      const speed = parseFloat(props.speed);
      if (isNaN(speed)) {
        sample.issues.push(`Неверный формат скорости: ${props.speed}`);
      } else {
        speedValues.push(speed);
        sample.properties.speed = speed;
      }
    }

    // Store other properties
    Object.keys(props).forEach(key => {
      if (key !== "time" && key !== "speed") {
        sample.properties[key] = props[key];
      }
    });

    if (sample.valid && sample.issues.length === 0) {
      validPoints++;
    }

    if (samples.length < 5 || sample.issues.length > 0) {
      samples.push(sample);
    }
  }

  result.stats.validPoints = validPoints;
  result.stats.hasTime = hasTime;
  result.stats.hasSpeed = hasSpeed;
  result.stats.validCoordinates = validCoordinates;
  result.samples = samples.slice(0, 10);

  // Calculate percentages
  const timePercent = (hasTime / features.length) * 100;
  const speedPercent = (hasSpeed / features.length) * 100;
  const validPercent = (validPoints / features.length) * 100;

  // Add warnings
  if (timePercent < 50) {
    result.warnings.push(`Только ${timePercent.toFixed(1)}% точек имеют поле 'time'`);
  }
  if (speedPercent < 50) {
    result.warnings.push(`Только ${speedPercent.toFixed(1)}% точек имеют поле 'speed'`);
  }
  if (validPercent < 10) {
    result.warnings.push(`Только ${validPercent.toFixed(1)}% точек полностью валидны`);
  }

  // Time range analysis
  if (hasTime > 0) {
    const times = [];
    features.forEach(feat => {
      if (feat.properties && feat.properties.time) {
        // Parse date with support for DD.MM.YYYY format
        let dt = null;
        const timeStr = String(feat.properties.time).trim();
        const ddmmyyyyMatch = timeStr.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?/);
        if (ddmmyyyyMatch) {
          const [, day, month, year, hour, minute, second] = ddmmyyyyMatch;
          dt = new Date(
            parseInt(year),
            parseInt(month) - 1,
            parseInt(day),
            parseInt(hour),
            parseInt(minute),
            second ? parseInt(second) : 0
          );
        } else {
          dt = new Date(timeStr);
        }
        if (dt && !isNaN(dt.getTime())) {
          times.push(dt);
        }
      }
    });

    if (times.length > 0) {
      times.sort((a, b) => a - b);
      result.stats.timeRange = {
        min: times[0].toISOString(),
        max: times[times.length - 1].toISOString(),
        count: times.length
      };
    }
  }

  // Speed statistics
  if (speedValues.length > 0) {
    speedValues.sort((a, b) => a - b);
    result.stats.speedRange = {
      min: speedValues[0],
      max: speedValues[speedValues.length - 1],
      avg: speedValues.reduce((a, b) => a + b, 0) / speedValues.length,
      count: speedValues.length
    };
  }

  result.stats.timeFormats = Array.from(timeFormats).slice(0, 5);

  return result;
}

function displayDiagnosticResults(diagnosis) {
  const resultsDiv = document.getElementById("diagnosticResults");
  let html = "<div class='diagnostic-panel'>";

  // Errors
  if (diagnosis.errors.length > 0) {
    html += "<div class='diagnostic-section error'>";
    html += "<h4>❌ Критические ошибки:</h4><ul>";
    diagnosis.errors.forEach(err => {
      html += `<li>${err}</li>`;
    });
    html += "</ul></div>";
  }

  // Warnings
  if (diagnosis.warnings.length > 0) {
    html += "<div class='diagnostic-section warning'>";
    html += "<h4>⚠️ Предупреждения:</h4><ul>";
    diagnosis.warnings.forEach(warn => {
      html += `<li>${warn}</li>`;
    });
    html += "</ul></div>";
  }

  // Statistics
  html += "<div class='diagnostic-section'>";
  html += "<h4>📊 Статистика:</h4>";
  html += `<p><strong>Всего объектов:</strong> ${diagnosis.stats.totalFeatures || 0}</p>`;
  html += `<p><strong>Валидных точек:</strong> ${diagnosis.stats.validPoints || 0} (${((diagnosis.stats.validPoints / diagnosis.stats.totalFeatures) * 100).toFixed(1)}%)</p>`;
  html += `<p><strong>С полем 'time':</strong> ${diagnosis.stats.hasTime || 0} (${((diagnosis.stats.hasTime / diagnosis.stats.totalFeatures) * 100).toFixed(1)}%)</p>`;
  html += `<p><strong>С полем 'speed':</strong> ${diagnosis.stats.hasSpeed || 0} (${((diagnosis.stats.hasSpeed / diagnosis.stats.totalFeatures) * 100).toFixed(1)}%)</p>`;

  if (diagnosis.stats.timeRange) {
    html += `<p><strong>Временной диапазон:</strong> ${new Date(diagnosis.stats.timeRange.min).toLocaleString("ru-RU")} - ${new Date(diagnosis.stats.timeRange.max).toLocaleString("ru-RU")}</p>`;
  }

  if (diagnosis.stats.speedRange) {
    html += `<p><strong>Скорость:</strong> мин=${diagnosis.stats.speedRange.min.toFixed(1)}, макс=${diagnosis.stats.speedRange.max.toFixed(1)}, средняя=${diagnosis.stats.speedRange.avg.toFixed(1)} км/ч</p>`;
  }
  html += "</div>";

  // Samples
  if (diagnosis.samples.length > 0) {
    html += "<div class='diagnostic-section'>";
    html += "<h4>🔍 Примеры точек:</h4>";
    html += "<div class='diagnostic-samples'>";
    diagnosis.samples.slice(0, 3).forEach(sample => {
      html += "<div class='sample-item'>";
      html += `<p><strong>Точка #${sample.index}:</strong></p>`;
      if (sample.coords) {
        html += `<p>Координаты: ${sample.coords.lat.toFixed(6)}, ${sample.coords.lon.toFixed(6)}</p>`;
      }
      if (sample.properties) {
        if (sample.properties.time) {
          html += `<p>Время: ${sample.properties.timeOriginal || sample.properties.time}</p>`;
        }
        if (sample.properties.speed !== undefined) {
          html += `<p>Скорость: ${sample.properties.speed} км/ч</p>`;
        }
      }
      if (sample.issues.length > 0) {
        html += `<p class='sample-issues'>Проблемы: ${sample.issues.join(", ")}</p>`;
      }
      html += "</div>";
    });
    html += "</div></div>";
  }

  html += "</div>";
  resultsDiv.innerHTML = html;
}

// Diagnostic button handler
document.getElementById("diagnoseBtn")?.addEventListener("click", diagnoseGeoJSON);

// Initialize
initWorker();
document.getElementById("analyzeBtn").disabled = true;
document.getElementById("exportBtn").disabled = true;
