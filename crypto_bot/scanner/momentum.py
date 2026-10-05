"""As-of lookup: no interpolation, lookahead or fabricated zero deltas."""
from datetime import timedelta


def score_momentum(current, timestamp, history, windows, tolerance_minutes):
    past = sorted((x for x in history if x.timestamp < timestamp), key=lambda x: x.timestamp)
    result = {"previous_score": past[-1].total_score if past else None}
    for hours in windows:
        target = timestamp - timedelta(hours=hours)
        matches = [x for x in past if target - timedelta(minutes=tolerance_minutes) <= x.timestamp <= target]
        value = matches[-1].total_score if matches else None
        result[f"score_{hours}h_ago"] = value
        result[f"delta_{hours}h"] = current - value if value is not None else None
    return result
