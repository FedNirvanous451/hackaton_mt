def detect_patterns(data, horizon_s):
    patterns = []
    if data.speed_change_5m_kmh is not None and data.speed_change_5m_kmh < -10:
        patterns.append("резкое снижение скорости")
    if data.stopped_share_5m is not None and data.stopped_share_5m > 0.5:
        patterns.append("длительная остановка")
    if data.mean_speed_1m_kmh is not None and data.mean_speed_5m_kmh is not None and data.mean_speed_5m_kmh > 0 and data.mean_speed_1m_kmh < data.mean_speed_5m_kmh * 0.6:
        patterns.append("скорость заметно снизилась")
    if data.last_message_age_s is not None and data.last_message_age_s > 60:
        patterns.append("телеметрия давно не обновлялась")
    if data.distance_to_target_km is not None and data.mean_speed_3m_kmh is not None and data.mean_speed_3m_kmh > 0 and horizon_s > 0:
        required_speed_kmh = data.distance_to_target_km / (horizon_s / 3600)
        if required_speed_kmh > data.mean_speed_3m_kmh * 1.5:
            patterns.append("текущей скорости недостаточно для прибытия по расписанию")
    return patterns
