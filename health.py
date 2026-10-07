"""Мониторинг здоровья парсеров: сообщает в Telegram, когда сайт «сломался».

Самая вероятная поломка бота — сайт меняет вёрстку, и парсер молча находит
0 карточек. Бот при этом просто «молчит», как будто новых вакансий нет.

Правило:
  • «плохой» прогон парсера = упал с ошибкой ИЛИ увидел 0 карточек (до фильтров);
  • после FAIL_THRESHOLD плохих прогонов подряд — одно сообщение о поломке;
  • после первого хорошего прогона — одно сообщение о восстановлении.
Одиночный сбой (сайт притормозил) не шумит: 3 проверки по 15 мин = 45 минут.

Состояние живёт в памяти процесса: после перезапуска бота счёт начинается заново.
"""
import html
import logging
import threading

FAIL_THRESHOLD = 3


class HealthMonitor:
    def __init__(self, threshold: int = FAIL_THRESHOLD):
        self.threshold = threshold
        self.streak: dict[str, int] = {}     # источник -> плохих прогонов подряд
        self.last_reason: dict[str, str] = {}
        self._pending: list[str] = []        # сообщения, ещё не отправленные в Telegram
        self._alerted: set[str] = set()      # источники, о поломке которых уже сообщено
        # record() зовут из потоков (to_thread), pop_alerts() — из event loop
        self._lock = threading.Lock()

    def record(self, source: str, cards_seen: int, error: Exception | None = None,
               label: str | None = None, immediate: bool = False) -> None:
        """Записывает итог прогона одного источника.

        label — человекочитаемая подпись для оповещения (иначе «Парсер <source>»).
        immediate — сообщить о поломке сразу, не дожидаясь threshold (ошибка конфигурации).
        """
        bad = error is not None or cards_seen == 0
        with self._lock:
            prev = self.streak.get(source, 0)

            if bad:
                reason = f"ошибка: {type(error).__name__}: {error}" if error else "0 карточек на странице"
                self.last_reason[source] = reason
                self.streak[source] = prev + 1
                logging.warning(f"[HEALTH] {source}: плохой прогон #{prev + 1} ({reason})")
                if source not in self._alerted and (immediate or self.streak[source] >= self.threshold):
                    self._alerted.add(source)
                    if label:
                        self._pending.append(
                            f"⚠️ <b>{html.escape(label)}: не работает</b>\n"
                            f"Неудачных проверок подряд: {self.streak[source]}. "
                            f"Причина: {html.escape(reason)}."
                        )
                    else:
                        self._pending.append(
                            f"⚠️ <b>Парсер {source} не работает</b>\n"
                            f"{self.threshold} проверки подряд: {html.escape(reason)}.\n\n"
                            f"Скорее всего, сайт поменял вёрстку или блокирует запросы. "
                            f"Вакансии с {source} сейчас не приходят."
                        )
            else:
                if source in self._alerted:
                    self._alerted.discard(source)
                    if label:
                        self._pending.append(
                            f"✅ <b>{html.escape(label)}: снова работает</b>\n"
                            f"(сбой длился {prev} проверок)."
                        )
                    else:
                        self._pending.append(
                            f"✅ <b>Парсер {source} снова работает</b>\n"
                            f"Найдено карточек: {cards_seen} (сбой длился {prev} проверок)."
                        )
                self.streak[source] = 0

    def streaks(self) -> dict[str, int]:
        """Копия счётчиков под lock: безопасно перебирать из event loop."""
        with self._lock:
            return dict(self.streak)

    def pop_alerts(self) -> list[str]:
        """Забирает накопленные сообщения (и очищает очередь)."""
        with self._lock:
            alerts, self._pending = self._pending, []
            return alerts


# Один монитор на весь процесс бота
monitor = HealthMonitor()
