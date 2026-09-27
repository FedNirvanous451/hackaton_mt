const API = '/api/v1';
const WS_URL = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${API}/ws?mode=live`;
const STATE_KEY = 'dispatcher-state-v1';

function readSavedState() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY)) || {}; }
    catch { return {}; }
}

const savedState = readSavedState();

let map, mapReady = false, mapReadyPromise, activePopup;
let currentSnapshot = null;
let currentSource = savedState.source === 'emulator' ? 'emulator' : 'historical';
let timeline = null;
let replayActive = false;
let replayToken = 0;
let replayTask = null;
let historicalCheckpoint = savedState.historicalAt || null;
let historicalShouldPlay = savedState.historicalPlaying !== false;
let sourceReady = false;
let sourceGeneration = 0;
let emulatorExpectedRunning = false;
let emulatorStartedAt = 0;
let emulatorLastPacketAt = 0;
let emulatorSchedule = savedState.emulatorStops || [];
let fallbackInProgress = false;
let fallbackNotice = '';
let websocketOpened = false;
let websocketConnected = false;
let websocketRecovering = false;
let pendingSnapshots = [];
let selectedVehicleId = null;
let allStops = [];
let stopSites = [];
let emulatorPackets = 0;
let emulatorFitted = false;
const tracks = new Map();
const vehicleMarkers = new Map();

function persistState() {
    const camera = mapReady && sourceReady
        ? { center: map.getCenter().toArray(), zoom: map.getZoom() }
        : savedState.camera;
    try { localStorage.setItem(STATE_KEY, JSON.stringify({ source: currentSource,
        historicalAt: historicalCheckpoint, historicalPlaying: historicalShouldPlay,
        emulatorPackets, emulatorRunning: emulatorExpectedRunning,
        emulatorStops: emulatorSchedule, selectedVehicleId, camera,
        risk: document.getElementById('risk-filter')?.value || 'all',
        showPaths: document.getElementById('show-paths')?.checked ?? true,
        showStops: document.getElementById('show-stops')?.checked ?? true,
        onlySelected: document.getElementById('only-selected')?.checked ?? false })); }
    catch { /* The dashboard still works when browser storage is unavailable. */ }
}

function restoreCamera() {
    const camera = savedState.camera;
    if (mapReady && Array.isArray(camera?.center) && Number.isFinite(camera.zoom)) {
        map.jumpTo({ center: camera.center, zoom: camera.zoom });
    }
}

function initMap() {
    const rasterFallback = { version: 8, sources: { osm: { type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize: 256,
        attribution: '&copy; OpenStreetMap contributors' } },
        layers: [{ id: 'basemap', type: 'raster', source: 'osm' }] };
    map = new maplibregl.Map({
        container: 'map', center: [37.6176, 55.7558], zoom: 10,
        style: 'https://tiles.openfreemap.org/styles/bright'
    });
    let fallbackStarted = false;
    map.on('error', () => {
        if (!mapReady && !fallbackStarted) {
            fallbackStarted = true;
            map.setStyle(rasterFallback);
        }
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
    map.on('moveend', () => { if (sourceReady) persistState(); });
    mapReadyPromise = new Promise(resolve => map.once('load', () => {
        map.addSource('stops', { type: 'geojson', data: emptyFeatures() });
        map.addLayer({ id: 'stops', type: 'circle', source: 'stops', paint: {
            'circle-radius': ['case', ['get', 'selected'], 5, 3],
            'circle-color': ['case', ['get', 'selected'], '#155a93', '#77a7cc'],
            'circle-opacity': ['case', ['get', 'selected'], 0.95, 0.7],
            'circle-stroke-color': ['case', ['get', 'selected'], '#095c9c', '#6199c7'],
            'circle-stroke-width': 1
        } });
        map.addSource('vehicles', { type: 'geojson', data: emptyFeatures() });
        map.addLayer({ id: 'vehicles', type: 'circle', source: 'vehicles', paint: {
            'circle-radius': ['get', 'radius'],
            'circle-color': ['get', 'color'],
            'circle-opacity': ['get', 'opacity'],
            'circle-stroke-color': '#fff', 'circle-stroke-width': 2
        } });
        map.on('click', 'stops', event => {
            const site = stopSites[event.features[0].properties.index];
            if (site) showPopup([site.lon, site.lat],
                `<b>${escapeHtml(site.name)}</b><br>Плановых прибытий: ${site.arrivals.length}`);
        });
        map.on('click', 'vehicles', event => {
            const vehicle = vehicleMarkers.get(event.features[0].properties.vehicle_id);
            if (vehicle) showVehiclePopup(vehicle);
        });
        for (const layer of ['stops', 'vehicles']) {
            map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
            map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
        }
        mapReady = true;
        renderStops();
        renderCurrent();
        resolve();
    }));
}

function emptyFeatures() { return { type: 'FeatureCollection', features: [] }; }

function showPopup(coordinates, html) {
    activePopup?.remove();
    activePopup = new maplibregl.Popup({ maxWidth: '280px' })
        .setLngLat(coordinates).setHTML(html).addTo(map);
}

function fitCoordinates(coordinates, maxZoom) {
    if (!mapReady || !coordinates.length) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const coordinate of coordinates) bounds.extend(coordinate);
    map.fitBounds(bounds, { padding: 35, maxZoom, duration: 0 });
}

function colorFor(vehicleId) {
    let hash = 0;
    for (const char of vehicleId) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
    return `hsl(${(Math.imul(hash, 2654435761) >>> 0) % 360}, 78%, 58%)`;
}

function clearMap() {
    if (mapReady) {
        for (const track of tracks.values()) {
            map.removeLayer(track.layerId);
            map.removeSource(track.sourceId);
        }
        map.getSource('stops').setData(emptyFeatures());
        map.getSource('vehicles').setData(emptyFeatures());
        activePopup?.remove();
        activePopup = null;
        map.jumpTo({ center: [37.6176, 55.7558], zoom: 10 });
    }
    tracks.clear();
    vehicleMarkers.clear();
    selectedVehicleId = null;
    allStops = [];
    stopSites = [];
    emulatorPackets = 0;
    emulatorFitted = false;
}

function appendTrackPoints(points, countPackets = true) {
    const changed = new Set();
    for (const point of points) {
        if (currentSource === 'emulator' && point.source !== 'ndtp') continue;
        if (currentSource === 'historical' && point.source !== 'csv') continue;
        const existing = tracks.get(point.vehicle_id);
        if (existing?.lastPoint && point.event_time < existing.lastPoint.event_time) continue;
        if (currentSource === 'emulator' && countPackets) emulatorPackets++;
        if (!point.location_valid || point.lat == null || point.lon == null) {
            const previous = tracks.get(point.vehicle_id);
            if (previous) previous.breakNext = true;
            continue;
        }
        let track = tracks.get(point.vehicle_id);
        if (!track) {
            track = { segments: [], segment: null, lastKey: null, lastPoint: null,
                breakNext: false, sourceId: `track-${point.vehicle_id}`,
                layerId: `track-layer-${point.vehicle_id}`, styleKey: null };
            tracks.set(point.vehicle_id, track);
        }
        const key = `${point.event_time}:${point.lat}:${point.lon}`;
        if (key !== track.lastKey) {
            const previous = track.lastPoint;
            const gapSeconds = previous
                ? (new Date(point.event_time) - new Date(previous.event_time)) / 1000 : 0;
            const jumpKm = previous ? distanceKm(previous.lat, previous.lon, point.lat, point.lon) : 0;
            if (!track.segment || track.breakNext || gapSeconds < 0 || gapSeconds > 120
                || jumpKm > Math.max(0.3, gapSeconds * 0.06)) {
                track.segment = [];
                track.segments.push(track.segment);
            }
            track.segment.push([point.lon, point.lat]);
            track.lastKey = key;
            track.lastPoint = point;
            track.breakNext = false;
            changed.add(point.vehicle_id);
        }
    }
    if (mapReady) for (const id of changed) syncTrack(id, tracks.get(id));
}

function syncTrack(id, track) {
    const data = { type: 'Feature', geometry: { type: 'MultiLineString',
        coordinates: track.segments.filter(segment => segment.length > 1) }, properties: {} };
    if (!map.getSource(track.sourceId)) {
        map.addSource(track.sourceId, { type: 'geojson', data });
        map.addLayer({ id: track.layerId, type: 'line', source: track.sourceId,
            paint: { 'line-color': colorFor(id), 'line-width': 3, 'line-opacity': 0.75 },
            layout: { 'line-join': 'round', 'line-cap': 'round' } }, 'stops');
    } else map.getSource(track.sourceId).setData(data);
}

function distanceKm(lat1, lon1, lat2, lon2) {
    const radians = Math.PI / 180;
    const a = Math.sin((lat2 - lat1) * radians / 2) ** 2
        + Math.cos(lat1 * radians) * Math.cos(lat2 * radians)
        * Math.sin((lon2 - lon1) * radians / 2) ** 2;
    return 12742 * Math.asin(Math.min(1, Math.sqrt(a)));
}

function formatDuration(value) {
    if (value == null || !Number.isFinite(value)) return '--';
    const total = Math.round(Math.abs(value));
    return `${value < 0 && total > 0 ? '-' : '+'}${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;',
        '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

function formatDateTime(value, includeSeconds = false) {
    if (!value) return '—';
    return `${value.slice(8, 10)}.${value.slice(5, 7)} ${value.slice(11, includeSeconds ? 19 : 16)}`;
}

function formatAge(observedAt, at) {
    if (!observedAt || !at) return '—';
    const seconds = Math.max(0, Math.round((new Date(at) - new Date(observedAt)) / 1000));
    if (seconds < 60) return `${seconds} сек. назад`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} мин. назад`;
    return `${Math.floor(seconds / 3600)} ч. назад`;
}

function stopName(stop) {
    return stop?.address?.trim() || (stop ? `Остановка №${stop.arrival_id}` : '—');
}

const STATUS_LABELS = { ml: 'ML', baseline: 'Базовый', last_known: 'Последний прогноз',
    outside_horizon: 'Ожидание ML', no_target: 'Нет расписания',
    insufficient_data: 'Мало данных', unknown_vehicle: 'ТС не известно' };
const RISK_LABELS = { red: 'Критично', yellow: 'Внимание', green: 'В норме', unknown: 'Без прогноза' };

function filteredVehicles(vehicles) {
    const risk = document.getElementById('risk-filter').value;
    const onlySelected = document.getElementById('only-selected').checked;
    return vehicles.filter(vehicle => (risk === 'all' || vehicle.risk === risk)
        && (!onlySelected || vehicle.vehicle_id === selectedVehicleId));
}

function updateKPI(vehicles, alerts) {
    document.getElementById('kpi-total').textContent = vehicles.length;
    document.getElementById('kpi-online').textContent = vehicles.filter(v => !v.stale && v.lon != null && v.lat != null).length;
    document.getElementById('kpi-alerts').textContent = alerts.length;
    const delays = vehicles.filter(v => v.prediction).map(v => v.prediction.prediction_s);
    document.getElementById('kpi-delay').textContent = delays.length
        ? formatDuration(delays.reduce((sum, value) => sum + value, 0) / delays.length) : '--';
    const stale = vehicles.filter(v => v.stale).length;
    const freshPct = vehicles.length ? Math.round(100 * (vehicles.length - stale) / vehicles.length) : 0;
    const freshness = document.getElementById('kpi-freshness');
    freshness.textContent = `${freshPct}%`;
    freshness.style.color = freshPct < 80 ? 'var(--accent-red)' : 'var(--accent-green)';
}

function renderIncidents(alerts, at) {
    const container = document.getElementById('incidents-list');
    document.getElementById('incident-count').textContent = alerts.length;
    if (!alerts.length) {
        container.innerHTML = '<div class="empty-state">Нет активных предупреждений</div>';
        return;
    }
    const lead = alerts.find(alert => alert.vehicle_id === selectedVehicleId) || alerts[0];
    const rest = alerts.filter(alert => alert !== lead);
    const minutes = Math.max(0, Math.round((new Date(lead.target_stop.planned_at) - new Date(at)) / 60000));
    container.innerHTML = `<div class="incident-card">
        <div class="incident-card-header"><div class="incident-id">Инцидент · ТС ${escapeHtml(lead.tr_id)}</div></div>
        <p class="incident-vehicle">${escapeHtml(stopName(lead.target_stop))}</p>
        <span class="risk-tag ${lead.risk}">${lead.risk === 'red' ? 'Высокий риск задержки' : 'Риск задержки'}</span>
        <dl class="detail-grid">
            <dt>Прогноз</dt><dd>${formatDateTime(lead.predicted_arrival, true)} (${formatDuration(lead.predicted_delay_s)})</dd>
            <dt>План</dt><dd>${formatDateTime(lead.target_stop.planned_at)}</dd>
            <dt>До остановки</dt><dd>${minutes} мин</dd>
            <dt>Остановка</dt><dd>${escapeHtml(stopName(lead.target_stop))}</dd>
        </dl>
        <div class="fact-heading">Наблюдаемые факторы</div>
        <ul class="fact-list">${(lead.patterns?.length ? lead.patterns : ['Отклонение от расписания'])
            .map(pattern => `<li>${escapeHtml(pattern)}</li>`).join('')}</ul>
        <button class="incident-open" data-vehicle-id="${escapeHtml(lead.vehicle_id)}">Показать ТС на карте</button>
    </div>${rest.map(alert => `<button class="incident-mini" data-vehicle-id="${escapeHtml(alert.vehicle_id)}">
        ТС ${escapeHtml(alert.tr_id)} · ${escapeHtml(stopName(alert.target_stop))}
        <span>${formatDuration(alert.predicted_delay_s)}</span></button>`).join('')}`;
}

function renderVehicles(vehicles) {
    document.getElementById('table-count').textContent = vehicles.length;
    document.getElementById('vehicles-table-body').innerHTML = vehicles.map(v => {
        const prediction = v.prediction;
        const status = STATUS_LABELS[v.forecast_status] || v.forecast_status;
        return `<tr data-vehicle-id="${escapeHtml(v.vehicle_id)}" tabindex="0" class="${selectedVehicleId === v.vehicle_id ? 'selected' : ''}">
            <td><strong>${escapeHtml(v.tr_id ?? v.unit_id ?? '—')}</strong></td>
            <td><span class="stop-name" title="${escapeHtml(stopName(v.target_arrival))}">${escapeHtml(stopName(v.target_arrival))}</span></td>
            <td>${formatDateTime(v.target_arrival?.planned_at)}</td>
            <td>${prediction ? `${formatDateTime(prediction.predicted_arrival, true)}
                <span class="subvalue">${formatDuration(prediction.prediction_s)} к плану</span>` : '—'}</td>
            <td>${v.speed_kmh == null ? '—' : `${Math.round(v.speed_kmh)} км/ч`}</td>
            <td>${formatDuration(v.schedule_deviation_s)}</td>
            <td><span class="risk-cell"><span class="risk-dot ${v.risk}"></span>${RISK_LABELS[v.risk]}</span></td>
            <td><span class="status-badge ${v.forecast_status}">${escapeHtml(status)}</span></td>
            <td>${formatDateTime(v.observed_at)}<span class="subvalue ${v.stale ? 'stale-text' : ''}">
                ${formatAge(v.observed_at, currentSnapshot?.at)}</span></td>
        </tr>`;
    }).join('');
}

function renderStops() {
    if (!mapReady) return;
    const visible = document.getElementById('show-stops').checked;
    map.setLayoutProperty('stops', 'visibility', visible ? 'visible' : 'none');
    if (!visible) return;
    const onlySelected = document.getElementById('only-selected').checked;
    const features = [];
    for (const [index, site] of stopSites.entries()) {
        if (onlySelected && !site.trIds.has(Number(selectedVehicleId))) continue;
        features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [site.lon, site.lat] },
            properties: { index, selected: Boolean(selectedVehicleId && site.trIds.has(Number(selectedVehicleId))) } });
    }
    map.getSource('stops').setData({ type: 'FeatureCollection', features });
}

async function loadStops(mode) {
    const stops = await requestJson(`/stops?mode=${mode}`);
    if ((mode === 'historical') !== (currentSource === 'historical')) return;
    if (mode === 'live' && stops.length) emulatorSchedule = stops;
    allStops = stops;
    const byLocation = new Map();
    for (const stop of allStops) {
        const key = `${stop.lat.toFixed(6)}:${stop.lon.toFixed(6)}`;
        let site = byLocation.get(key);
        if (!site) {
            site = { lat: stop.lat, lon: stop.lon, name: stopName(stop), arrivals: [], trIds: new Set() };
            byLocation.set(key, site);
        }
        if (site.name.startsWith('Остановка №') && stop.address) site.name = stop.address;
        site.arrivals.push(stop);
        site.trIds.add(stop.tr_id);
    }
    stopSites = [...byLocation.values()];
    document.getElementById('stop-count').textContent = `Пути ТС · ${stopSites.length.toLocaleString('ru-RU')} остановок на карте`;
    await mapReadyPromise;
    if ((mode === 'historical') !== (currentSource === 'historical')) return;
    renderStops();
    fitCoordinates(stopSites.map(site => [site.lon, site.lat]), 11);
}

function renderMarkers(vehicles) {
    vehicleMarkers.clear();
    const features = [];
    for (const vehicle of vehicles) {
        if (vehicle.lon == null || vehicle.lat == null) continue;
        const color = vehicle.stale ? '#8293a2' : vehicle.risk === 'red' ? '#d93e42'
            : vehicle.risk === 'yellow' ? '#d99319' : '#1e9d5b';
        features.push({ type: 'Feature', geometry: { type: 'Point',
            coordinates: [vehicle.lon, vehicle.lat] }, properties: {
            vehicle_id: vehicle.vehicle_id, color, radius: vehicle.stale ? 6 : 8,
            opacity: vehicle.stale ? 0.7 : 0.96 } });
        vehicleMarkers.set(vehicle.vehicle_id, vehicle);
    }
    if (mapReady) map.getSource('vehicles').setData({ type: 'FeatureCollection', features });
}

function showVehiclePopup(vehicle) {
    showPopup([vehicle.lon, vehicle.lat], `<b>ТС ${escapeHtml(vehicle.tr_id ?? vehicle.unit_id)}</b><br>
        ${escapeHtml(stopName(vehicle.target_arrival))}<br>
        Прогноз: ${formatDateTime(vehicle.prediction?.predicted_arrival, true)}<br>
        Скорость: ${vehicle.speed_kmh ?? '—'} км/ч<br>
        ${vehicle.stale ? 'Положение по последнему пакету: ' : 'Данные: '}${formatDateTime(vehicle.observed_at, true)}`);
}

window.focusVehicle = function(vehicleId) {
    selectedVehicleId = vehicleId;
    renderStops();
    renderCurrent();
    const vehicle = vehicleMarkers.get(vehicleId);
    if (vehicle && mapReady) {
        showVehiclePopup(vehicle);
        map.easeTo({ center: [vehicle.lon, vehicle.lat],
            zoom: Math.max(map.getZoom(), currentSource === 'emulator' ? 16 : 13), duration: 300 });
    }
    persistState();
};

function showAllVehicles() {
    selectedVehicleId = null;
    document.getElementById('only-selected').checked = false;
    activePopup?.remove();
    renderStops();
    renderCurrent();
    const coordinates = [];
    for (const vehicle of vehicleMarkers.values()) coordinates.push([vehicle.lon, vehicle.lat]);
    for (const track of tracks.values()) {
        for (const segment of track.segments) coordinates.push(...segment);
    }
    if (!coordinates.length) for (const site of stopSites) coordinates.push([site.lon, site.lat]);
    fitCoordinates(coordinates, 13);
    persistState();
}

function renderCurrent() {
    if (!currentSnapshot) return;
    const snapshot = currentSnapshot;
    const vehicles = currentSource === 'emulator'
        ? snapshot.vehicles.filter(vehicle => vehicle.source === 'ndtp') : snapshot.vehicles;
    if (document.getElementById('only-selected').checked && !selectedVehicleId && vehicles.length) {
        selectedVehicleId = vehicles[0].vehicle_id;
        renderStops();
    }
    const allIds = new Set(vehicles.map(vehicle => vehicle.vehicle_id));
    const allAlerts = snapshot.alerts.filter(alert => allIds.has(alert.vehicle_id));
    const visibleVehicles = filteredVehicles(vehicles);
    const visibleIds = new Set(visibleVehicles.map(vehicle => vehicle.vehicle_id));
    updateKPI(vehicles, allAlerts);
    renderIncidents(allAlerts.filter(alert => visibleIds.has(alert.vehicle_id)), snapshot.at);
    renderVehicles(visibleVehicles);
    renderMarkers(visibleVehicles);
    const showPaths = document.getElementById('show-paths').checked;
    const riskFilter = document.getElementById('risk-filter').value;
    const onlySelected = document.getElementById('only-selected').checked;
    if (mapReady) for (const [id, track] of tracks) {
        if (!map.getLayer(track.layerId)) syncTrack(id, track);
        const filteredOut = riskFilter !== 'all' && !visibleIds.has(id);
        const selectedOut = onlySelected && id !== selectedVehicleId;
        const width = id === selectedVehicleId ? 5 : 3;
        const opacity = showPaths && !filteredOut && !selectedOut
            ? (selectedVehicleId && id !== selectedVehicleId ? 0.25 : 0.75) : 0;
        const styleKey = `${width}:${opacity}`;
        if (styleKey !== track.styleKey) {
            map.setPaintProperty(track.layerId, 'line-width', width);
            map.setPaintProperty(track.layerId, 'line-opacity', opacity);
            track.styleKey = styleKey;
        }
    }
    if (currentSource === 'historical') {
        document.getElementById('clock').textContent = new Date(snapshot.at).toLocaleString('ru-RU');
    }
    if (mapReady && currentSource === 'emulator' && !emulatorFitted && vehicles.length >= 3) {
        const locations = [...vehicleMarkers.values()].map(vehicle => [vehicle.lon, vehicle.lat]);
        fitCoordinates(locations, 15);
        emulatorFitted = true;
    }
    document.getElementById('mode-label').textContent = currentSource === 'historical' ? 'ИСТОРИЯ' : 'ЭМУЛЯТОР';
    if (currentSource === 'historical' && timeline) {
        document.getElementById('replay-progress').value = snapshot.processed_packets;
        setStatus(`${snapshot.processed_packets.toLocaleString('ru-RU')} / ${timeline.packet_count.toLocaleString('ru-RU')} пакетов · ${snapshot.at.replace('T', ' ')}`);
    } else if (currentSource === 'emulator') {
        setStatus(`${emulatorPackets.toLocaleString('ru-RU')} пакетов NDTP · ${vehicles.length} ТС`);
    }
    if (mapReady) requestAnimationFrame(() => map.resize());
}

function applySnapshot(snapshot) {
    if (currentSource === 'historical' && snapshot.mode !== 'historical') return;
    if (currentSource === 'emulator' && snapshot.mode !== 'live') return;
    currentSnapshot = snapshot;
    if (currentSource === 'historical') historicalCheckpoint = snapshot.at;
    if (currentSource === 'emulator' && (snapshot.track_points || []).some(point => point.source === 'ndtp')) {
        emulatorLastPacketAt = Date.now();
    }
    appendTrackPoints(snapshot.track_points || []);
    renderCurrent();
    persistState();
}

function setStatus(message, error = false) {
    const element = document.getElementById('stream-status');
    element.textContent = fallbackNotice && currentSource === 'historical'
        ? `${fallbackNotice} · ${message}` : message;
    element.classList.toggle('status-error', error);
}

async function requestJson(path, options = {}) {
    const response = await fetch(`${API}${path}`, options);
    if (!response.ok) {
        const body = await response.text();
        const error = new Error(`${response.status}: ${body.slice(0, 180)}`);
        error.status = response.status;
        throw error;
    }
    return response.json();
}

function pauseHistorical(discardInFlight = false) {
    replayActive = false;
    if (discardInFlight) replayToken++;
}

function startHistorical() {
    historicalShouldPlay = true;
    persistState();
    if (replayTask) return replayTask;
    replayTask = runHistorical().finally(() => { replayTask = null; });
    return replayTask;
}

async function resumeHistorical() {
    if (historicalCheckpoint) await restoreHistorical(historicalCheckpoint, false);
    return startHistorical();
}

async function runHistorical() {
    if (replayActive) return;
    replayActive = true;
    const token = ++replayToken;
    document.getElementById('start-stream').disabled = true;
    document.getElementById('pause-stream').disabled = false;
    try {
        while (replayActive && token === replayToken && currentSource === 'historical') {
            const advance = Number(document.getElementById('replay-speed').value);
            const snapshot = await requestJson(`/replay/step?advance_s=${advance}`, { method: 'POST' });
            if (!replayActive || token !== replayToken || currentSource !== 'historical') break;
            applySnapshot(snapshot);
            if (!replayActive) break;
            if (snapshot.at >= timeline.end) {
                historicalShouldPlay = false;
                persistState();
                setStatus(`Воспроизведены все ${timeline.packet_count.toLocaleString('ru-RU')} пакетов`);
                break;
            }
            await new Promise(resolve => setTimeout(resolve, 120));
        }
    } catch (error) {
        setStatus(`Ошибка исторического потока: ${error.message}`, true);
    } finally {
        if (token === replayToken) {
            replayActive = false;
            document.getElementById('start-stream').disabled = false;
            document.getElementById('pause-stream').disabled = true;
        }
    }
}

async function startEmulator() {
    await requestJson('/emulator/start', { method: 'POST' });
    emulatorExpectedRunning = true;
    emulatorStartedAt = Date.now();
    emulatorLastPacketAt = 0;
    await loadStops('live');
    document.getElementById('start-stream').disabled = true;
    document.getElementById('pause-stream').disabled = false;
    setStatus('Эмулятор запущен · ожидание пакетов NDTP');
}

async function restoreHistorical(at, rebuildPaths) {
    if (rebuildPaths) {
        for (let offset = 0; ; offset += 5000) {
            const page = await requestJson(`/replay/tracks?until=${encodeURIComponent(at)}&offset=${offset}&limit=5000`);
            appendTrackPoints(page);
            if (page.length < 5000) break;
        }
    }
    const snapshot = await requestJson(`/replay/step?at=${encodeURIComponent(at)}`, { method: 'POST' });
    applySnapshot({ ...snapshot, track_points: [] });
}

async function restoreLive(rebuildPaths) {
    const points = await requestJson('/tracks?source=ndtp');
    if (rebuildPaths) {
        appendTrackPoints(points, false);
    } else {
        appendTrackPoints(points.filter(point => {
            const last = tracks.get(point.vehicle_id)?.lastPoint;
            return !last || point.event_time > last.event_time;
        }));
    }
    const snapshot = await requestJson('/snapshot?mode=live');
    applySnapshot({ ...snapshot, track_points: [] });
    emulatorLastPacketAt = Date.now();
}

async function restoreEmulatorSchedule() {
    const stops = await requestJson('/stops?mode=live');
    if (stops.length) return;
    if (!emulatorSchedule.length) {
        await requestJson('/emulator/start', { method: 'POST' });
        return;
    }
    for (const trId of new Set(emulatorSchedule.map(stop => stop.tr_id))) {
        await requestJson('/registrations', { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tr_id: trId, unit_id: trId }) });
    }
    for (const stop of emulatorSchedule) {
        await requestJson('/arrivals', { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(stop) });
    }
}

async function selectSource(source, { restore = false, reason = '' } = {}) {
    const generation = ++sourceGeneration;
    sourceReady = false;
    pauseHistorical(true);
    if (replayTask) await replayTask.catch(() => {});
    if (generation !== sourceGeneration) return;
    currentSource = source;
    currentSnapshot = null;
    emulatorExpectedRunning = false;
    if (!restore && source === 'historical') {
        historicalCheckpoint = null;
        historicalShouldPlay = true;
    }
    if (!reason) fallbackNotice = '';
    clearMap();
    document.getElementById('stop-count').textContent = 'Остановки загружаются...';
    updateKPI([], []);
    renderIncidents([], new Date().toISOString());
    renderVehicles([]);
    setStatus('Подключение к источнику...');
    document.getElementById('mode-label').textContent = source === 'historical' ? 'ИСТОРИЯ' : 'ЭМУЛЯТОР';
    document.getElementById('replay-speed').hidden = source !== 'historical';
    document.getElementById('replay-progress').hidden = source !== 'historical';
    document.getElementById('start-stream').disabled = false;
    document.getElementById('pause-stream').disabled = true;
    if (source === 'historical') {
        await requestJson('/emulator/stop', { method: 'POST' }).catch(() => {});
        if (generation !== sourceGeneration) return;
        timeline = await requestJson('/timeline');
        document.getElementById('replay-progress').max = timeline.packet_count;
        document.getElementById('replay-progress').value = 0;
        if (!restore || !historicalCheckpoint) await requestJson('/replay/reset', { method: 'POST' });
        await loadStops('historical');
        if (generation !== sourceGeneration) return;
        if (restore && historicalCheckpoint) await restoreHistorical(historicalCheckpoint, true);
        if (restore && savedState.source === 'historical') restoreCamera();
        if (restore && selectedVehicleId == null && savedState.source === source) {
            selectedVehicleId = savedState.selectedVehicleId || null;
            renderStops();
            renderCurrent();
        }
        sourceReady = true;
        persistState();
        setStatus(historicalShouldPlay
            ? `История: ${timeline.packet_count.toLocaleString('ru-RU')} пакетов`
            : 'Исторический поток на паузе');
        if (historicalShouldPlay) void startHistorical();
    } else {
        if (restore) {
            const shouldRun = savedState.emulatorRunning !== false;
            if (shouldRun) {
                const status = await requestJson('/emulator/status');
                if (!status.running) throw new Error('эмулятор не работает');
            }
            emulatorExpectedRunning = shouldRun;
            emulatorStartedAt = Date.now();
            emulatorPackets = savedState.emulatorPackets || 0;
            await restoreEmulatorSchedule();
            await loadStops('live');
            await restoreLive(true);
            restoreCamera();
            selectedVehicleId = savedState.selectedVehicleId || null;
            renderStops();
            renderCurrent();
            document.getElementById('start-stream').disabled = shouldRun;
            document.getElementById('pause-stream').disabled = !shouldRun;
        } else await startEmulator();
        if (generation !== sourceGeneration) return;
        sourceReady = true;
        persistState();
    }
}

async function fallbackToHistorical(reason) {
    if (fallbackInProgress || currentSource !== 'emulator') return;
    fallbackInProgress = true;
    emulatorExpectedRunning = false;
    historicalShouldPlay = true;
    fallbackNotice = 'Эмулятор недоступен, включена история';
    document.getElementById('source-select').value = 'historical';
    try {
        await selectSource('historical', { restore: Boolean(historicalCheckpoint), reason });
    } catch (error) {
        retrySourceActivation('historical', Boolean(historicalCheckpoint), error);
    } finally {
        fallbackInProgress = false;
    }
}

function retrySourceActivation(source, restore, error) {
    if (currentSource !== source) return;
    if (source === 'emulator' && error.status === 503) {
        void fallbackToHistorical(error.message);
        return;
    }
    setStatus(`Ожидание backend: ${error.message}`, true);
    setTimeout(() => {
        if (currentSource !== source || sourceReady) return;
        selectSource(source, { restore, reason: 'повторное подключение' })
            .catch(nextError => retrySourceActivation(source, restore, nextError));
    }, 3000);
}

async function recoverAfterReconnect() {
    if (!sourceReady || websocketRecovering) return;
    websocketRecovering = true;
    pendingSnapshots = [];
    const source = currentSource;
    try {
        if (source === 'historical') {
            pauseHistorical(true);
            if (replayTask) await replayTask.catch(() => {});
            if (historicalCheckpoint) await restoreHistorical(historicalCheckpoint, false);
            if (historicalShouldPlay) void startHistorical();
        } else {
            const status = await requestJson('/emulator/status');
            if (emulatorExpectedRunning && !status.running) {
                await fallbackToHistorical('эмулятор остановлен');
            } else {
                await restoreEmulatorSchedule();
                await restoreLive(false);
            }
        }
    } catch (error) {
        if (source === 'emulator' && emulatorExpectedRunning && error.status === 503) {
            await fallbackToHistorical(error.message);
        } else {
            setStatus(`Восстановление соединения: ${error.message}`, true);
            setTimeout(() => {
                if (websocketConnected && sourceReady && currentSource === source) {
                    void recoverAfterReconnect();
                }
            }, 3000);
        }
    } finally {
        websocketRecovering = false;
        if (currentSource === 'emulator') {
            for (const snapshot of pendingSnapshots) {
                if (!currentSnapshot || snapshot.at >= currentSnapshot.at) applySnapshot(snapshot);
            }
        }
        pendingSnapshots = [];
    }
}

function connectWebSocket() {
    const ws = new WebSocket(WS_URL);
    ws.onopen = () => {
        websocketConnected = true;
        if (websocketOpened) void recoverAfterReconnect();
        websocketOpened = true;
    };
    ws.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.type !== 'snapshot') return;
        if (!sourceReady || currentSource === 'historical') return;
        if (websocketRecovering) {
            pendingSnapshots.push(message.data);
            return;
        }
        applySnapshot(message.data);
    };
    ws.onclose = () => {
        websocketConnected = false;
        setTimeout(connectWebSocket, 3000);
    };
}

async function watchEmulator() {
    if (!sourceReady || currentSource !== 'emulator' || !emulatorExpectedRunning
        || fallbackInProgress || websocketRecovering) return;
    try {
        const status = await requestJson('/emulator/status');
        if (!status.running) {
            await fallbackToHistorical('эмулятор остановлен');
            return;
        }
        const lastPacket = Math.max(emulatorStartedAt, emulatorLastPacketAt);
        if (websocketConnected && Date.now() - lastPacket > 15000) {
            await fallbackToHistorical('нет новых пакетов NDTP более 15 секунд');
        }
    } catch (error) {
        if (error.status === 503) await fallbackToHistorical(error.message);
        else setStatus(`Ожидание backend: ${error.message}`, true);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('source-select').value = currentSource;
    document.getElementById('risk-filter').value = savedState.risk || 'all';
    document.getElementById('show-paths').checked = savedState.showPaths !== false;
    document.getElementById('show-stops').checked = savedState.showStops !== false;
    document.getElementById('only-selected').checked = savedState.onlySelected === true;
    initMap();
    connectWebSocket();
    document.getElementById('source-select').addEventListener('change', event => {
        const source = event.target.value;
        selectSource(source).catch(error => retrySourceActivation(source, false, error));
    });
    document.getElementById('start-stream').addEventListener('click', () => {
        (currentSource === 'historical' ? resumeHistorical() : startEmulator())
            .catch(error => {
                if (currentSource === 'emulator' && error.status === 503) void fallbackToHistorical(error.message);
                else setStatus(error.message, true);
            });
    });
    document.getElementById('pause-stream').addEventListener('click', () => {
        if (currentSource === 'historical') {
            pauseHistorical(true);
            historicalShouldPlay = false;
            persistState();
            document.getElementById('pause-stream').disabled = true;
            setStatus('Исторический поток на паузе');
        } else {
            emulatorExpectedRunning = false;
            persistState();
            requestJson('/emulator/stop', { method: 'POST' })
                .then(() => {
                    document.getElementById('start-stream').disabled = false;
                    document.getElementById('pause-stream').disabled = true;
                    setStatus('Эмулятор остановлен');
                }).catch(error => {
                    emulatorExpectedRunning = true;
                    persistState();
                    setStatus(error.message, true);
                });
        }
    });
    document.getElementById('reset-stream').addEventListener('click', () => {
        const source = currentSource;
        selectSource(source).catch(error => retrySourceActivation(source, false, error));
    });
    document.getElementById('show-all').addEventListener('click', showAllVehicles);
    document.getElementById('risk-filter').addEventListener('change', () => { renderCurrent(); persistState(); });
    document.getElementById('show-paths').addEventListener('change', () => { renderCurrent(); persistState(); });
    document.getElementById('show-stops').addEventListener('change', () => { renderStops(); persistState(); });
    document.getElementById('only-selected').addEventListener('change', () => {
        if (document.getElementById('only-selected').checked && !selectedVehicleId) {
            const vehicles = currentSnapshot?.vehicles || [];
            selectedVehicleId = vehicles.find(vehicle => currentSource !== 'emulator'
                || vehicle.source === 'ndtp')?.vehicle_id || null;
        }
        renderStops();
        renderCurrent();
        persistState();
    });
    document.getElementById('clear-filters').addEventListener('click', () => {
        document.getElementById('risk-filter').value = 'all';
        document.getElementById('show-paths').checked = true;
        document.getElementById('show-stops').checked = true;
        document.getElementById('only-selected').checked = false;
        selectedVehicleId = null;
        renderStops();
        renderCurrent();
        persistState();
    });
    document.getElementById('vehicles-table-body').addEventListener('click', event => {
        const row = event.target.closest('[data-vehicle-id]');
        if (row) window.focusVehicle(row.dataset.vehicleId);
    });
    document.getElementById('vehicles-table-body').addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const row = event.target.closest('[data-vehicle-id]');
        if (row) {
            event.preventDefault();
            window.focusVehicle(row.dataset.vehicleId);
        }
    });
    document.getElementById('incidents-list').addEventListener('click', event => {
        const item = event.target.closest('[data-vehicle-id]');
        if (item) window.focusVehicle(item.dataset.vehicleId);
    });
    setInterval(() => {
        const time = currentSource === 'historical' ? currentSnapshot?.at : null;
        document.getElementById('clock').textContent = time
            ? new Date(time).toLocaleString('ru-RU') : new Date().toLocaleTimeString('ru-RU');
    }, 1000);
    setInterval(() => { void watchEmulator(); }, 5000);
    const restore = Boolean(savedState.source);
    selectSource(currentSource, { restore })
        .catch(error => retrySourceActivation(currentSource, restore, error));
});
