"""Журнал увиденных фриланс-заказов: словарь состояний и чистая логика (без БД и сети).

У заказа три НЕЗАВИСИМЫХ измерения - раньше они были смешаны в одном поле status:
  tech      - техническая обработка: filtered (отсечён правилом, причина в reject_reason),
              duplicate (дубль, оригинал в dup_of), queued (ждёт оценки AI, причина в queue_reason),
              assessed (оценён, итог в verdict), stale (устарел, причина в stale_reason),
              legacy (старая запись, причина неизвестна);
  delivery  - доставка: none, pending (оценён, ждёт отправки), card (отдельная карточка),
              digest (строка сводки «ещё N»), unknown (старая запись: сводка или дубль - не различить);
  lead      - сделка, решение пользователя: NULL (не трогал), saved, applied, discussing,
              agreed, done, lost.
Удаление карточки из чата по сроку (card_deleted_at) не меняет ни одного из трёх.
Поле status в БД - лишь «зеркало» для старых запросов и инструментов (legacy_status()).
"""
import difflib
import hashlib
import re

# tech
FILTERED, DUPLICATE, QUEUED, ASSESSED, STALE, LEGACY = (
    "filtered", "duplicate", "queued", "assessed", "stale", "legacy")
TECH_STATES = (FILTERED, DUPLICATE, QUEUED, ASSESSED, STALE, LEGACY)

# queue_reason (для tech=queued)
Q_WAIT, Q_NO_QUOTA, Q_ERROR, Q_RESERVED, Q_NO_TEXT = (
    "ai_wait", "no_quota", "ai_error", "reserved", "no_text")

# verdict (для tech=assessed)
V_OK, V_LOW, V_TRIAGE, V_NO_AI = "ok", "low_fit", "triage_low", "no_ai"

# delivery
D_NONE, D_PENDING, D_CARD, D_DIGEST, D_UNKNOWN = "none", "pending", "card", "digest", "unknown"

# причины устаревания (stale_reason)
S_OLD_AT_DISCOVERY = "old_at_discovery"   # к моменту обнаружения старше предела очереди
S_QUEUE_EXPIRED = "queue_expired"         # не дождался оценки до предела
S_DELIVERY_EXPIRED = "delivery_expired"   # оценён, но не доставлен до предела

# lead: этапы сделки по порядку; LEAD_ACTIVE - сохранённые лиды, которые чистка не трогает
LEAD_SAVED, LEAD_APPLIED, LEAD_DISCUSSING, LEAD_AGREED, LEAD_DONE, LEAD_LOST = (
    "saved", "applied", "discussing", "agreed", "done", "lost")
LEAD_STAGES = (LEAD_SAVED, LEAD_APPLIED, LEAD_DISCUSSING, LEAD_AGREED, LEAD_DONE, LEAD_LOST)
LEAD_ACTIVE = (LEAD_SAVED, LEAD_APPLIED, LEAD_DISCUSSING, LEAD_AGREED)

# Тело короче этого (в нормализованных символах) не позволяет судить о дубле по тексту:
# «Нужен сайт» с пустым описанием у двух разных заказчиков - два разных заказа
MIN_BODY_FOR_TEXT_DUP = 60
TEXT_DUP_RATIO = 0.85
# Короткие тексты: одна биржа, одно название и цена, публикации в пределах этого окна
SHORT_DUP_WINDOW_SEC = 30 * 60
SNIPPET_FILTERED = 400   # отсеянным и дублям хватает короткого обезличенного фрагмента


def norm_text(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", (value or "").lower().replace("ё", "е")).strip()


def title_key(site: str, title: str) -> str:
    return f"{site}|{norm_text(title)}"


def text_key(site: str, title: str, body: str) -> str:
    """Точный ключ текста: биржа + название + начало описания (быстрый поиск оригинала)."""
    return f"{site}|{norm_text(title)}|{norm_text(body)[:300]}"


def content_hash(title: str, body: str) -> str:
    """Отпечаток содержимого: по нему видно, что заказчик поправил описание."""
    raw = f"{norm_text(title)}\n{norm_text(body)[:2000]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def pub_unreliable(site: str, origin: str) -> bool:
    """Дата публикации ненадёжна: alot.pro для Kwork отдаёт время СВОЕГО сбора страницы,
    а не создания проекта (проверено 2026-11-13: у 77 заказов одна и та же минута)."""
    return origin == "alot" and site == "kworkru"


def same_listing(new: dict, cand: dict) -> bool:
    """Тот же ли это заказ, что уже есть в журнале. Вспомогательная проверка ПОСЛЕ того, как
    биржа и название совпали (идентификатора исходной биржи у alot.pro нет).
    new/cand: {"body": нормализованный текст, "origin", "price", "published_ts", "reliable"}.
    - оба текста длинные: сходство текста >= TEXT_DUP_RATIO (так находится и заказ с поправленным
      описанием, и повтор одного заказа тем же источником);
    - иначе по тексту судить нельзя: дубль только из ДРУГОГО источника, с той же ценой и временем
      публикации в пределах SHORT_DUP_WINDOW_SEC. Два однотипных заказа одного источника
      («Создать бота» без описания) не склеиваются."""
    a, b = new.get("body") or "", cand.get("body") or ""
    if len(a) >= MIN_BODY_FOR_TEXT_DUP and len(b) >= MIN_BODY_FOR_TEXT_DUP:
        return difflib.SequenceMatcher(None, a[:600], b[:600], autojunk=False).ratio() >= TEXT_DUP_RATIO
    if new.get("origin") == cand.get("origin"):
        return False
    if float(new.get("price") or 0) != float(cand.get("price") or 0):
        return False
    if not (new.get("reliable") and cand.get("reliable")):
        return False
    try:
        return abs(float(new["published_ts"]) - float(cand["published_ts"])) <= SHORT_DUP_WINDOW_SEC
    except (KeyError, TypeError, ValueError):
        return False


def legacy_status(tech: str | None, delivery: str | None, lead: str | None,
                  card_deleted: bool = False) -> str:
    """Старое поле status (pending/sent/taken/dismissed/expired/rejected) из новых измерений.
    Нужно только старым запросам и инструментам; логика бота опирается на tech/delivery/lead."""
    if lead in (LEAD_APPLIED, LEAD_DISCUSSING, LEAD_AGREED, LEAD_DONE):
        return "taken"
    if lead == LEAD_LOST:
        return "dismissed"
    if tech == QUEUED or (tech == ASSESSED and delivery == D_PENDING):
        return "pending"
    if delivery == D_CARD:
        return "expired" if card_deleted else "sent"
    if tech == STALE:
        return "expired"
    return "rejected"


def status_to_states(status: str, fit: int | None, attempts: int = 0) -> dict:
    """Новые измерения по старому status - для вызовов freelance_save без явных состояний
    (совместимость). Возвращает {tech, delivery, verdict, queue_reason, lead}; None - не задавать."""
    out = {"tech": None, "delivery": None, "verdict": None, "queue_reason": None, "lead": None}
    if status == "pending":
        if fit is not None:
            out.update(tech=ASSESSED, delivery=D_PENDING, verdict=V_OK)
        else:
            out.update(tech=QUEUED, queue_reason=Q_ERROR if attempts else Q_WAIT)
    elif status == "sent":
        out.update(tech=ASSESSED, delivery=D_CARD, verdict=V_OK if fit is not None else V_NO_AI)
    elif status == "taken":
        out.update(tech=ASSESSED, delivery=D_CARD, verdict=V_OK, lead=LEAD_AGREED)
    elif status == "dismissed":
        out.update(tech=ASSESSED, delivery=D_CARD, verdict=V_OK, lead=LEAD_LOST)
    elif status == "expired":
        out.update(tech=STALE)
    elif status == "rejected":
        if fit is not None:
            out.update(tech=ASSESSED, verdict=V_LOW)
        else:
            out.update(tech=LEGACY)
    return out
