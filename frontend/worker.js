// frontend/worker.js - Web Worker for data processing

// Helper function to calculate distance
function haversineDistance(lat1, lon1, lat2, lon2) {
  const R = 6371000; // Earth radius in meters
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) * Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

// Process GeoJSON data
function processGeoJSON(data, options) {
  const {
    start,
    end,
    include_zero,
    speed_thresh,
    onProgress
  } = options;

  const features = data.features || [];
  const total = features.length;
  let processed = 0;
  const filtered = [];
  const speeds = [];
  const heatmapData = [];
  
  // Statistics for debugging
  const stats = {
    total: total,
    invalidGeometry: 0,
    invalidCoordinates: 0,
    noTime: 0,
    invalidTime: 0,
    timeFiltered: 0,
    noSpeed: 0,
    zeroSpeedFiltered: 0,
    valid: 0
  };

  // Parse datetime strings
  const startDt = start ? new Date(start) : null;
  const endDt = end ? new Date(end) : null;

  for (let i = 0; i < features.length; i++) {
    const feat = features[i];
    const geom = feat.geometry;
    const props = feat.properties || {};

    // Validate geometry
    if (!geom || geom.type !== "Point") {
      stats.invalidGeometry++;
      processed++;
      if (processed % 100 === 0) {
        onProgress({
          stage: "Фильтрация данных",
          progress: (processed / total) * 100,
          current: processed,
          total: total
        });
      }
      continue;
    }

    const coords = geom.coordinates;
    if (!coords || coords.length < 2) {
      stats.invalidCoordinates++;
      processed++;
      continue;
    }

    const lon = coords[0];
    const lat = coords[1];

    // Validate coordinates
    if (!(-180 <= lon && lon <= 180) || !(-90 <= lat && lat <= 90)) {
      stats.invalidCoordinates++;
      processed++;
      continue;
    }

    // Parse time
    const rawTime = props.time;
    if (!rawTime) {
      stats.noTime++;
      processed++;
      continue;
    }

    // Parse date with support for DD.MM.YYYY format
    let dt = null;
    const timeStr = String(rawTime).trim();
    
    // Try parsing with different formats
    // Format: DD.MM.YYYY HH:mm:ss or DD.MM.YYYY HH:mm
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
      // Try standard Date parsing
      dt = new Date(timeStr);
    }

    if (!dt || isNaN(dt.getTime())) {
      stats.invalidTime++;
      processed++;
      continue;
    }

    // Apply time filter
    if (startDt && dt < startDt) {
      stats.timeFiltered++;
      processed++;
      continue;
    }
    if (endDt && dt > endDt) {
      stats.timeFiltered++;
      processed++;
      continue;
    }

    // Parse speed
    let speed = 0;
    if (props.speed === undefined || props.speed === null) {
      stats.noSpeed++;
      processed++;
      continue;
    }
    
    try {
      speed = parseFloat(props.speed);
      if (isNaN(speed) || speed < 0) speed = 0;
    } catch (e) {
      stats.noSpeed++;
      processed++;
      continue;
    }

    // Apply zero speed filter
    if (!include_zero && speed <= 1.0) {
      stats.zeroSpeedFiltered++;
      processed++;
      continue;
    }
    
    stats.valid++;

    // Accept point
    filtered.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lon, lat] },
      properties: {
        ...props,
        speed: speed,
        time: dt.toISOString()
      }
    });

    speeds.push(speed);

    // Add to heatmap data (weighted by inverse speed for congestion visualization)
    // Lower speed = higher intensity
    const intensity = speed > 0 ? Math.max(0.1, 1 - (speed / 50)) : 1;
    heatmapData.push([lat, lon, intensity]);

    processed++;

    // Report progress every 100 points
    if (processed % 100 === 0 || processed === total) {
      onProgress({
        stage: "Фильтрация данных",
        progress: (processed / total) * 100,
        current: processed,
        total: total
      });
    }
  }

  // Add warnings if many points were filtered
  const warnings = [];
  if (stats.invalidGeometry > total * 0.5) {
    warnings.push(`Много точек с неверной геометрией: ${stats.invalidGeometry}`);
  }
  if (stats.noTime > total * 0.5) {
    warnings.push(`Много точек без поля 'time': ${stats.noTime}`);
  }
  if (stats.noSpeed > total * 0.5) {
    warnings.push(`Много точек без поля 'speed': ${stats.noSpeed}`);
  }
  if (stats.timeFiltered > total * 0.5) {
    warnings.push(`Много точек отфильтровано по времени: ${stats.timeFiltered}`);
  }
  
  return {
    filtered,
    speeds,
    heatmapData,
    stats: {
      total: total,
      processed: processed,
      filtered: filtered.length,
      skipped: total - filtered.length,
      details: stats,
      warnings: warnings
    }
  };
}

// Main worker message handler
self.addEventListener("message", function(e) {
  const { type, data, options } = e.data;

  try {
    if (type === "process") {
      if (!data || !data.features) {
        throw new Error("Invalid GeoJSON data");
      }

      const result = processGeoJSON(data, {
        ...options,
        onProgress: (progress) => {
          self.postMessage({
            type: "progress",
            data: progress
          });
        }
      });

      self.postMessage({
        type: "complete",
        data: result
      });
    }
  } catch (error) {
    self.postMessage({
      type: "error",
      error: error.message || String(error)
    });
  }
});
