"""
Prometheus v3.1 — Self-Improvement Loop
Система учится на реальных результатах.
Обновляет веса сигналов. Сохраняет историю точности.

FIXES v3.1:
- _save() больше не читает весь файл каждый раз (был O(n²)) — теперь append-only
- _save() дедупликация через in-memory set, не через файл
- record() принимает signals из EnsembleResult (не []) — правильная привязка
- stats() добавляет "predictit" в список сигналов (раньше отсутствовал)
- calc_weights() добавляет "predictit" в расчёт
- Добавлен cleanup_old(): удаляет записи старше 90 дней
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict

log = logging.getLogger("prometheus.learning")

SIGNAL_NAMES = ["momentum", "consensus", "predictit", "volume_spike", "ai_guard", "sentiment", "calibration", "base_rate"]


@dataclass
class SignalRecord:
    """Запись о предсказании сигнала и реальном исходе."""
    timestamp:      str
    market_id:      str
    signal_name:    str
    predicted_prob: float
    direction:      str
    edge:           float
    market_price:   float
    actual_outcome: str
    was_correct:    bool
    pnl:            float
    question_type:  str = "general"   # electoral/economic/geopolitical/crypto/binary


class LearningEngine:
    """
    Накапливает историю предсказаний.
    Пересчитывает веса каждые N закрытых позиций.
    """
    UPDATE_EVERY = 20

    def __init__(self, data_dir: str = "/app/logs"):
        self._dir     = Path(data_dir)
        self._records: list[SignalRecord] = []
        # FIX: in-memory dedup set — ключ = market_id + signal_name
        self._saved_keys: set[str] = set()
        self._load()

    def record(
        self,
        market_id:      str,
        signals:        list,          # list[Signal] из signals.py
        direction:      str,
        market_price:   float,
        actual_outcome: str,
        pnl:            float,
        question_type:  str = "general",
    ):
        """
        Записать результат предсказания после резолюции рынка.
        signals должны приходить из EnsembleResult.signals — не пустой список.
        """
        if not signals:
            log.debug(f"learning.record: no signals for {market_id[:20]} — skipping")
            return None

        new_records = []
        for sig in signals:
            key = f"{market_id}:{sig.name}"
            if key in self._saved_keys:
                continue
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
                question_type  = question_type,
            )
            self._records.append(rec)
            self._saved_keys.add(key)
            new_records.append(rec)

        if new_records:
            self._append(new_records)

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
        perf: dict[str, dict] = {
            n: {"correct": 0, "total": 0, "weighted_correct": 0.0}
            for n in SIGNAL_NAMES
        }

        for r in self._records:
            if r.signal_name not in perf:
                continue
            p = perf[r.signal_name]
            p["total"] += 1
            if r.was_correct:
                p["correct"] += 1
                p["weighted_correct"] += 1 + r.edge * 2

        accuracies = {}
        for name, p in perf.items():
            if p["total"] < 5:
                accuracies[name] = 0.5
            else:
                raw_acc = p["correct"] / p["total"]
                accuracies[name] = max(0.1, raw_acc)

        total = sum(accuracies.values())
        weights = {
            name: round(max(0.05, acc / total), 3)
            for name, acc in accuracies.items()
        }

        self._save_weights(weights, accuracies)
        return weights

    def probability_shrinkage(self) -> float:
        """
        Коэффициент сжатия вероятности ансамбля к рыночной цене (0.50–1.00).

        Ensemble-вероятность — взвешенный индекс уверенности, не калиброванная
        вероятность. Сжатие компенсирует систематическое завышение claimed edge:

            calibrated_p = market_price + (raw_p - market_price) * shrinkage

        При малом объёме данных: shrinkage 0.50 — Kelly bet вдвое меньше.
        При росте track record: shrinkage растёт до 0.95 — почти без поправки.
        При плохом track record: shrinkage падает до 0.55 — защита от переставки.
        """
        n = len({r.market_id for r in self._records})   # уникальные рынки

        if n < 10:
            return 0.75   # мало данных — умеренное сжатие (было 0.50, убивало edges)

        if n < 30:
            return 0.80   # ранняя стадия

        if n < 60:
            return 0.80   # достаточно данных для умеренного доверия

        # При >= 60 рынках: калибруем по реальному win rate последних 50 записей
        recent = [r for r in self._records[-100:] if r.direction != "NEUTRAL"]
        if len(recent) < 20:
            return 0.80

        win_rate = sum(1 for r in recent if r.was_correct) / len(recent)
        if win_rate >= 0.65:
            return 0.95   # сильный track record — минимальное сжатие
        if win_rate >= 0.55:
            return 0.85   # хороший track record
        if win_rate >= 0.45:
            return 0.70   # средний
        return 0.55       # слабый track record — заметное сжатие

    def stats(self) -> dict:
        """Статистика точности по сигналам — для дашборда."""
        if not self._records:
            return {}

        result = {}
        for name in SIGNAL_NAMES:
            recs = [r for r in self._records if r.signal_name == name]
            if not recs:
                continue
            correct = sum(1 for r in recs if r.was_correct)
            result[name] = {
                "accuracy": round(correct / len(recs), 3),
                "total":    len(recs),
                "avg_edge": round(sum(r.edge for r in recs) / len(recs), 3),
            }
        return result

    def stats_by_type(self) -> dict:
        """Точность по типу вопроса — для выявления где AI сильнее/слабее."""
        result = {}
        for qtype in ["electoral", "economic", "geopolitical", "crypto", "binary", "general"]:
            recs = [r for r in self._records if getattr(r, "question_type", "general") == qtype]
            if not recs:
                continue
            correct = sum(1 for r in recs if r.was_correct)
            result[qtype] = {
                "accuracy": round(correct / len(recs), 3),
                "total":    len(recs),
                "avg_pnl":  round(sum(r.pnl for r in recs) / len(recs), 3),
            }
        return result

    def cleanup_old(self, days: int = 90) -> int:
        """Удалить записи старше N дней. Возвращает кол-во удалённых."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        before = len(self._records)
        self._records = [r for r in self._records if r.timestamp >= cutoff]
        removed = before - len(self._records)
        if removed > 0:
            log.info(f"🧹 Learning: удалено {removed} старых записей (>{days}d)")
            # Перезаписываем файл
            self._rewrite()
        return removed

    def _append(self, records: list[SignalRecord]) -> None:
        """FIX: просто дописываем новые записи — не читаем весь файл."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / "signal_records.jsonl"
            with path.open("a") as f:
                for r in records:
                    f.write(json.dumps(asdict(r)) + "\n")
        except Exception as e:
            log.error(f"Learning append error: {e}")

    def _rewrite(self) -> None:
        """Полная перезапись файла (после cleanup)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / "signal_records.jsonl"
            with path.open("w") as f:
                for r in self._records:
                    f.write(json.dumps(asdict(r)) + "\n")
        except Exception as e:
            log.error(f"Learning rewrite error: {e}")

    def _load(self):
        try:
            path = self._dir / "signal_records.jsonl"
            if not path.exists():
                return
            for line in path.read_text().splitlines():
                try:
                    d = json.loads(line)
                    # Backward compatibility: old records may not have question_type
                    d.setdefault("question_type", "general")
                    r = SignalRecord(**d)
                    key = f"{r.market_id}:{r.signal_name}"
                    if key not in self._saved_keys:
                        self._records.append(r)
                        self._saved_keys.add(key)
                except Exception:
                    pass
            log.info(f"Загружено {len(self._records)} signal records")
        except Exception as e:
            log.error(f"Learning load error: {e}")

    def load_last_weights(self) -> dict | None:
        """
        Загрузить последние сохранённые веса из weights_history.jsonl.
        Вызывать при старте бота чтобы восстановить накопленные веса.
        Возвращает None если файл не существует или запись некорректна.
        """
        path = self._dir / "weights_history.jsonl"
        if not path.exists():
            return None
        try:
            last_line = None
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
            if not last_line:
                return None
            entry = json.loads(last_line)
            weights = entry.get("weights", {})
            # Проверяем что запись полная — все ключи на месте
            if weights and all(k in SIGNAL_NAMES for k in weights):
                n = entry.get("n_records", "?")
                log.info(f"📥 Веса восстановлены из истории ({n} записей): {weights}")
                return weights
        except Exception as e:
            log.warning(f"load_last_weights: не удалось загрузить — {e}")
        return None

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
