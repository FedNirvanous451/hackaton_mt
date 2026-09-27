// Конфигурация
const WS_URL = `ws://${window.location.host}/api/v1/ws`; // Работает через прокси Nginx или напрямую если порты совпадают
// Если фронтенд на 3000, а бэк на 8000, используй: const WS_URL = "ws://localhost:8000/api/v1/ws";

let map, markersLayer;
let currentSnapshot = null;

// Инициализация карты
function initMap() {
    map = L.map('map').setView([55.7558, 37.6176], 10);
    
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors',
        maxZoom: 19
    }).addTo(map);

    // Делаем карту темной программно
    document.querySelector('.leaflet-tile-pane').style.filter = 'invert(100%) hue-rotate(180deg) brightness(95%) contrast(90%)';
    
    markersLayer = L.layerGroup().addTo(map);
}

// Форматирование времени
function formatTime(isoString) {
    if (!isoString) return "--:--";
    const d = new Date(isoString);
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return "--";
    const m = Math.floor(Math.abs(seconds) / 60);
    const s = Math.abs(seconds) % 60;
    const sign = seconds >= 0 ? "+" : "-";
    return `${sign}${m}:${s.toString().padStart(2, '0')}`;
}

// Обновление KPI
function updateKPI(snapshot) {
    document.getElementById('kpi-total').textContent = snapshot.summary.total_vehicles;
    document.getElementById('kpi-online').textContent = snapshot.summary.located_vehicles;
    document.getElementById('kpi-alerts').textContent = snapshot.alerts.length;
    
    // Средний прогноз задержки по активным алертам
    if (snapshot.alerts.length > 0) {
        const avgDelay = snapshot.alerts.reduce((sum, a) => sum + a.predicted_delay_s, 0) / snapshot.alerts.length;
        document.getElementById('kpi-delay').textContent = formatDuration(avgDelay);
    } else {
        document.getElementById('kpi-delay').textContent = "0:00";
    }

    // Свежесть телеметрии (упрощенно: есть ли stale vehicles)
    const stalePct = snapshot.summary.stale_vehicles / (snapshot.summary.total_vehicles || 1) * 100;
    const freshnessEl = document.getElementById('kpi-freshness');
    freshnessEl.textContent = `${Math.round(100 - stalePct)}%`;
    freshnessEl.style.color = stalePct > 20 ? 'var(--accent-red)' : 'var(--accent-green)';
}

// Рендер инцидентов
function renderIncidents(alerts) {
    const container = document.getElementById('incidents-list');
    if (alerts.length === 0) {
        container.innerHTML = '<div style="padding:20px; color:#8b949e; text-align:center;">Нет активных инцидентов</div>';
        return;
    }

    container.innerHTML = alerts.map(alert => `
        <div class="incident-item" onclick="focusVehicle('${alert.vehicle_id}')">
            <div class="incident-header">
                <div class="risk-dot ${alert.risk === 'red' ? 'risk-red' : 'risk-yellow'}"></div>
                <div>
                    <div class="incident-id">ТС ${alert.tr_id}</div>
                    <div class="incident-route">маршрут ${alert.target_stop.arrival_id}</div>
                </div>
            </div>
            <div class="incident-details">
                <div>${formatDuration(alert.predicted_delay_s)} через ${Math.round(alert.target_stop.planned_at ? (new Date(alert.target_stop.planned_at) - new Date()) / 60000 : 0)} мин</div>
                ${alert.patterns.length ? `<div class="incident-pattern">↓ ${alert.patterns.join(', ')}</div>` : ''}
            </div>
            <button class="btn-details">[Подробнее]</button>
        </div>
    `).join('');
}

// Рендер таблицы ТС
function renderVehicles(vehicles) {
    const tbody = document.getElementById('vehicles-table-body');
    tbody.innerHTML = vehicles.map(v => {
        const riskClass = v.risk === 'red' ? 'risk-red' : v.risk === 'yellow' ? 'risk-yellow' : '';
        const badgeClass = v.forecast_status === 'ml' ? 'badge-ml' : 'badge-base';
        
        return `
            <tr onclick="focusVehicle('${v.vehicle_id}')" style="cursor:pointer">
                <td>${v.tr_id || v.unit_id || 'N/A'}</td>
                <td>${v.target_arrival ? v.target_arrival.arrival_id : '-'}</td>
                <td>${v.speed_kmh ? Math.round(v.speed_kmh) + ' km/h' : '-'}</td>
                <td>${v.schedule_deviation_s ? formatDuration(v.schedule_deviation_s) : '-'}</td>
                <td>${v.prediction ? formatDuration(v.prediction.prediction_s) : '-'}</td>
                <td><div class="risk-dot ${riskClass}" style="display:inline-block"></div></td>
                <td><span class="status-badge ${badgeClass}">${v.forecast_status.toUpperCase()}</span></td>
            </tr>
        `;
    }).join('');
}

// Обновление маркеров на карте
function updateMapMarkers(vehicles) {
    markersLayer.clearLayers();
    
    // Собираем только те машины, у которых есть координаты
    const validVehicles = vehicles.filter(v => v.lon && v.lat);
    
    if (validVehicles.length === 0) return;

    const bounds = [];

    validVehicles.forEach(v => {
        let color = '#2ea043'; // green
        if (v.risk === 'yellow') color = '#d29922';
        if (v.risk === 'red') color = '#da3633';

        // Рисуем точку
        const marker = L.circleMarker([v.lat, v.lon], {
            radius: 8,
            fillColor: color,
            color: '#fff',
            weight: 1,
            opacity: 1,
            fillOpacity: 0.9
        });

        // Добавляем попап с инфой
        const statusText = v.forecast_status === 'no_target' ? 'Нет цели (проверь init-data)' : v.forecast_status;
        marker.bindPopup(`<b>ТС ${v.tr_id}</b><br>Статус: ${statusText}<br>Скорость: ${v.speed_kmh} km/h`);
        
        markersLayer.addLayer(marker);
        bounds.push([v.lat, v.lon]);
    });

    // Плавное перемещение камеры только если есть новые точки
    if (bounds.length > 0) {
        const newBounds = L.latLngBounds(bounds);
        // Не зумим слишком сильно, чтобы не "колбасило"
        map.fitBounds(newBounds, { padding: [50, 50], maxZoom: 14 }); 
    }
}

// Фокус на ТС при клике
window.focusVehicle = function(vehicleId) {
    markersLayer.eachLayer(layer => {
        const popup = layer.getPopup();
        if (popup && popup.getContent().includes(vehicleId)) {
            layer.openPopup();
            map.panTo(layer.getLatLng());
        }
    });
};

// WebSocket подключение
function connectWebSocket() {
    const ws = new WebSocket(WS_URL);
    
    ws.onopen = () => console.log('WS Connected');
    
    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'snapshot') {
            currentSnapshot = msg.data;
            updateKPI(currentSnapshot);
            renderIncidents(currentSnapshot.alerts);
            renderVehicles(currentSnapshot.vehicles);
            updateMapMarkers(currentSnapshot.vehicles);
        }
    };

    ws.onclose = () => {
        console.log('WS Closed. Reconnecting in 3s...');
        setTimeout(connectWebSocket, 3000);
    };
}

// Часы
setInterval(() => {
    document.getElementById('clock').textContent = new Date().toLocaleTimeString('ru-RU');
}, 1000);

// Старт
document.addEventListener('DOMContentLoaded', () => {
    initMap();
    connectWebSocket();
});