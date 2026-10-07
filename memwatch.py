"""Наблюдение за памятью контейнера (лимит BotHost - 1 ГБ).

mem_used_mb() считает так же, как `docker stats`: память cgroup (вместе с
Chromium и Node-драйвером) минус неактивный файловый кэш — его ядро сбрасывает
само, до OOM-killer дело из-за него не доходит. Вне Linux возвращает None.
"""
import gc
import logging
import os
import sys

MEM_LIMIT_MB = 1024
MEM_WARN_MB = int(os.getenv("MEM_WARN_MB", "850"))

_MB = 1024 * 1024

# (файл текущего потребления, файл статистики, поле неактивного кэша, файл пика)
_CGROUPS = (
    ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.stat",
     "inactive_file", "/sys/fs/cgroup/memory.peak"),                       # cgroup v2
    ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.stat",
     "total_inactive_file", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"),  # cgroup v1
)

peak = 0  # максимум наблюдённого значения, МБ (по нашим замерам)


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="ascii") as f:
            value = int(f.read().strip())
    except (OSError, ValueError):  # нет файла, 'max', мусор
        return None
    return value if value >= 0 else None


def _stat_value(path: str, key: str) -> int:
    try:
        with open(path, "r", encoding="ascii") as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value)
    except (OSError, ValueError):
        pass
    return 0


def mem_used_mb() -> int | None:
    """Память контейнера в МБ без неактивного файлового кэша, или None."""
    global peak
    for current_file, stat_file, inactive_key, _ in _CGROUPS:
        usage = _read_int(current_file)
        if usage is None:
            continue
        inactive = _stat_value(stat_file, inactive_key)
        used = max(usage - inactive, 0) // _MB
        if used > peak:
            peak = used
        return used
    return None


def peak_mb() -> int:
    """Пик с момента старта контейнера.

    Ядро помнит настоящий пик (memory.peak), в том числе в моменты, когда
    Chromium открыт, а мы не замеряем. В нём есть и файловый кэш, так что это
    оценка сверху. Если файла нет, берём максимум собственных замеров.
    """
    for _, _, _, peak_file in _CGROUPS:
        kernel_peak = _read_int(peak_file)
        if kernel_peak is not None:
            return max(kernel_peak // _MB, peak)
    return peak


def trim() -> int:
    """gc.collect() + возврат свободных страниц glibc системе. Число собранных объектов."""
    collected = gc.collect()
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass  # musl/нет libc.so.6: не критично
    return collected


def log_mem(tag: str) -> None:
    used = mem_used_mb()
    if used is None:
        logging.debug(f"[MEM] после {tag}: недоступно")
        return
    logging.info(f"[MEM] после {tag}: {used} МБ (пик {peak_mb()})")
