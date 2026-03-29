"""
Prometheus v3 — Self-Improvement Loop
Система учится на реальных результатах.
Обновляет веса сигналов. Сохраняет историю точности.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict

log = logging.getLogger("prometheus.learning")


@dataclass
class SignalRecord:
    """Запись о предсказании сигнала и реальном исходе."""
    timestamp:      str
    market_id:      str
    signal_name:    str    # sentiment / momentum / calibration / consensus / base_rate
    predicted_prob: float  # что сигнал предсказал
    direction:      str    # YES / NO
    edge:           float
    market_price:   float
    actual_outcome: str    # YES / NO
    was_correct:    bool
    pnl:            float


class LearningEngine:
    """
    Накапливает историю предсказаний.
    Пересчитывает веса каждые N закрытых позиций.
    """
    UPDATE_EVERY = 20   # пересчитывать веса каждые 20 закрытых позиций

    def __init__(self, data_dir: str = "/app/logs"):
        self._dir     = Path(data_dir)
        self._records: list[SignalRecord] = []
        self._load()

    def record(
        self,
        market_id:      str,
        signals:        list,          # list[Signal] из signals.py
        direction:      str,
        market_price:   float,
        actual_outcome: str,
        pnl:            float,
    ):
        """Записать результат предсказания после резолюции рынка."""
        for sig in signals:
            rec = SignalRecord(
                timestamp      = datetime.now(timezone.utc).isoformat(),
                market_id      = market_id,
                signal_name    = sig.name,
                predicted_prob = sig.score,
                direction      = sig.direction,
                edge           = abs(sig.score - market_price),
                market_price   = market_price,
                actual_outcome = actual_outcome,
                was_correct    = sig.direction == actual_outcome,
                pnl            = pnl,
            )
            self._records.append(rec)

        self._save()

        # Пересчитываем веса каждые UPDATE_EVERY уникальных рынков
        unique_markets = len({r.market_id for r in self._records})
        if unique_markets > 0 and unique_markets % self.UPDATE_EVERY == 0:
            new_weights = self.calc_weights()
            log.info(f"🧠 Веса обновлены после {unique_markets} рынков: {new_weights}")
            return new_weights

        return None

    def calc_weights(self) -> dict[str, float]:
        """
        Посчитать новые веса на основе реальной точности каждого сигнала.
        Сигнал который часто прав → получает больший вес.
        """
        signal_names = ["sentiment", "momentum", "calibration", "consensus", "base_rate"]
        perf: dict[str, dict] = {
            n: {"correct": 0, "total": 0, "weighted_correct": 0.0}
            for n in signal_names
        }

        for r in self._records:
            if r.signal_name not in perf:
                continue
            p = perf[r.signal_name]
            p["total"] += 1
            if r.was_correct:
                p["correct"] += 1
                # Большие edge вносят больший вклад
                p["weighted_correct"] += 1 + r.edge * 2

        # Accuracy с взвешиванием по edge
        accuracies = {}
        for name, p in perf.items():
            if p["total"] < 5:
                accuracies[name] = 0.5  # нейтрально если мало данных
            else:
                raw_acc = p["correct"] / p["total"]
                # Штрафуем сигналы которые хуже random (< 0.5)
                accuracies[name] = max(0.1, raw_acc)

        # Нормализуем в веса (сумма = 1)
        total = sum(accuracies.values())
        weights = {
            name: round(max(0.05, acc / total), 3)
            for name, acc in accuracies.items()
        }

        # Сохранить историю весов
        self._save_weights(weights, accuracies)
        return weights

    def stats(self) -> dict:
        """Статистика точности по сигналам — для дашборда."""
        if not self._records:
            return {}

        result = {}
        for name in ["sentiment", "momentum", "calibration", "consensus", "base_rate"]:
            recs = [r for r in self._records if r.signal_name == name]
            if not recs:
                continue
            correct = sum(1 for r in recs if r.was_correct)
            result[name] = {
                "accuracy":   round(correct / len(recs), 3),
                "total":      len(recs),
                "avg_edge":   round(sum(r.edge for r in recs) / len(recs), 3),
            }

        return result

    def _save(self):
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / "signal_records.jsonl"
            # Appending только новые
            existing = set()
            if path.exists():
                for line in path.read_text().splitlines():
                    try:
                        existing.add(json.loads(line).get("market_id","") +
                                     json.loads(line).get("signal_name",""))
                    except Exception:
                        pass

            with path.open("a") as f:
                for r in self._records:
                    key = r.market_id + r.signal_name
                    if key not in existing:
                        f.write(json.dumps(asdict(r)) + "\n")
                        existing.add(key)
        except Exception as e:
            log.error(f"Learning save error: {e}")

    def _load(self):
        try:
            path = self._dir / "signal_records.jsonl"
            if not path.exists():
                return
            for line in path.read_text().splitlines():
                try:
                    d = json.loads(line)
                    self._records.append(SignalRecord(**d))
                except Exception:
                    pass
            log.info(f"Загружено {len(self._records)} signal records")
        except Exception as e:
            log.error(f"Learning load error: {e}")

    def _save_weights(self, weights: dict, accuracies: dict):
        try:
            path = self._dir / "weights_history.jsonl"
            entry = {
                "ts":         datetime.now(timezone.utc).isoformat(),
                "weights":    weights,
                "accuracies": {k: round(v, 3) for k, v in accuracies.items()},
                "n_records":  len(self._records),
            }
            with path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.error(f"Weights history save error: {e}")
